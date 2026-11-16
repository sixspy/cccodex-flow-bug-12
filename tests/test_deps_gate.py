"""前置依赖闸门 / 等待舱 / 并发开抽 的验收测试。"""
import threading
import time

import pytest

from app.db import get_db, init_db
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    init_db(str(db))
    _req, resp = app.test_client.post(
        "/api/login", json={"username": "admin", "password": "123456"}
    )
    cookie = resp.cookies.get("flow_session")
    return app.test_client, {"Cookie": f"flow_session={cookie}"}, str(db)


def _post(client_tuple, path, payload):
    client, headers, _db = client_tuple
    _req, resp = client.post(path, json=payload, headers=headers)
    return resp


def _get(client_tuple, path):
    client, headers, _db = client_tuple
    _req, resp = client.get(path, headers=headers)
    return resp


def _set_status(db, flow_id, status):
    with get_db(db) as conn:
        conn.execute("UPDATE flows SET status=? WHERE id=?", (status, flow_id))


def test_gate_blocks_when_transitive_parent_pending(client):
    # 1 -> 2 -> 3 (3 依赖 2，2 依赖 1)
    _post(client, "/api/deps/action", {"action": "link", "flow_id": 2, "parent_id": 1})
    _post(client, "/api/deps/action", {"action": "link", "flow_id": 3, "parent_id": 2})
    _set_status(client[2], 1, "已落库")
    _set_status(client[2], 2, "已抽取待落库")  # 直接父未收尾

    resp = _post(client, "/api/flows/3/extract", {})
    assert resp.status == 409
    assert resp.json["blocked"] is True
    assert resp.json["ok"] is False
    ids = {b["id"] for b in resp.json["blockers"]}
    assert 2 in ids  # 直接父阻断

    flow = _get(client, "/api/flows/3").json
    assert flow["status"] == "待抽取"  # 没有偷偷开抽
    assert flow["entry_visible"] is False
    assert flow["gate"]["state"] == "closed"


def test_gate_uses_all_ancestors_not_only_direct_parent(client):
    # 1 -> 2 -> 3；2 已落库但祖父 1 未落库 -> 3 仍须被拦
    _post(client, "/api/deps/action", {"action": "link", "flow_id": 2, "parent_id": 1})
    _post(client, "/api/deps/action", {"action": "link", "flow_id": 3, "parent_id": 2})
    _set_status(client[2], 2, "已落库")
    _set_status(client[2], 1, "待抽取")

    resp = _post(client, "/api/flows/3/extract", {})
    assert resp.status == 409
    ids = {b["id"] for b in resp.json["blockers"]}
    assert 1 in ids
    assert 2 not in ids


def test_gate_opens_only_when_all_ancestors_landed(client):
    _post(client, "/api/deps/action", {"action": "link", "flow_id": 2, "parent_id": 1})
    _post(client, "/api/deps/action", {"action": "link", "flow_id": 3, "parent_id": 2})
    _set_status(client[2], 1, "已落库")
    _set_status(client[2], 2, "已落库")

    flow = _get(client, "/api/flows/3").json
    assert flow["entry_visible"] is True
    assert flow["gate"]["state"] == "open"

    resp = _post(client, "/api/flows/3/extract", {})
    assert resp.status == 200
    assert resp.json["ok"] is True
    assert resp.json["revalidated"] is True


def test_cabin_persists_and_is_readable_after_refresh(client):
    _post(client, "/api/deps/action", {"action": "link", "flow_id": 2, "parent_id": 1})
    _post(client, "/api/deps/action", {"action": "wait", "flow_id": 2, "note": "等目录扫描"})

    # 模拟刷新：重新拉详情 / 面板 / 舱位
    flow = _get(client, "/api/flows/2").json
    assert flow["in_cabin"] is True
    assert flow["cabin"]["note"] == "等目录扫描"

    cabin = _get(client, "/api/flows/2/cabin").json
    assert cabin["in_cabin"] is True

    panel = _get(client, "/api/deps/panel").json
    assert any(c["flow_id"] == 2 for c in panel["cabin"])

    # 阻断开抽也会把下游写进舱位
    resp = _post(client, "/api/flows/2/extract", {})
    assert resp.status == 409
    assert resp.json["in_cabin"] is True


def test_entry_visibility_and_gate_share_one_judgement(client):
    _post(client, "/api/deps/action", {"action": "link", "flow_id": 2, "parent_id": 1})

    flow = _get(client, "/api/flows/2").json
    panel = _get(client, "/api/deps/panel").json
    entry = next(e for e in panel["entries"] if e["flow_id"] == 2)
    # 三处结果必须同源
    assert flow["entry_visible"] == flow["gate"]["entry_visible"] == flow["gate"]["open"] is False
    assert entry["entry_visible"] == flow["entry_visible"]
    assert entry["gate_state"] == flow["gate"]["state"]

    _set_status(client[2], 1, "已落库")
    flow = _get(client, "/api/flows/2").json
    panel = _get(client, "/api/deps/panel").json
    entry = next(e for e in panel["entries"] if e["flow_id"] == 2)
    assert flow["entry_visible"] is True
    assert entry["entry_visible"] is True
    assert flow["gate"]["state"] == "open"


def test_snapshot_and_live_revalidation_not_mixed(client):
    # 依赖页看到快照（此刻齐套），随后前置退回未齐，开抽必须用 live 结果拦住
    _post(client, "/api/deps/action", {"action": "link", "flow_id": 2, "parent_id": 1})
    _set_status(client[2], 1, "已落库")
    panel = _get(client, "/api/deps/panel").json
    snap = panel["gates"]["2"]
    assert snap["ready"] is True and snap["source"] == "snapshot"

    _set_status(client[2], 1, "待抽取")  # 快照已过期

    # 客户端把快照回传，也不得作为放行依据
    resp = _post(client, "/api/flows/2/extract", {
        "snapshot": snap, "ready": True, "gate_ready": True, "entry_visible": True,
    })
    assert resp.status == 409
    body = resp.json
    assert body["blocked"] is True
    assert body["gate"]["source"] == "live"
    assert body["revalidated"] is True
    assert body["snapshot_ignored"] is True


def test_snapshot_carries_no_live_flag(client):
    panel = _get(client, "/api/deps/panel").json
    assert panel["snapshot"]["source"] == "snapshot"
    for gate in panel["gates"].values():
        assert gate["snapshot"] is True
        assert gate["live"] is False
        assert "revalidated" in gate


def _extract_concurrently(db_path, flow_ids):
    """用各自独立的连接并发开抽（sanic 测试客户端不是线程安全的）。"""
    from app.deps import extract_gated

    results = {}
    start = threading.Barrier(len(flow_ids))

    def fire(fid):
        start.wait()  # 确保两侧同时开抽
        with get_db(db_path) as conn:
            results[fid] = extract_gated(conn, fid, {})

    threads = [threading.Thread(target=fire, args=(fid,)) for fid in flow_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_extract_serialises_and_later_one_leaves_contention(client):
    # 两条无前置、都在「待抽取」的下游同时开抽
    results = _extract_concurrently(client[2], [1, 2])

    assert all(r["ok"] for r in results.values())
    # 两者都不得静默互相覆盖：必有一方被判定为后到并留痕
    assert any(r["contended"] for r in results.values())

    panel = _get(client, "/api/deps/panel").json
    contests = panel["contests"]
    assert contests, "后到者必须留下争用记录"
    # 闸门最终已释放
    assert panel["gate_holder"] is None
    # 状态没有被互相覆盖到非法值
    for fid in (1, 2):
        st = _get(client, f"/api/flows/{fid}").json["status"]
        assert st in ("已抽取待落库", "已落库", "待抽取")


def test_concurrent_extract_never_lets_both_bypass_gate(client):
    # 两个下游都依赖一个未落库的前置：并发开抽必须都被拦
    _post(client, "/api/deps/action", {"action": "link", "flow_id": 2, "parent_id": 1})
    _post(client, "/api/deps/action", {"action": "link", "flow_id": 3, "parent_id": 1})
    results = _extract_concurrently(client[2], [2, 3])

    assert all(r["blocked"] for r in results.values())
    for fid in (2, 3):
        assert _get(client, f"/api/flows/{fid}").json["status"] == "待抽取"


def test_strategy_name_is_readable(client):
    panel = _get(client, "/api/deps/panel").json
    assert panel["strategy_name"]
    assert not panel["strategy_name"].isascii()
    flow = _get(client, "/api/flows/1").json
    assert flow["dependency_strategy"]
    assert flow["gate"]["strategy_name"] == flow["dependency_strategy"]


def test_cycle_is_detected(client):
    _post(client, "/api/deps/action", {"action": "link", "flow_id": 2, "parent_id": 1})
    resp = _post(client, "/api/deps/action", {"action": "link", "flow_id": 1, "parent_id": 2})
    assert resp.status == 409


def test_flow_without_parents_can_extract(client):
    flow = _get(client, "/api/flows/4").json
    assert flow["entry_visible"] is True
    resp = _post(client, "/api/flows/4/extract", {})
    assert resp.status == 200 and resp.json["ok"] is True


def test_cabin_cleared_after_successful_extract(client):
    _post(client, "/api/deps/action", {"action": "link", "flow_id": 2, "parent_id": 1})
    _post(client, "/api/deps/action", {"action": "wait", "flow_id": 2, "note": "等待前置"})
    assert _get(client, "/api/flows/2").json["in_cabin"] is True

    _set_status(client[2], 1, "已落库")
    resp = _post(client, "/api/flows/2/extract", {})
    assert resp.status == 200 and resp.json["ok"] is True
    assert _get(client, "/api/flows/2").json["in_cabin"] is False
    panel = _get(client, "/api/deps/panel").json
    assert all(c["flow_id"] != 2 for c in panel["cabin"])


def test_entry_hidden_means_gate_closed(client):
    """入口显隐与闸门必须同源：不存在「入口隐但闸门放行」。"""
    _post(client, "/api/deps/action", {"action": "link", "flow_id": 2, "parent_id": 1})
    panel = _get(client, "/api/deps/panel").json
    for entry in panel["entries"]:
        g = entry["gate"]
        assert entry["entry_visible"] == g["entry_visible"]
        assert entry["entry_enabled"] == g["entry_enabled"]
        assert entry["entry_visible"] == (g["state"] == "open")
        if not entry["entry_visible"]:
            # 入口隐的流绝不能被开抽放行
            r = _post(client, f"/api/flows/{entry['flow_id']}/extract", {})
            assert r.status == 409 and r.json["blocked"] is True


def test_wait_action_without_prereq_is_readable(client):
    # 即使尚未登记依赖，也能入舱并在刷新后回读
    resp = _post(client, "/api/deps/action", {"action": "wait", "flow_id": 4, "note": "手工等待"})
    assert resp.status == 200
    assert resp.json["cabin"]["note"] == "手工等待"
    assert _get(client, "/api/flows/4").json["cabin"]["note"] == "手工等待"
