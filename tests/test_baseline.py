def test_login_page(client):
    _req, resp = client.get("/login")
    assert resp.status == 200
    assert "登录" in resp.text and "密码" in resp.text


def test_login_and_flows(client):
    _req, resp = client.post("/api/login", json={"username": "admin", "password": "123456"})
    assert resp.status == 200
    cookie = resp.cookies.get("flow_session")
    assert cookie
    headers = {"Cookie": f"flow_session={cookie}"}
    _req, resp = client.get("/api/flows", headers=headers)
    assert resp.status == 200
    assert len(resp.json["items"]) >= 1
