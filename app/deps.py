"""前置依赖闸门、等待舱与开抽编排。

判定口径（全站唯一一套）：
- 一条流的所有祖先（直接 + 传递）都必须是「已落库」才算齐套；
- 齐套才允许开抽（entry_visible / entry_enabled / gate.state 全部由
  :func:`evaluate_gate` 产出，列表入口显隐与抽取闸共用这一结果）；
- 未齐套时下游进入等待舱，舱位持久化、可回读；
- 依赖页/详情页拿到的是 snapshot 快照；真正开抽时会重新做一次 live 校验，
  两者不混用（快照永远不会被当成放行凭据）；
- 抽取闸串行放行：并发开抽排队，后到者留下争用记录。
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from datetime import datetime, timezone

# 状态口径
STATUS_READY = "已落库"          # 前置作业收尾
STATUS_EXTRACTED = "已抽取待落库"
STATUS_PENDING = "待抽取"
STATUS_FAILED = "抽取失败"

# 依赖策略（人类可读）
STRATEGY_CODE = "all_ancestors_landed"
STRATEGY_NAME = "前置全链路齐套后放行（所有祖先作业已落库）"
LIST_LABEL = "依赖列表"
CABIN_LABEL = "等待舱"

GATE_KEY = "extract_gate"
GATE_TIMEOUT = 3.0          # 排队等待上限（秒）
CONTENTION_WINDOW = 0.75    # 相邻开抽的争用判定窗口（秒）
ARRIVAL_RETENTION = 60.0    # 到达登记保留时长（秒），过后清理

_SNAPSHOT_KEYS = (
    "snapshot",
    "snapshot_id",
    "snapshot_ready",
    "ready",
    "gate",
    "gate_ready",
    "entry_visible",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _as_int(value):
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def ensure(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS dep_edges (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      flow_id INTEGER NOT NULL,
      parent_id INTEGER NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS wait_cabin (
      flow_id INTEGER PRIMARY KEY,
      note TEXT NOT NULL,
      enqueued_at TEXT
    )""")
    # 兼容旧库：补 enqueued_at 列
    cols = {r[1] for r in conn.execute("PRAGMA table_info(wait_cabin)")}
    if "enqueued_at" not in cols:
        try:
            conn.execute("ALTER TABLE wait_cabin ADD COLUMN enqueued_at TEXT")
        except sqlite3.OperationalError:
            pass
    conn.execute("""CREATE TABLE IF NOT EXISTS gate_state (
      gate_key TEXT PRIMARY KEY,
      holder_flow_id INTEGER,
      holder_token TEXT,
      acquired_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS gate_history (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      gate_key TEXT NOT NULL,
      flow_id INTEGER NOT NULL,
      arrived_at REAL,
      started_at REAL,
      ended_at REAL,
      outcome TEXT NOT NULL
    )""")
    # 在途开抽登记（用于面板展示当前闸内活动）
    conn.execute("""CREATE TABLE IF NOT EXISTS gate_inflight (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      gate_key TEXT NOT NULL,
      flow_id INTEGER NOT NULL,
      arrived_at REAL NOT NULL
    )""")
    # 到达登记（只增不删，按到达次序做确定性 FIFO 争用判定）
    conn.execute("""CREATE TABLE IF NOT EXISTS gate_arrivals (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      gate_key TEXT NOT NULL,
      flow_id INTEGER NOT NULL,
      arrived_at REAL NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS gate_contests (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      gate_key TEXT NOT NULL,
      flow_id INTEGER NOT NULL,
      holder_flow_id INTEGER,
      outcome TEXT NOT NULL,
      note TEXT,
      waited_ms REAL,
      at TEXT NOT NULL
    )""")


# ---------------------------------------------------------------------------
# 基础读取
# ---------------------------------------------------------------------------
def _flow_row(conn, flow_id):
    return conn.execute(
        "SELECT id, title, code, status, source_uri, extracted, landed "
        "FROM flows WHERE id=?",
        (flow_id,),
    ).fetchone()


def _parent_ids(conn, flow_id):
    rows = conn.execute(
        "SELECT parent_id FROM dep_edges WHERE flow_id=? ORDER BY id", (flow_id,)
    )
    out = []
    for r in rows:
        pid = int(r[0])
        if pid not in out:
            out.append(pid)
    return out


def _reason(status):
    if status == STATUS_PENDING:
        return "前置尚未抽取"
    if status == STATUS_EXTRACTED:
        return "前置已抽取但未落库"
    if status == STATUS_FAILED:
        return "前置抽取失败，未收尾"
    if status == STATUS_READY:
        return "前置已落库"
    return "前置作业未收尾"


# ---------------------------------------------------------------------------
# 齐套判定（唯一口径）
# ---------------------------------------------------------------------------
def evaluate_gate(conn, flow_id, source="snapshot"):
    """计算某条流的前置齐套结果。

    source: "snapshot"（依赖页/列表/详情读到的快照）或 "live"（真正开抽时重验）。
    返回里的 entry_visible / entry_enabled / gate.state 都由同一份结果派生。
    """
    ensure(conn)
    flow = _flow_row(conn, flow_id)
    checked_at = _now()
    if flow is None:
        return {
            "flow_id": flow_id,
            "ready": False,
            "open": False,
            "gate_open": False,
            "state": "closed",
            "entry_visible": False,
            "entry_enabled": False,
            "blocked": True,
            "blockers": [{
                "id": flow_id, "title": None, "status": None, "direct": False,
                "reason": "流不存在", "blocking": True,
            }],
            "missing": [flow_id],
            "cycles": [],
            "direct_parents": [],
            "ancestors": [],
            "strategy": STRATEGY_CODE,
            "strategy_name": STRATEGY_NAME,
            "checked_at": checked_at,
            "source": source,
            "snapshot": source == "snapshot",
            "live": source == "live",
            "revalidated": source == "live",
            "error": "流不存在",
        }

    direct = _parent_ids(conn, flow_id)
    direct_set = set(direct)
    seen = set()
    order = []
    cycles = []

    def walk(node, stack):
        if node in stack:
            start = stack.index(node)
            cycles.append(stack[start:] + [node])
            return
        if node in seen:
            return
        seen.add(node)
        order.append(node)
        for parent in _parent_ids(conn, node):
            walk(parent, stack + [node])

    for pid in direct:
        walk(pid, [flow_id])

    blockers = []
    ancestor_map = {}
    for aid in order:
        row = _flow_row(conn, aid)
        if row is None:
            ancestor_map[aid] = None
            blockers.append({
                "id": aid, "title": None, "status": None,
                "direct": aid in direct_set, "reason": "前置不存在",
                "blocking": True,
            })
            continue
        ancestor_map[aid] = row["status"]
        if row["status"] != STATUS_READY:
            blockers.append({
                "id": aid,
                "title": row["title"],
                "status": row["status"],
                "direct": aid in direct_set,
                "reason": _reason(row["status"]),
                "blocking": True,
            })
    for cycle in cycles:
        for aid in cycle:
            if aid == flow_id:
                blockers.append({
                    "id": aid, "title": flow["title"], "status": flow["status"],
                    "direct": aid in direct_set, "reason": "循环依赖", "blocking": True,
                })
                break

    # 去重（同一前置只保留一条阻断原因）
    deduped = []
    seen_blockers = set()
    for b in blockers:
        key = (b["id"], b["reason"])
        if key in seen_blockers:
            continue
        seen_blockers.add(key)
        deduped.append(b)

    ready = not deduped
    return {
        "flow_id": flow_id,
        "ready": ready,
        "open": ready,
        "gate_open": ready,
        "state": "open" if ready else "closed",
        "entry_visible": ready,
        "entry_enabled": ready,
        "blocked": not ready,
        "blockers": deduped,
        "missing": [b["id"] for b in deduped if b["reason"] == "前置不存在"],
        "cycles": cycles,
        "direct_parents": direct,
        "ancestors": order,
        "checked_statuses": ancestor_map,
        "strategy": STRATEGY_CODE,
        "strategy_name": STRATEGY_NAME,
        "checked_at": checked_at,
        "source": source,
        "snapshot": source == "snapshot",
        "live": source == "live",
        "revalidated": source == "live",
    }


# ---------------------------------------------------------------------------
# 等待舱
# ---------------------------------------------------------------------------
def _cabin_row(conn, flow_id):
    row = conn.execute(
        "SELECT flow_id, note, enqueued_at FROM wait_cabin WHERE flow_id=?",
        (flow_id,),
    ).fetchone()
    if not row:
        return None
    flow = _flow_row(conn, flow_id)
    return {
        "flow_id": int(row["flow_id"]),
        "title": flow["title"] if flow else None,
        "status": flow["status"] if flow else None,
        "note": row["note"],
        "enqueued_at": row["enqueued_at"],
        "waiting": True,
        "in_cabin": True,
    }


def cabin_entry(conn, flow_id):
    ensure(conn)
    return _cabin_row(conn, flow_id)


def _upsert_cabin(conn, flow_id, note):
    existing = conn.execute(
        "SELECT enqueued_at FROM wait_cabin WHERE flow_id=?", (flow_id,)
    ).fetchone()
    enqueued_at = existing["enqueued_at"] if existing and existing["enqueued_at"] else _now()
    conn.execute(
        "INSERT OR REPLACE INTO wait_cabin(flow_id, note, enqueued_at) VALUES (?,?,?)",
        (flow_id, note, enqueued_at),
    )


def _leave_cabin(conn, flow_id):
    conn.execute("DELETE FROM wait_cabin WHERE flow_id=?", (flow_id,))


def _cabin_list(conn):
    rows = conn.execute(
        "SELECT flow_id, note, enqueued_at FROM wait_cabin ORDER BY enqueued_at, flow_id"
    ).fetchall()
    return [_cabin_row(conn, int(r["flow_id"])) for r in rows]


def _blocker_note(gate):
    parts = []
    for b in gate["blockers"][:3]:
        title = b.get("title") or f"#{b['id']}"
        parts.append(f"{title}（{b.get('reason')}）")
    suffix = "…" if len(gate["blockers"]) > 3 else ""
    return "前置未齐：" + "、".join(parts) + suffix


# ---------------------------------------------------------------------------
# 抽取闸（串行放行 + 争用留痕）
# ---------------------------------------------------------------------------
def _acquire_gate(conn, flow_id, token):
    """原子占闸；返回 (是否拿到, 是否排队等待过)。"""
    deadline = time.monotonic() + GATE_TIMEOUT
    waited = False
    while True:
        try:
            conn.execute(
                "INSERT INTO gate_state(gate_key, holder_flow_id, holder_token, acquired_at) "
                "VALUES (?,?,?,?)",
                (GATE_KEY, flow_id, token, str(time.time())),
            )
            conn.commit()
            return True, waited
        except sqlite3.IntegrityError:
            conn.rollback()
            waited = True
            # 回收崩溃残留的闸门持有者，避免永久卡死
            try:
                row = conn.execute(
                    "SELECT acquired_at FROM gate_state WHERE gate_key=?",
                    (GATE_KEY,),
                ).fetchone()
                if row and row["acquired_at"]:
                    if time.time() - float(row["acquired_at"]) > GATE_TIMEOUT * 10:
                        conn.execute(
                            "DELETE FROM gate_state WHERE gate_key=?", (GATE_KEY,)
                        )
                        conn.commit()
                        continue
            except (sqlite3.OperationalError, TypeError, ValueError):
                conn.rollback()
        except sqlite3.OperationalError:
            conn.rollback()
            waited = True
        if time.monotonic() >= deadline:
            return False, True
        time.sleep(0.005)


def _release_gate(conn, token):
    try:
        conn.execute(
            "DELETE FROM gate_state WHERE gate_key=? AND holder_token=?",
            (GATE_KEY, token),
        )
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()


def _register_inflight(conn, flow_id, arrived):
    """登记在途开抽并当场判定「先到/后到」。

    到达登记只增不删（gate_arrivals），因此即使对方的在途记录在本请求
    判定前就已清除，也能按登记次序得到确定性的 FIFO 结论。
    返回 (用于清理的在途登记 id, 先到者 flow_id 或 None)。
    """
    # 顺带清理过期的到达登记，避免无限增长
    try:
        conn.execute(
            "DELETE FROM gate_arrivals WHERE gate_key=? AND arrived_at<?",
            (GATE_KEY, arrived - ARRIVAL_RETENTION),
        )
    except sqlite3.OperationalError:
        pass
    cur = conn.execute(
        "INSERT INTO gate_arrivals(gate_key, flow_id, arrived_at) VALUES (?,?,?)",
        (GATE_KEY, flow_id, arrived),
    )
    arrival_id = int(cur.lastrowid)
    cur2 = conn.execute(
        "INSERT INTO gate_inflight(gate_key, flow_id, arrived_at) VALUES (?,?,?)",
        (GATE_KEY, flow_id, arrived),
    )
    inflight_id = int(cur2.lastrowid)
    conn.commit()
    return inflight_id, _prior_in_window(conn, flow_id, arrival_id, arrived)


def _prior_in_window(conn, flow_id, arrival_id, self_arrived):
    """同争用窗口内、比本请求更早登记的其它流。"""
    try:
        rows = conn.execute(
            "SELECT flow_id, arrived_at FROM gate_arrivals "
            "WHERE gate_key=? AND id<? AND flow_id!=? ORDER BY id DESC",
            (GATE_KEY, arrival_id, flow_id),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for row in rows:
        if self_arrived - float(row["arrived_at"]) <= CONTENTION_WINDOW:
            return int(row["flow_id"])
    return None


def _clear_inflight(conn, inflight_id):
    try:
        conn.execute("DELETE FROM gate_inflight WHERE id=?", (inflight_id,))
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()


def _gate_holder(conn):
    row = conn.execute(
        "SELECT holder_flow_id, holder_token, acquired_at FROM gate_state WHERE gate_key=?",
        (GATE_KEY,),
    ).fetchone()
    if not row:
        return None
    flow = _flow_row(conn, int(row["holder_flow_id"]))
    return {
        "flow_id": int(row["holder_flow_id"]),
        "title": flow["title"] if flow else None,
        "acquired_at": row["acquired_at"],
        "token": row["holder_token"],
    }


def _recent_history(conn, limit=20):
    rows = conn.execute(
        "SELECT flow_id, arrived_at, started_at, ended_at, outcome "
        "FROM gate_history WHERE gate_key=? ORDER BY id DESC LIMIT ?",
        (GATE_KEY, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _mark_history(conn, flow_id, arrived, started, outcome):
    conn.execute(
        "INSERT INTO gate_history(gate_key, flow_id, arrived_at, started_at, ended_at, outcome) "
        "VALUES (?,?,?,?,?,?)",
        (GATE_KEY, flow_id, arrived, started, time.time(), outcome),
    )


def _record_contest(conn, flow_id, holder_flow_id, outcome, note, waited_ms):
    conn.execute(
        "INSERT INTO gate_contests(gate_key, flow_id, holder_flow_id, outcome, note, waited_ms, at) "
        "VALUES (?,?,?,?,?,?,?)",
        (GATE_KEY, flow_id, holder_flow_id, outcome, note, waited_ms, _now()),
    )


def _contests(conn, limit=20):
    rows = conn.execute(
        "SELECT flow_id, holder_flow_id, outcome, note, waited_ms, at "
        "FROM gate_contests WHERE gate_key=? ORDER BY id DESC LIMIT ?",
        (GATE_KEY, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _snapshot_inputs(body):
    if not isinstance(body, dict):
        return {}
    return {k: body[k] for k in _SNAPSHOT_KEYS if k in body}


def extract_gated(conn, flow_id, body=None):
    """带闸门开抽。真正开抽前一定重新做 live 齐套校验，快照不参与放行。"""
    ensure(conn)
    body = body if isinstance(body, dict) else {}
    flow_id = _as_int(flow_id)
    flow = _flow_row(conn, flow_id) if flow_id is not None else None
    if flow is None:
        return {"ok": False, "not_found": True, "missing_flow": True, "error": "流不存在"}

    frow = _flow_row(conn, flow_id)
    if frow["status"] == STATUS_READY and not _parent_ids(conn, flow_id):
        # 无前置且已落库：保持幂等，无需要占闸
        gate = evaluate_gate(conn, flow_id, source="live")
        return {
            "ok": True, "extracted": True, "changed": False,
            "flow": dict(frow), "gate": gate, "entry_visible": gate["entry_visible"],
            "queued": False, "contended": False, "waited_ms": 0,
            "revalidated": True, "snapshot_ignored": bool(_snapshot_inputs(body)),
            "strategy_name": STRATEGY_NAME,
        }

    arrived = time.time()
    token = uuid.uuid4().hex
    # 在途登记：登记瞬间就按序号完成「先到/后到」判定，避免事后查不到对方
    inflight_id, prior_flow_id = _register_inflight(conn, flow_id, arrived)
    contended_before = prior_flow_id is not None
    try:
        acquired, waited = _acquire_gate(conn, flow_id, token)
        started = time.time()
        if not acquired:
            holder = _gate_holder(conn)
            holder_id = holder["flow_id"] if holder else prior_flow_id
            _record_contest(
                conn, flow_id, holder_id, "contended_rejected",
                "抽取闸被占用，等待超时后仍未放行", (started - arrived) * 1000.0,
            )
            gate = evaluate_gate(conn, flow_id, source="live")
            return {
                "ok": False,
                "blocked": False,
                "contended": True,
                "queued": True,
                "admitted": False,
                "error": "抽取闸争用：已排队但未在等待上限内放行",
                "detail": "抽取闸争用：已排队但未在等待上限内放行",
                "gate": gate,
                "flow": dict(frow),
                "holder": holder,
                "waited_ms": round((started - arrived) * 1000.0, 1),
                "revalidated": True,
                "snapshot_ignored": bool(_snapshot_inputs(body)),
                "strategy_name": STRATEGY_NAME,
            }

        # 后到（或确有排队等待）即为争用方，须留痕
        contended = bool(contended_before or waited)
        ended_ok = False
        try:
            # 真正开抽时重验（live）：不信任任何客户端快照
            gate = evaluate_gate(conn, flow_id, source="live")
            if not gate["ready"]:
                _upsert_cabin(conn, flow_id, _blocker_note(gate))
                if contended:
                    _record_contest(
                        conn, flow_id, prior_flow_id, "contended_blocked",
                        "排队放行后重验仍前置未齐", (started - arrived) * 1000.0,
                    )
                _mark_history(conn, flow_id, arrived, started, "blocked")
                ended_ok = True
                return {
                    "ok": False,
                    "blocked": True,
                    "contended": contended,
                    "queued": bool(contended),
                    "error": "前置未齐",
                    "detail": "前置未齐",
                    "gate": gate,
                    "blockers": gate["blockers"],
                    "entry_visible": False,
                    "flow": dict(_flow_row(conn, flow_id)),
                    "cabin": _cabin_row(conn, flow_id),
                    "in_cabin": True,
                    "waited_ms": round((started - arrived) * 1000.0, 1),
                    "revalidated": True,
                    "snapshot_ignored": bool(_snapshot_inputs(body)),
                    "strategy_name": STRATEGY_NAME,
                }

            changed = False
            if frow["status"] in (STATUS_PENDING, STATUS_FAILED):
                n = 15 + (flow_id * 4) % 10
                conn.execute(
                    "UPDATE flows SET status=?, extracted=?, landed=0 WHERE id=?",
                    (STATUS_EXTRACTED, n, flow_id),
                )
                changed = True
            _leave_cabin(conn, flow_id)
            conn.commit()
            new_flow = dict(_flow_row(conn, flow_id))
            if contended:
                _record_contest(
                    conn, flow_id, prior_flow_id, "contended_admitted",
                    "并发开抽已排队放行，后到者留痕", (started - arrived) * 1000.0,
                )
            _mark_history(conn, flow_id, arrived, started, "ok")
            ended_ok = True
            return {
                "ok": True,
                "extracted": True,
                "changed": changed,
                "flow": new_flow,
                "gate": gate,
                "entry_visible": True,
                "blockers": [],
                "queued": contended,
                "contended": contended,
                "admitted": True,
                "waited_ms": round((started - arrived) * 1000.0, 1),
                "revalidated": True,
                "snapshot_ignored": bool(_snapshot_inputs(body)),
                "strategy_name": STRATEGY_NAME,
            }
        finally:
            if not ended_ok:
                try:
                    _mark_history(conn, flow_id, arrived, started, "error")
                except sqlite3.OperationalError:
                    pass
    finally:
        _clear_inflight(conn, inflight_id)
        _release_gate(conn, token)


# ---------------------------------------------------------------------------
# 流对象装订（列表/详情/面板共用）
# ---------------------------------------------------------------------------
def annotate_flow(conn, flow, source="snapshot"):
    gate = evaluate_gate(conn, flow["id"], source=source)
    cabin = _cabin_row(conn, flow["id"])
    out = dict(flow)
    out.update({
        "gate": gate,
        "deps_gate": gate,
        "gate_state": gate["state"],
        "entry_visible": gate["entry_visible"],
        "entry_enabled": gate["entry_enabled"],
        "blocked": gate["blocked"],
        "blockers": gate["blockers"],
        "cabin": cabin,
        "wait_cabin": cabin,
        "in_cabin": bool(cabin),
        "waiting": bool(cabin),
        "cabin_note": cabin["note"] if cabin else None,
        "wait_note": cabin["note"] if cabin else None,
        "dependency_strategy": STRATEGY_NAME,
    })
    return out


# ---------------------------------------------------------------------------
# 依赖面板 / 动作
# ---------------------------------------------------------------------------
def panel_payload(conn, args=None):
    ensure(conn)
    args = args or {}
    taken_at = _now()
    flows = [dict(r) for r in conn.execute(
        "SELECT id, title, code, status, source_uri, extracted, landed FROM flows ORDER BY id"
    )]
    gates = {str(f["id"]): evaluate_gate(conn, f["id"], source="snapshot") for f in flows}
    by_id = {f["id"]: f for f in flows}

    cabin = _cabin_list(conn)
    cabin_by_flow = {c["flow_id"]: c for c in cabin}

    edges = []
    for r in conn.execute("SELECT id, flow_id, parent_id FROM dep_edges ORDER BY id"):
        e = dict(r)
        gate = gates.get(str(e["flow_id"])) or evaluate_gate(
            conn, e["flow_id"], source="snapshot"
        )
        down = by_id.get(e["flow_id"]) or {}
        up = by_id.get(e["parent_id"]) or {}
        e.update({
            "downstream_id": e["flow_id"],
            "upstream_id": e["parent_id"],
            "downstream_title": down.get("title"),
            "downstream_status": down.get("status"),
            "parent_id": e["parent_id"],
            "upstream_title": up.get("title"),
            "parent_title": up.get("title"),
            "parent_status": up.get("status"),
            "upstream_status": up.get("status"),
            "ready": gate["ready"],
            "entry_visible": gate["entry_visible"],
            "entry_enabled": gate["entry_enabled"],
            "blocked": gate["blocked"],
            "gate_state": gate["state"],
            "blockers": gate["blockers"],
            "strategy_name": STRATEGY_NAME,
        })
        edges.append(e)

    entries = []
    for f in flows:
        gate = gates[str(f["id"])]
        has_parents = bool(gate["direct_parents"])
        cab = cabin_by_flow.get(f["id"])
        entries.append({
            "flow_id": f["id"],
            "id": f["id"],
            "title": f["title"],
            "code": f["code"],
            "status": f["status"],
            "is_downstream": has_parents,
            "downstream": has_parents,
            "ready": gate["ready"],
            "entry_visible": gate["entry_visible"],
            "entry_enabled": gate["entry_enabled"],
            "blocked": gate["blocked"],
            "gate_state": gate["state"],
            "gate": gate,
            "blockers": gate["blockers"],
            "in_cabin": bool(cab),
            "waiting": bool(cab),
            "cabin": cab,
            "cabin_note": cab["note"] if cab else None,
            "strategy_name": STRATEGY_NAME,
        })

    focused_id = _as_int(args.get("flow_id"))
    focused_gate = gates.get(str(focused_id)) if focused_id is not None else None

    return {
        "edges": edges,
        "entries": entries,
        "downstreams": [e for e in entries if e["is_downstream"]],
        "cabin": cabin,
        "wait_list": cabin,
        "cabin_count": len(cabin),
        "cabin_label": CABIN_LABEL,
        "gates": gates,
        "focused_gate": focused_gate,
        "flow_gate": focused_gate,
        "strategy": STRATEGY_CODE,
        "strategy_code": STRATEGY_CODE,
        "strategy_name": STRATEGY_NAME,
        "strategy_label": STRATEGY_NAME,
        "strategy_readable": True,
        "list_label": LIST_LABEL,
        "gate": {
            "key": GATE_KEY,
            "holder": _gate_holder(conn),
            "contests": _contests(conn),
            "history": _recent_history(conn),
            "strategy_name": STRATEGY_NAME,
        },
        "contests": _contests(conn),
        "gate_holder": _gate_holder(conn),
        "gate_history": _recent_history(conn),
        "snapshot": {
            "source": "snapshot",
            "taken_at": taken_at,
            "snapshot_id": uuid.uuid4().hex,
            "gates": gates,
        },
        "snapshot_taken_at": taken_at,
    }


def mutate_action(conn, body):
    ensure(conn)
    body = body if isinstance(body, dict) else {}
    action = body.get("action") or "extract"
    flow_id = _as_int(
        body.get("flow_id")
        if body.get("flow_id") is not None
        else (body.get("downstream_id") if body.get("downstream_id") is not None else body.get("id"))
    )
    if flow_id is None:
        flow_id = 1

    if action == "link":
        parent_id = _as_int(
            body.get("parent_id")
            if body.get("parent_id") is not None
            else (body.get("upstream_id") if body.get("upstream_id") is not None else body.get("parent"))
        )
        if parent_id is None or _flow_row(conn, flow_id) is None or _flow_row(conn, parent_id) is None:
            return {"ok": False, "error": "编排流或前置不存在"}
        if parent_id == flow_id:
            return {"ok": False, "error": "不能依赖自身（循环依赖）"}
        # 若 flow 已是 parent 的祖先，再加边就会成环
        if flow_id in _ancestor_ids(conn, parent_id):
            return {"ok": False, "error": "循环依赖：前置已依赖当前流"}
        exists = conn.execute(
            "SELECT id FROM dep_edges WHERE flow_id=? AND parent_id=?",
            (flow_id, parent_id),
        ).fetchone()
        if exists:
            edge_id = int(exists["id"])
            created = False
        else:
            cur = conn.execute(
                "INSERT INTO dep_edges(flow_id, parent_id) VALUES (?,?)",
                (flow_id, parent_id),
            )
            edge_id = int(cur.lastrowid)
            created = True
        gate = evaluate_gate(conn, flow_id, source="snapshot")
        return {
            "ok": True, "created": created, "edge_id": edge_id,
            "edge": {"id": edge_id, "flow_id": flow_id, "parent_id": parent_id},
            "gate": gate, "entry_visible": gate["entry_visible"],
            "blockers": gate["blockers"], "strategy_name": STRATEGY_NAME,
        }

    if action in ("unlink", "unlink_parent"):
        parent_id = _as_int(body.get("parent_id") or body.get("upstream_id"))
        if parent_id is None:
            conn.execute("DELETE FROM dep_edges WHERE flow_id=?", (flow_id,))
        else:
            conn.execute(
                "DELETE FROM dep_edges WHERE flow_id=? AND parent_id=?",
                (flow_id, parent_id),
            )
        gate = evaluate_gate(conn, flow_id, source="snapshot")
        return {"ok": True, "gate": gate, "entry_visible": gate["entry_visible"],
                "strategy_name": STRATEGY_NAME}

    if action == "wait":
        if _flow_row(conn, flow_id) is None:
            return {"ok": False, "error": "流不存在"}
        note = str(body.get("note") or "等待前置").strip() or "等待前置"
        _upsert_cabin(conn, flow_id, note)
        cabin = _cabin_row(conn, flow_id)
        gate = evaluate_gate(conn, flow_id, source="snapshot")
        return {
            "ok": True, "cabin": cabin, "waiting": True, "in_cabin": True,
            "cabin_note": cabin["note"], "gate": gate,
            "entry_visible": gate["entry_visible"], "blockers": gate["blockers"],
            "strategy_name": STRATEGY_NAME,
        }

    if action in ("leave_wait", "unwait", "release"):
        _leave_cabin(conn, flow_id)
        gate = evaluate_gate(conn, flow_id, source="snapshot")
        return {"ok": True, "cabin": None, "waiting": False, "in_cabin": False,
                "gate": gate, "entry_visible": gate["entry_visible"],
                "strategy_name": STRATEGY_NAME}

    if action in ("gate", "status", "check"):
        gate = evaluate_gate(conn, flow_id, source="snapshot")
        return {"ok": True, "gate": gate, "ready": gate["ready"],
                "entry_visible": gate["entry_visible"], "blockers": gate["blockers"],
                "strategy_name": STRATEGY_NAME}

    if action == "extract":
        return extract_gated(conn, flow_id, body)

    return {"ok": False, "error": f"未知操作：{action}"}


def _ancestor_ids(conn, flow_id):
    seen = set()
    stack = list(_parent_ids(conn, flow_id))
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(_parent_ids(conn, node))
    return seen
