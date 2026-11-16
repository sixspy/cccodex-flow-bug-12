from __future__ import annotations

def ensure(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS dep_edges (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      flow_id INTEGER NOT NULL,
      parent_id INTEGER NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS wait_cabin (
      flow_id INTEGER PRIMARY KEY,
      note TEXT NOT NULL
    )""")

def panel_payload(conn, args):
    ensure(conn)
    edges = [dict(r) for r in conn.execute("SELECT * FROM dep_edges")]
    return {"edges": edges, "cabin": [], "strategy_name": "进舱", "list_label": "依赖列表"}

def mutate_action(conn, body):
    ensure(conn)
    action = body.get("action") or "extract"
    flow_id = int(body.get("flow_id") or 1)
    if action == "link":
        parent_id = int(body.get("parent_id") or 0)
        conn.execute(
            "INSERT INTO dep_edges(flow_id, parent_id) VALUES (?,?)",
            (flow_id, parent_id),
        )
        return {"ok": True}
    if action == "wait":
        conn.execute(
            "INSERT OR REPLACE INTO wait_cabin(flow_id, note) VALUES (?,?)",
            (flow_id, "等待前置"),
        )
        # 立刻删掉制造丢失
        conn.execute("DELETE FROM wait_cabin WHERE flow_id=?", (flow_id,))
        return {"ok": True, "cabin": True}
    # extract：只查直接 parent 是否「已落库」，忽略父的父；父在「已抽取待落库」也放行
    parents = [
        r[0]
        for r in conn.execute(
            "SELECT parent_id FROM dep_edges WHERE flow_id=?", (flow_id,)
        )
    ]
    for pid in parents:
        st = conn.execute("SELECT status FROM flows WHERE id=?", (pid,)).fetchone()
        if st and st[0] == "待抽取":
            return {"ok": False, "error": "前置未齐"}
    conn.execute(
        "UPDATE flows SET status=?, extracted=10 WHERE id=?",
        ("已抽取待落库", flow_id),
    )
    return {"ok": True, "extracted": True, "downstream_visible": True}
