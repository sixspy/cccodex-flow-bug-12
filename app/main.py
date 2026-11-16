from pathlib import Path

from itsdangerous import BadSignature, URLSafeSerializer
from sanic import Request, Sanic, json, redirect
from sanic.response import file

from app.config import Config
from app.db import get_db, get_flow, init_db, list_flows, run_extract, run_land
import app.deps  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
ser = URLSafeSerializer(Config.SECRET, salt="cc-flow-session")

app = Sanic("cc-flow-lab")
app.static("/static", str(STATIC), name="static")


def _user(request: Request) -> str | None:
    raw = request.cookies.get("flow_session")
    if not raw:
        return None
    try:
        data = ser.loads(raw)
        u = data.get("user") if isinstance(data, dict) else None
        return u if u == Config.ADMIN_USER else None
    except BadSignature:
        return None


@app.listener("before_server_start")
async def _boot(_app, _loop):
    init_db()


@app.get("/")
async def root(request: Request):
    return redirect("/app/" if _user(request) else "/login")


@app.get("/login")
async def login_page(request: Request):
    return await file(STATIC / "login.html")


@app.get("/app/", name="spa_root")
async def spa_root(request: Request):
    if not _user(request):
        return redirect("/login")
    return await file(STATIC / "index.html")


@app.get("/app/<path:path>", name="spa_path")
async def spa_path(request: Request, path: str = ""):
    if not _user(request):
        return redirect("/login")
    return await file(STATIC / "index.html")


@app.post("/api/login")
async def api_login(request: Request):
    body = request.json or {}
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return json({"ok": False, "error": "账号或密码错误"}, status=400)
    resp = json({"ok": True, "user": user})
    resp.add_cookie(
        "flow_session",
        ser.dumps({"user": user}),
        httponly=True,
        samesite="Lax",
        path="/",
    )
    return resp


@app.post("/api/logout")
async def api_logout(request: Request):
    resp = json({"ok": True})
    resp.delete_cookie("flow_session", path="/")
    return resp


@app.get("/api/me")
async def api_me(request: Request):
    user = _user(request)
    if not user:
        return json({"detail": "未登录"}, status=401)
    return json({"user": user, "lab": Config.LAB_NAME, "domain": "数据工程"})


@app.get("/api/flows")
async def api_flows(request: Request):
    if not _user(request):
        return json({"detail": "未登录"}, status=401)
    with get_db() as conn:
        return json({"items": list_flows(conn)})


@app.get("/api/flows/<int:flow_id>")
async def api_flow(request: Request, flow_id: int):
    if not _user(request):
        return json({"detail": "未登录"}, status=401)
    with get_db() as conn:
        item = get_flow(conn, flow_id)
    if not item:
        return json({"detail": "流不存在"}, status=404)
    return json(item)


@app.post("/api/flows/<int:flow_id>/extract")
async def api_extract(request: Request, flow_id: int):
    if not _user(request):
        return json({"detail": "未登录"}, status=401)
    with get_db() as conn:
        item = run_extract(conn, flow_id)
    if not item:
        return json({"detail": "流不存在"}, status=404)
    return json(item)


@app.post("/api/flows/<int:flow_id>/land")
async def api_land(request: Request, flow_id: int):
    if not _user(request):
        return json({"detail": "未登录"}, status=401)
    with get_db() as conn:
        item = run_land(conn, flow_id)
    if not item:
        return json({"detail": "流不存在"}, status=404)
    return json(item)

# PLANT_ROUTES

from app.deps import panel_payload, mutate_action

@app.get("/api/deps/panel")
async def api_deps_panel(request: Request):
    if not _user(request):
        return json({"detail": "未登录"}, status=401)
    with get_db() as conn:
        return json(panel_payload(conn, dict(request.args)))

@app.post("/api/deps/action")
async def api_deps_action(request: Request):
    if not _user(request):
        return json({"detail": "未登录"}, status=401)
    body = request.json or {}
    with get_db() as conn:
        out = mutate_action(conn, body)
    code = 200 if out.get("ok") else 409
    return json(out, status=code)

