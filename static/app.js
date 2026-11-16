/* 编排流台：组件化 SPA（Svelte 式状态驱动渲染，无构建步骤便于容器验收） */
const state = { user: '', lab: '', items: [], selected: null, plantPanel: null };

async function api(path, opts) {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || j.error || '请求失败');
  return j;
}

async function boot() {
  try {
    const me = await api('/api/me');
    state.user = me.user;
    state.lab = me.lab;
    state.items = (await api('/api/flows')).items;
    try { state.plantPanel = await api('/api/deps/panel'); } catch (e) { state.plantPanel = { error: String(e) }; }
    render();
  } catch (e) {
    location.href = '/login';
  }
}

async function select(id) {
  state.selected = await api('/api/flows/' + id);
  render();
}

async function doExtract() {
  state.selected = await api('/api/flows/' + state.selected.id + '/extract', { method: 'POST' });
  state.items = (await api('/api/flows')).items;
  render();
}

async function doLand() {
  state.selected = await api('/api/flows/' + state.selected.id + '/land', { method: 'POST' });
  state.items = (await api('/api/flows')).items;
  render();
}

async function logout() {
  await api('/api/logout', { method: 'POST' });
  location.href = '/login';
}

function render() {
  const el = document.getElementById('app');
  const rows = state.items.map((it) => `
    <button type="button" class="row ${state.selected && state.selected.id === it.id ? 'on' : ''}" data-id="${it.id}">
      <strong>${it.title}</strong>
      <span class="mono">${it.code}</span>
      <span class="chip">${it.status}</span>
    </button>`).join('');
  const detail = state.selected ? `
    <section class="detail">
      <h2>${state.selected.title}</h2>
      <p class="mono">${state.selected.source_uri}</p>
      <p>状态 <span class="chip">${state.selected.status}</span> · 已抽取 ${state.selected.extracted} · 已落库 ${state.selected.landed}</p>
      <div class="actions">
        <button type="button" id="btn-ex">执行抽取</button>
        <button type="button" id="btn-land" class="ghost">执行落库</button>
      </div>
    </section>` : '<p class="hint">从左侧选择一条编排流</p>';
  const plant = state.plantPanel ? `<section class="detail plant-panel"><h3>前置依赖</h3><pre>${JSON.stringify(state.plantPanel,null,2)}</pre><button type="button" id="plant-act">执行前置依赖</button></section>` : '';
  el.innerHTML = `
    <header class="bar"><div class="brand">${state.lab}</div><div>${state.user} · <button type="button" id="out" class="link">退出</button></div></header>
    <div class="layout"><aside>${rows}</aside><main>${detail}${plant}</main></div>`;
  el.querySelectorAll('.row').forEach((b) => { b.onclick = () => select(+b.dataset.id); });
  const out = el.querySelector('#out'); if (out) out.onclick = logout;
  const be = el.querySelector('#btn-ex'); if (be) be.onclick = doExtract;
  const bl = el.querySelector('#btn-land'); if (bl) bl.onclick = doLand;
  const pa = el.querySelector('#plant-act'); if (pa) pa.onclick = async () => {
    state.plantPanel = await api('/api/deps/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ flow_id: state.selected && state.selected.id }) });
    state.items = (await api('/api/flows')).items; render();
  };
}

boot();
