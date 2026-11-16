/* 编排流台：组件化 SPA（Svelte 式状态驱动渲染，无构建步骤便于容器验收） */
const state = {
  user: '',
  lab: '',
  items: [],
  selected: null,
  plantPanel: null,
  // 依赖页快照（snapshot）与真正开抽重验（live）分开存放，绝不互相覆盖
  snapshot: null,
  live: null,
  banner: null,
  busy: new Set(),
};

async function api(path, opts) {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, body: j };
}

async function apiOrThrow(path, opts) {
  const res = await api(path, opts);
  if (!res.ok) {
    const err = new Error(res.body.detail || res.body.error || '请求失败');
    err.body = res.body;
    err.status = res.status;
    throw err;
  }
  return res.body;
}

async function boot() {
  try {
    const me = await apiOrThrow('/api/me');
    state.user = me.user;
    state.lab = me.lab;
    await refreshLists();
    await refreshPanel();
    render();
  } catch (e) {
    location.href = '/login';
  }
}

async function refreshLists() {
  const data = await apiOrThrow('/api/flows');
  state.items = data.items;
  state.strategyName = data.strategy_name;
}

async function refreshPanel() {
  try {
    state.plantPanel = await apiOrThrow('/api/deps/panel');
    state.snapshot = state.plantPanel.snapshot || null;
  } catch (e) {
    state.plantPanel = { error: String(e.message || e) };
  }
}

async function select(id) {
  state.selected = await apiOrThrow('/api/flows/' + id);
  state.banner = null;
  render();
}

function gateOf(item) {
  return (item && item.gate) || null;
}

function entryVisible(item) {
  // 与后端闸门同一判定：entry_visible / entry_enabled 由同一次 evaluate_gate 派生
  const g = gateOf(item);
  return !!(g && g.entry_visible && g.entry_enabled && g.state === 'open');
}

async function doExtract() {
  const target = state.selected;
  if (!target || state.busy.has(target.id)) return;
  state.busy.add(target.id);
  render();
  try {
    const res = await api('/api/flows/' + target.id + '/extract', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ flow_id: target.id }),
    });
    const body = res.body || {};
    // 真正开抽后的结果来自 live 重验，单独记录，不回填快照
    state.live = body.gate || null;
    if (res.ok && body.ok) {
      state.banner = body.contended
        ? { kind: 'warn', text: `已排队放行（等待 ${Math.round(body.waited_ms || 0)}ms，存在并发争用）` }
        : { kind: 'ok', text: '开抽放行：前置齐套已通过开抽时的实时重验' };
    } else {
      state.banner = {
        kind: 'err',
        text: body.error || body.detail || '开抽未放行',
      };
    }
    await refreshLists();
    await refreshPanel();
    if (body.flow) {
      state.selected = await apiOrThrow('/api/flows/' + target.id);
    }
  } catch (e) {
    state.banner = { kind: 'err', text: e.message || '开抽失败' };
  } finally {
    state.busy.delete(target.id);
    render();
  }
}

async function actOnDeps(body) {
  const res = await api('/api/deps/action', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  state.banner = res.ok
    ? { kind: 'ok', text: '等待舱已更新' }
    : { kind: 'err', text: res.body.error || res.body.detail || '操作失败' };
  await refreshLists();
  await refreshPanel();
  if (state.selected) state.selected = await apiOrThrow('/api/flows/' + state.selected.id);
  render();
}

async function doLand() {
  state.selected = await apiOrThrow('/api/flows/' + state.selected.id + '/land', { method: 'POST' });
  await refreshLists();
  await refreshPanel();
  render();
}

async function logout() {
  await api('/api/logout', { method: 'POST' });
  location.href = '/login';
}

function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]
  ));
}

function gateBadge(g) {
  if (!g) return '<span class="chip muted">未判定</span>';
  return g.state === 'open'
    ? '<span class="chip ok">闸门开</span>'
    : '<span class="chip bad">闸门闭</span>';
}

function gateSourceLabel(g) {
  if (!g) return '';
  return g.source === 'live'
    ? '<span class="src live">开抽时重验（实时）</span>'
    : '<span class="src snap">页面快照</span>';
}

function blockersHtml(g) {
  if (!g || !g.blockers || !g.blockers.length) return '';
  return `<ul class="blockers">${g.blockers.map((b) => `
    <li><span class="mono">#${esc(b.id)}</span> ${esc(b.title || '未知流')}
      ${b.status ? `<span class="chip muted">${esc(b.status)}</span>` : ''}
      <span class="muted">${esc(b.reason)}</span>
      ${b.direct ? '<span class="tag">直接前置</span>' : '<span class="tag">传递前置</span>'}
    </li>`).join('')}</ul>`;
}

function itemRow(it) {
  const sel = state.selected && state.selected.id === it.id;
  const g = gateOf(it);
  const blocked = !entryVisible(it);
  return `
    <button type="button" class="row ${sel ? 'on' : ''}" data-id="${it.id}">
      <strong>${esc(it.title)}</strong>
      <span class="mono">${esc(it.code)}</span>
      <span class="row-chips">
        <span class="chip">${esc(it.status)}</span>
        ${it.in_cabin ? '<span class="chip wait">等待舱</span>' : ''}
        ${g && g.state !== 'open' ? '<span class="chip bad">入口受限</span>' : ''}
        ${blocked ? '' : '<span class="chip ok">可开抽</span>'}
      </span>
    </button>`;
}

function detailHtml() {
  if (!state.selected) return '<p class="hint">从左侧选择一条编排流</p>';
  const s = state.selected;
  const g = gateOf(s);
  const allowed = entryVisible(s);
  const cabin = s.cabin || null;
  return `
    <section class="detail">
      <h2>${esc(s.title)} <span class="mono">${esc(s.code)}</span></h2>
      <p class="mono">${esc(s.source_uri)}</p>
      <p>状态 <span class="chip">${esc(s.status)}</span> · 已抽取 ${esc(s.extracted)} · 已落库 ${esc(s.landed)}</p>
      <div class="gate-line">
        ${gateBadge(g)} ${gateSourceLabel(g)}
        <span class="muted">策略：${esc(s.dependency_strategy || (g && g.strategy_name) || '—')}</span>
      </div>
      ${blockersHtml(g)}
      ${cabin ? `<p class="cabin-line">等待舱：<strong>${esc(cabin.note)}</strong>
        <span class="muted">（入舱 ${esc(cabin.enqueued_at || '—')}，刷新后仍可回读）</span></p>` : ''}
      <div class="actions">
        ${allowed
          ? `<button type="button" id="btn-ex" ${state.busy.has(s.id) ? 'disabled' : ''}>
               ${state.busy.has(s.id) ? '排队开抽…' : '执行抽取'}</button>`
          : `<button type="button" class="blocked" disabled title="前置未齐，闸门关闭">执行抽取（前置未齐）</button>`}
        <button type="button" id="btn-land" class="ghost">执行落库</button>
        ${cabin
          ? '<button type="button" id="btn-unwait" class="ghost">移出等待舱</button>'
          : '<button type="button" id="btn-wait" class="ghost">进入等待舱</button>'}
      </div>
      <p class="hint small">入口显隐与开抽闸门共用同一套齐套判定；开抽时会在服务端重新做一次实时齐套校验，
        页面快照不作为放行凭据。</p>
    </section>`;
}

function cabinHtml() {
  const list = (state.plantPanel && state.plantPanel.cabin) || [];
  if (!list.length) return '<p class="hint small">等待舱为空</p>';
  return `<ul class="cabin-list">${list.map((c) => `
    <li><span class="mono">#${esc(c.flow_id)}</span> ${esc(c.title || '未知流')}
      <span class="chip wait">${esc(c.note)}</span>
      <span class="muted">${esc(c.enqueued_at || '')}</span>
    </li>`).join('')}</ul>`;
}

function panelHtml() {
  const p = state.plantPanel;
  if (!p) return '';
  if (p.error) {
    return `<section class="detail plant-panel"><h3>前置依赖</h3><p class="err">${esc(p.error)}</p></section>`;
  }
  const edges = p.edges || [];
  const contests = p.contests || [];
  const edgeRows = edges.length
    ? edges.map((e) => `
        <tr>
          <td class="mono">#${esc(e.upstream_id)}</td>
          <td>${esc(e.upstream_title || '未知流')}</td>
          <td class="arrow">→</td>
          <td class="mono">#${esc(e.downstream_id)}</td>
          <td>${esc(e.downstream_title || '未知流')}</td>
          <td>${e.entry_visible ? '<span class="chip ok">入口开</span>' : '<span class="chip bad">入口隐</span>'}</td>
          <td>${e.gate_state === 'open' ? '<span class="chip ok">闸开</span>' : '<span class="chip bad">闸闭</span>'}</td>
        </tr>`).join('')
    : '<tr><td colspan="7" class="hint small">暂无依赖边</td></tr>';
  return `
    <section class="detail plant-panel">
      <h3>前置依赖 · ${esc(p.list_label || '依赖列表')}</h3>
      <p class="muted small">依赖策略：<strong>${esc(p.strategy_name)}</strong>
        ${gateSourceLabel({ source: 'snapshot' })}
        <span class="mono">snapshot ${esc(p.snapshot_taken_at || '')}</span></p>
      <table class="deps-table">
        <thead><tr><th>前置</th><th>作业</th><th></th><th>下游</th><th>作业</th><th>入口</th><th>闸门</th></tr></thead>
        <tbody>${edgeRows}</tbody>
      </table>
      <h4>${esc(p.cabin_label || '等待舱')}（${esc(p.cabin_count || 0)}）</h4>
      ${cabinHtml()}
      ${contests.length ? `<h4>闸门争用记录</h4><ul class="contest-list">${contests.slice(0, 5).map((c) => `
        <li><span class="mono">#${esc(c.flow_id)}</span> ${esc(c.outcome)}
          <span class="muted">${esc(c.note || '')} · 等待 ${Math.round(c.waited_ms || 0)}ms</span></li>`).join('')}</ul>` : ''}
      <div class="actions">
        <button type="button" id="plant-act">登记当前流为等待舱</button>
      </div>
    </section>`;
}

function render() {
  const el = document.getElementById('app');
  const rows = state.items.map(itemRow).join('');
  const banner = state.banner
    ? `<div class="banner ${esc(state.banner.kind)}">${esc(state.banner.text)}</div>` : '';
  let liveBox = '';
  if (state.live) {
    liveBox = `<section class="detail live-box">
      <h3>开抽时重验结果（live）</h3>
      <p>${gateBadge(state.live)} ${gateSourceLabel(state.live)}
        <span class="muted">策略：${esc(state.live.strategy_name || '—')} · 校验于 ${esc(state.live.checked_at || '')}</span></p>
      ${blockersHtml(state.live)}
    </section>`;
  }
  el.innerHTML = `
    <header class="bar"><div class="brand">${esc(state.lab)}</div>
      <div>${esc(state.user)} · <button type="button" id="out" class="link">退出</button></div></header>
    ${banner}
    <div class="layout">
      <aside>${rows || '<p class="hint">暂无编排流</p>'}</aside>
      <main>${detailHtml()}${liveBox}${panelHtml()}</main>
    </div>`;
  el.querySelectorAll('.row').forEach((b) => { b.onclick = () => select(+b.dataset.id); });
  const out = el.querySelector('#out'); if (out) out.onclick = logout;
  const be = el.querySelector('#btn-ex'); if (be) be.onclick = doExtract;
  const bl = el.querySelector('#btn-land'); if (bl) bl.onclick = doLand;
  const bw = el.querySelector('#btn-wait');
  if (bw) bw.onclick = () => {
    if (!state.selected) return;
    actOnDeps({ action: 'wait', flow_id: state.selected.id, note: '等待前置' });
  };
  const bu = el.querySelector('#btn-unwait');
  if (bu) bu.onclick = () => {
    if (!state.selected) return;
    actOnDeps({ action: 'leave_wait', flow_id: state.selected.id });
  };
  const pa = el.querySelector('#plant-act');
  if (pa) pa.onclick = () => {
    if (!state.selected) { state.banner = { kind: 'err', text: '请先选择一条流' }; render(); return; }
    actOnDeps({ action: 'wait', flow_id: state.selected.id, note: '等待前置' });
  };
}

boot();
