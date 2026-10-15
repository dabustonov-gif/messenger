"""
Secure Messenger — relay server.

The server never sees plaintext or private keys. It:
  * registers users (invite-only) with their Ed25519 signing key and a signed X25519 key,
  * authenticates devices by Ed25519 challenge-response,
  * stores encrypted envelopes until the recipient acknowledges them (store-and-forward),
  * stores encrypted file blobs,
  * keeps group membership (group names and member lists are NOT secret from the server).
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import contextlib
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from collections import defaultdict

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from gis import engine as gis_engine
from gis import sources as gis_sources

DATA_DIR = os.environ.get("MSG_DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
DB_PATH = os.path.join(DATA_DIR, "messenger.db")
BLOB_DIR = os.path.join(DATA_DIR, "blobs")
LAYER_DIR = os.environ.get("MSG_LAYER_DIR", os.path.join(DATA_DIR, "layers"))
MAPS_OUT = os.path.join(DATA_DIR, "maps")
GIS_WORKERS = int(os.environ.get("GIS_WORKERS", "2"))

TOKEN_TTL = 30 * 24 * 3600          # session token lifetime
CHALLENGE_TTL = 60                  # seconds to answer a login challenge
RETENTION = int(os.environ.get("MSG_RETENTION_DAYS", "30")) * 24 * 3600  # undelivered msgs / blobs
MAX_BODY = 96 * 1024                # encrypted message body (bytes, after base64 decode)
MAX_BLOB = int(os.environ.get("MSG_MAX_FILE_MB", "50")) * 1024 * 1024
MAX_GROUP = 256

USERNAME_RE = re.compile(r"^[a-z0-9_]{3,32}$")
BOXKEY_CTX = b"msgr-boxkey-v1"
REGISTER_CTX = b"msgr-register-v1"
AUTH_CTX = b"msgr-auth-v1"


# ---------------------------------------------------------------- utilities
def b64d(s: str, max_len: int | None = None) -> bytes:
    try:
        raw = base64.b64decode(s, validate=True)
    except Exception:
        raise ApiError(400, "bad_base64")
    if max_len is not None and len(raw) > max_len:
        raise ApiError(413, "too_large")
    return raw


def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode()


def now() -> int:
    return int(time.time())


def verify_sig(pk: bytes, sig: bytes, msg: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(pk).verify(sig, msg)
        return True
    except (InvalidSignature, ValueError):
        return False


class ApiError(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code


# ---------------------------------------------------------------- database
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
  sign_pk BLOB NOT NULL, box_pk BLOB NOT NULL, box_sig BLOB NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS invites(
  code TEXT PRIMARY KEY, created_at INTEGER NOT NULL, used_by INTEGER REFERENCES users(id));
CREATE TABLE IF NOT EXISTS tokens(
  token_hash BLOB PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS groups(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, owner_id INTEGER NOT NULL REFERENCES users(id),
  created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS group_members(
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  PRIMARY KEY(group_id, user_id));
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY, sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  group_id INTEGER REFERENCES groups(id) ON DELETE CASCADE,
  body BLOB NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS deliveries(
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  recipient_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  wrapped_key BLOB NOT NULL,
  PRIMARY KEY(message_id, recipient_id));
CREATE INDEX IF NOT EXISTS idx_deliv_recipient ON deliveries(recipient_id);
CREATE TABLE IF NOT EXISTS blobs(
  id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  size INTEGER NOT NULL, created_at INTEGER NOT NULL);
"""


def open_db(path: str = None) -> sqlite3.Connection:
    path = path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


class State:
    def __init__(self, db: sqlite3.Connection, blob_dir: str):
        self.db = db
        self.blob_dir = blob_dir
        os.makedirs(blob_dir, exist_ok=True)
        self.challenges: dict[str, tuple[bytes, int]] = {}
        self.sockets: dict[int, set[WebSocket]] = defaultdict(set)
        self.rate: dict[str, list[float]] = defaultdict(list)


STATE: State | None = None


def st() -> State:
    assert STATE is not None
    return STATE


# ---------------------------------------------------------------- helpers
def rate_limit(key: str, limit: int, window: float = 60.0):
    bucket = st().rate[key]
    t = time.monotonic()
    bucket[:] = [x for x in bucket if t - x < window]
    if len(bucket) >= limit:
        raise ApiError(429, "rate_limited")
    bucket.append(t)


def client_ip(req) -> str:
    return req.client.host if req.client else "?"


def user_by_token(token: str | None):
    if not token:
        return None
    h = hashlib.sha256(token.encode()).digest()
    return st().db.execute(
        "SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token_hash=? AND t.expires>?",
        (h, now()),
    ).fetchone()


def require_user(req: Request):
    auth = req.headers.get("authorization", "")
    user = user_by_token(auth[7:] if auth.lower().startswith("bearer ") else None)
    if not user:
        raise ApiError(401, "unauthorized")
    return user


def user_json(u) -> dict:
    return {"username": u["username"], "sign_pk": b64e(u["sign_pk"]),
            "box_pk": b64e(u["box_pk"]), "box_sig": b64e(u["box_sig"])}


def group_json(gid: int) -> dict | None:
    db = st().db
    g = db.execute("SELECT g.*, u.username owner FROM groups g JOIN users u ON u.id=g.owner_id WHERE g.id=?",
                   (gid,)).fetchone()
    if not g:
        return None
    members = [r["username"] for r in db.execute(
        "SELECT u.username FROM group_members m JOIN users u ON u.id=m.user_id WHERE m.group_id=? ORDER BY u.username",
        (gid,))]
    return {"id": g["id"], "name": g["name"], "owner": g["owner"], "members": members}


def is_member(gid: int, uid: int) -> bool:
    return st().db.execute("SELECT 1 FROM group_members WHERE group_id=? AND user_id=?", (gid, uid)).fetchone() is not None


async def push(uid: int, payload: dict):
    dead = []
    for ws in list(st().sockets.get(uid, ())):
        try:
            await ws.send_text(json.dumps(payload))
        except Exception:
            dead.append(ws)
    for ws in dead:
        st().sockets[uid].discard(ws)


async def notify_group(gid: int, extra_uids: tuple[int, ...] = ()):
    info = group_json(gid)
    uids = {r["user_id"] for r in st().db.execute("SELECT user_id FROM group_members WHERE group_id=?", (gid,))}
    uids.update(extra_uids)
    for uid in uids:
        await push(uid, {"op": "group", "group": info, "group_id": gid})


async def json_body(req: Request) -> dict:
    try:
        data = await req.json()
    except Exception:
        raise ApiError(400, "bad_json")
    if not isinstance(data, dict):
        raise ApiError(400, "bad_json")
    return data


def api(fn):
    async def wrapper(req: Request):
        try:
            return await fn(req)
        except ApiError as e:
            return JSONResponse({"error": e.code}, status_code=e.status)
    return wrapper


# ---------------------------------------------------------------- REST: accounts
@api
async def register(req: Request):
    rate_limit("reg:" + client_ip(req), 10)
    d = await json_body(req)
    username = str(d.get("username", "")).lower()
    if not USERNAME_RE.match(username):
        raise ApiError(400, "bad_username")
    sign_pk, box_pk = b64d(d.get("sign_pk", ""), 32), b64d(d.get("box_pk", ""), 32)
    box_sig, reg_sig = b64d(d.get("box_sig", ""), 64), b64d(d.get("reg_sig", ""), 64)
    if len(sign_pk) != 32 or len(box_pk) != 32:
        raise ApiError(400, "bad_key")
    if not verify_sig(sign_pk, box_sig, BOXKEY_CTX + box_pk):
        raise ApiError(400, "bad_box_sig")
    if not verify_sig(sign_pk, reg_sig, REGISTER_CTX + username.encode()):
        raise ApiError(400, "bad_reg_sig")
    db = st().db
    db.execute("BEGIN IMMEDIATE")
    try:
        inv = db.execute("SELECT * FROM invites WHERE code=? AND used_by IS NULL", (str(d.get("invite", "")),)).fetchone()
        if not inv:
            raise ApiError(403, "bad_invite")
        if db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise ApiError(409, "username_taken")
        first = db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        cur = db.execute("INSERT INTO users(username,sign_pk,box_pk,box_sig,is_admin,created_at) VALUES(?,?,?,?,?,?)",
                         (username, sign_pk, box_pk, box_sig, int(first), now()))
        db.execute("UPDATE invites SET used_by=? WHERE code=?", (cur.lastrowid, inv["code"]))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return JSONResponse({"ok": True, "username": username, "admin": first})


@api
async def challenge(req: Request):
    rate_limit("auth:" + client_ip(req), 30)
    d = await json_body(req)
    username = str(d.get("username", "")).lower()
    nonce = secrets.token_bytes(32)
    st().challenges[username] = (nonce, now() + CHALLENGE_TTL)
    return JSONResponse({"nonce": b64e(nonce)})


@api
async def verify(req: Request):
    rate_limit("auth:" + client_ip(req), 30)
    d = await json_body(req)
    username = str(d.get("username", "")).lower()
    ch = st().challenges.pop(username, None)
    if not ch or ch[1] < now():
        raise ApiError(401, "no_challenge")
    u = st().db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not u or not verify_sig(u["sign_pk"], b64d(d.get("signature", ""), 64), AUTH_CTX + ch[0]):
        raise ApiError(401, "bad_signature")
    token = secrets.token_urlsafe(32)
    st().db.execute("INSERT INTO tokens VALUES(?,?,?)",
                    (hashlib.sha256(token.encode()).digest(), u["id"], now() + TOKEN_TTL))
    return JSONResponse({"token": token, "expires_in": TOKEN_TTL})


@api
async def get_user(req: Request):
    require_user(req)
    u = st().db.execute("SELECT * FROM users WHERE username=?",
                        (req.path_params["username"].lower(),)).fetchone()
    if not u:
        raise ApiError(404, "not_found")
    return JSONResponse(user_json(u))


@api
async def search_users(req: Request):
    require_user(req)
    q = req.query_params.get("q", "").lower()
    if not re.match(r"^[a-z0-9_]{1,32}$", q):
        return JSONResponse({"users": []})
    rows = st().db.execute("SELECT * FROM users WHERE username LIKE ? ORDER BY username LIMIT 20", (q + "%",))
    return JSONResponse({"users": [user_json(u) for u in rows]})


@api
async def create_invite(req: Request):
    u = require_user(req)
    if not u["is_admin"]:
        raise ApiError(403, "admin_only")
    code = secrets.token_urlsafe(12)
    st().db.execute("INSERT INTO invites VALUES(?,?,NULL)", (code, now()))
    return JSONResponse({"invite": code})


# ---------------------------------------------------------------- REST: groups
@api
async def groups(req: Request):
    u = require_user(req)
    db = st().db
    if req.method == "GET":
        ids = [r[0] for r in db.execute("SELECT group_id FROM group_members WHERE user_id=?", (u["id"],))]
        return JSONResponse({"groups": [group_json(i) for i in ids]})
    d = await json_body(req)
    name = str(d.get("name", "")).strip()[:64]
    if not name:
        raise ApiError(400, "bad_name")
    names = {str(x).lower() for x in d.get("members", [])} | {u["username"]}
    if len(names) > MAX_GROUP:
        raise ApiError(400, "too_many_members")
    rows = db.execute(f"SELECT id FROM users WHERE username IN ({','.join('?' * len(names))})", tuple(names)).fetchall()
    if len(rows) != len(names):
        raise ApiError(404, "unknown_member")
    cur = db.execute("INSERT INTO groups(name,owner_id,created_at) VALUES(?,?,?)", (name, u["id"], now()))
    gid = cur.lastrowid
    db.executemany("INSERT INTO group_members VALUES(?,?)", [(gid, r["id"]) for r in rows])
    await notify_group(gid)
    return JSONResponse(group_json(gid))


@api
async def add_member(req: Request):
    u = require_user(req)
    gid = int(req.path_params["gid"])
    info = group_json(gid)
    if not info or info["owner"] != u["username"]:
        raise ApiError(403, "owner_only")
    if len(info["members"]) >= MAX_GROUP:
        raise ApiError(400, "too_many_members")
    d = await json_body(req)
    target = st().db.execute("SELECT id FROM users WHERE username=?", (str(d.get("username", "")).lower(),)).fetchone()
    if not target:
        raise ApiError(404, "not_found")
    st().db.execute("INSERT OR IGNORE INTO group_members VALUES(?,?)", (gid, target["id"]))
    await notify_group(gid)
    return JSONResponse(group_json(gid))


@api
async def remove_member(req: Request):
    u = require_user(req)
    gid = int(req.path_params["gid"])
    info = group_json(gid)
    username = req.path_params["username"].lower()
    if not info or u["username"] not in info["members"]:
        raise ApiError(404, "not_found")
    if info["owner"] != u["username"] and username != u["username"]:
        raise ApiError(403, "owner_only")
    target = st().db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if not target:
        raise ApiError(404, "not_found")
    st().db.execute("DELETE FROM group_members WHERE group_id=? AND user_id=?", (gid, target["id"]))
    await notify_group(gid, extra_uids=(target["id"],))
    return JSONResponse(group_json(gid))


# ---------------------------------------------------------------- REST: encrypted files
@api
async def upload_blob(req: Request):
    u = require_user(req)
    rate_limit(f"blob:{u['id']}", 60)
    size = int(req.headers.get("content-length") or 0)
    if size > MAX_BLOB:
        raise ApiError(413, "too_large")
    blob_id = secrets.token_hex(16)
    path = os.path.join(st().blob_dir, blob_id)
    total = 0
    with open(path, "wb") as f:
        async for chunk in req.stream():
            total += len(chunk)
            if total > MAX_BLOB:
                f.close()
                os.remove(path)
                raise ApiError(413, "too_large")
            f.write(chunk)
    st().db.execute("INSERT INTO blobs VALUES(?,?,?,?)", (blob_id, u["id"], total, now()))
    return JSONResponse({"blob_id": blob_id, "size": total})


@api
async def get_blob(req: Request):
    require_user(req)
    blob_id = req.path_params["blob_id"]
    if not re.match(r"^[0-9a-f]{32}$", blob_id):
        raise ApiError(400, "bad_id")
    if not st().db.execute("SELECT 1 FROM blobs WHERE id=?", (blob_id,)).fetchone():
        raise ApiError(404, "not_found")
    with open(os.path.join(st().blob_dir, blob_id), "rb") as f:
        data = f.read()
    return Response(data, media_type="application/octet-stream")


# ---------------------------------------------------------------- REST: карты (ГИС)
#
# Тяжёлые расчёты (GDAL warp, сетка, нарезка) идут в отдельных процессах, чтобы не блокировать
# доставку сообщений. Готовый файл клиент скачивает по HTTPS и сам пересылает в чат как обычное
# E2E-вложение — сервер к этому вложению ключа не имеет.
#
# Важно: сервер видит, какую область запросил пользователь (это метаданные заказа карты).

def _map_job(req_json: dict, layer_dir: str, out_path: str) -> dict:
    """Выполняется в дочернем процессе."""
    req = gis_engine.MapRequest.from_json(req_json)
    return gis_engine.build(req, gis_sources.gdal_provider(layer_dir), out_path)


class MapJobs:
    def __init__(self):
        self.pool = None
        self.jobs: dict[str, dict] = {}

    def submit(self, owner: int, req_json: dict) -> str:
        if self.pool is None:
            self.pool = concurrent.futures.ProcessPoolExecutor(max_workers=GIS_WORKERS)
        if sum(1 for j in self.jobs.values() if j["owner"] == owner and j["status"] == "running") >= 2:
            raise ApiError(429, "too_many_map_jobs")
        job_id = secrets.token_hex(8)
        ext = "kmz" if req_json.get("format", "kmz") == "kmz" else "pdf"
        os.makedirs(MAPS_OUT, exist_ok=True)
        path = os.path.join(MAPS_OUT, f"{job_id}.{ext}")
        job = {"owner": owner, "status": "running", "path": path, "created": now(),
               "filename": f"{(str(req_json.get('name') or 'map'))[:40]}.{ext}"}
        self.jobs[job_id] = job
        fut = self.pool.submit(_map_job, req_json, LAYER_DIR, path)

        def done(f):
            try:
                job.update(status="done", info=f.result(), size=os.path.getsize(path))
            except gis_engine.MapError as e:
                job.update(status="error", error=str(e))
            except Exception as e:  # noqa: BLE001
                job.update(status="error", error=f"Ошибка сборки карты: {type(e).__name__}: {e}")
        fut.add_done_callback(done)
        return job_id

    def get(self, job_id: str, owner: int) -> dict:
        job = self.jobs.get(job_id)
        if not job or job["owner"] != owner:
            raise ApiError(404, "not_found")
        return job

    def cleanup(self, max_age=24 * 3600):
        for jid, j in list(self.jobs.items()):
            if j["created"] < now() - max_age:
                with contextlib.suppress(FileNotFoundError):
                    os.remove(j["path"])
                self.jobs.pop(jid, None)


MAP_JOBS = MapJobs()


def _require_gis():
    if not gis_sources.HAVE_GDAL:
        raise ApiError(503, "gis_unavailable")


@api
async def map_layers(req: Request):
    require_user(req)
    return JSONResponse(gis_sources.availability(LAYER_DIR))


@api
async def map_create(req: Request):
    u = require_user(req)
    _require_gis()
    d = await json_body(req)
    try:
        gis_engine.MapRequest.from_json(d)          # проверяем сразу, чтобы ошибка пришла мгновенно
    except gis_engine.MapError as e:
        return JSONResponse({"error": "bad_map_request", "message": str(e)}, status_code=400)
    return JSONResponse({"job_id": MAP_JOBS.submit(u["id"], d)})


@api
async def map_status(req: Request):
    u = require_user(req)
    j = MAP_JOBS.get(req.path_params["job_id"], u["id"])
    return JSONResponse({k: j.get(k) for k in ("status", "error", "info", "size", "filename")})


@api
async def map_file(req: Request):
    u = require_user(req)
    j = MAP_JOBS.get(req.path_params["job_id"], u["id"])
    if j["status"] != "done":
        raise ApiError(409, "not_ready")
    with open(j["path"], "rb") as f:
        data = f.read()
    mime = "application/vnd.google-earth.kmz" if j["path"].endswith(".kmz") else "application/pdf"
    return Response(data, media_type=mime)


@api
async def map_tile(req: Request):
    """Тайлы предпросмотра для окна карты в приложении (кешируются на диске)."""
    require_user(req)
    _require_gis()
    layer = req.path_params["layer"]
    z, x, y = (int(req.path_params[k]) for k in ("z", "x", "y"))
    if layer not in ("satellite", "genshtab", "mix") or not (0 <= z <= 19) or not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise ApiError(400, "bad_tile")
    cache = os.path.join(DATA_DIR, "tile_render", layer, str(z), str(x), f"{y}.png")
    if os.path.exists(cache):
        with open(cache, "rb") as f:
            return Response(f.read(), media_type="image/png", headers={"Cache-Control": "max-age=86400"})
    png = await asyncio.get_running_loop().run_in_executor(None, gis_sources.render_tile, LAYER_DIR, layer, z, x, y)
    if png is None:
        return Response(status_code=204)
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    with open(cache, "wb") as f:
        f.write(png)
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


async def health(_req):
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------- WebSocket
def deliver_payload(row) -> dict:
    return {"op": "msg", "id": row["id"], "from": row["sender"], "group_id": row["group_id"],
            "body": b64e(row["body"]), "key": b64e(row["wrapped_key"]), "ts": row["created_at"]}


PENDING_SQL = """SELECT m.id, m.group_id, m.body, m.created_at, d.wrapped_key, u.username sender
FROM deliveries d JOIN messages m ON m.id=d.message_id JOIN users u ON u.id=m.sender_id
WHERE d.recipient_id=? ORDER BY m.id"""


async def handle_send(user, msg: dict) -> dict:
    db = st().db
    body = b64d(msg.get("body", ""), MAX_BODY)
    keys = msg.get("keys")
    if not isinstance(keys, dict) or not keys:
        raise ApiError(400, "no_recipients")
    names = {str(k).lower() for k in keys}
    gid = msg.get("group_id")
    if gid is not None:
        gid = int(gid)
        info = group_json(gid)
        if not info or user["username"] not in info["members"]:
            raise ApiError(403, "not_member")
        expected = set(info["members"]) - {user["username"]}
        if names != expected:
            # client has a stale member list; tell it to refresh instead of leaking to ex-members
            return {"op": "error", "cid": msg.get("cid"), "error": "members_changed", "group": info}
    elif len(names) != 1 or user["username"] in names:
        raise ApiError(400, "direct_needs_one_peer")
    rows = db.execute(f"SELECT id, username FROM users WHERE username IN ({','.join('?' * len(names))})",
                      tuple(names)).fetchall()
    if len(rows) != len(names):
        raise ApiError(404, "unknown_recipient")
    wrapped = {r["username"]: b64d(keys[r["username"]], 256) for r in rows}
    ts = now()
    db.execute("BEGIN")
    cur = db.execute("INSERT INTO messages(sender_id,group_id,body,created_at) VALUES(?,?,?,?)",
                     (user["id"], gid, body, ts))
    mid = cur.lastrowid
    db.executemany("INSERT INTO deliveries VALUES(?,?,?)", [(mid, r["id"], wrapped[r["username"]]) for r in rows])
    db.execute("COMMIT")
    for r in rows:
        await push(r["id"], {"op": "msg", "id": mid, "from": user["username"], "group_id": gid,
                             "body": b64e(body), "key": b64e(wrapped[r["username"]]), "ts": ts})
    return {"op": "sent", "cid": msg.get("cid"), "id": mid, "ts": ts}


def handle_ack(user, ids):
    db = st().db
    ids = [int(i) for i in ids][:500]
    if not ids:
        return
    db.execute(f"DELETE FROM deliveries WHERE recipient_id=? AND message_id IN ({','.join('?' * len(ids))})",
               (user["id"], *ids))
    db.execute(f"DELETE FROM messages WHERE id IN ({','.join('?' * len(ids))}) "
               "AND NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.message_id=messages.id)", ids)


async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    try:
        first = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=15))
        user = user_by_token(first.get("token")) if first.get("op") == "auth" else None
    except Exception:
        user = None
    if not user:
        await ws.send_text(json.dumps({"op": "error", "error": "unauthorized"}))
        await ws.close(code=4401)
        return
    uid = user["id"]
    st().sockets[uid].add(ws)
    try:
        await ws.send_text(json.dumps({"op": "ready", "username": user["username"]}))
        for row in st().db.execute(PENDING_SQL, (uid,)).fetchall():
            await ws.send_text(json.dumps(deliver_payload(row)))
        while True:
            msg = json.loads(await ws.receive_text())
            op = msg.get("op")
            try:
                if op == "send":
                    rate_limit(f"send:{uid}", 120)
                    await ws.send_text(json.dumps(await handle_send(user, msg)))
                elif op == "ack":
                    handle_ack(user, msg.get("ids", []))
                elif op == "ping":
                    await ws.send_text('{"op":"pong"}')
            except ApiError as e:
                await ws.send_text(json.dumps({"op": "error", "cid": msg.get("cid"), "error": e.code}))
    except (WebSocketDisconnect, RuntimeError, json.JSONDecodeError):
        pass
    finally:
        st().sockets[uid].discard(ws)


# ---------------------------------------------------------------- maintenance
def cleanup_once():
    db = st().db
    cutoff = now() - RETENTION
    db.execute("DELETE FROM messages WHERE created_at<?", (cutoff,))
    db.execute("DELETE FROM tokens WHERE expires<?", (now(),))
    for r in db.execute("SELECT id FROM blobs WHERE created_at<?", (cutoff,)).fetchall():
        with contextlib.suppress(FileNotFoundError):
            os.remove(os.path.join(st().blob_dir, r["id"]))
        db.execute("DELETE FROM blobs WHERE id=?", (r["id"],))
    MAP_JOBS.cleanup()
    for k in [k for k, (_, exp) in st().challenges.items() if exp < now()]:
        st().challenges.pop(k, None)


async def cleanup_loop():
    while True:
        await asyncio.sleep(3600)
        with contextlib.suppress(Exception):
            cleanup_once()


@contextlib.asynccontextmanager
async def lifespan(_app):
    global STATE
    if STATE is None:
        STATE = State(open_db(), BLOB_DIR)
    task = asyncio.create_task(cleanup_loop())
    yield
    task.cancel()


def create_app(db_path: str | None = None, blob_dir: str | None = None) -> Starlette:
    global STATE
    if db_path:
        STATE = State(open_db(db_path), blob_dir or os.path.join(os.path.dirname(db_path), "blobs"))
    return Starlette(routes=[
        Route("/health", health),
        Route("/api/register", register, methods=["POST"]),
        Route("/api/auth/challenge", challenge, methods=["POST"]),
        Route("/api/auth/verify", verify, methods=["POST"]),
        Route("/api/users", search_users),
        Route("/api/users/{username}", get_user),
        Route("/api/invites", create_invite, methods=["POST"]),
        Route("/api/groups", groups, methods=["GET", "POST"]),
        Route("/api/groups/{gid:int}/members", add_member, methods=["POST"]),
        Route("/api/groups/{gid:int}/members/{username}", remove_member, methods=["DELETE"]),
        Route("/api/blobs", upload_blob, methods=["POST"]),
        Route("/api/blobs/{blob_id}", get_blob),
        Route("/api/maps/layers", map_layers),
        Route("/api/maps/jobs", map_create, methods=["POST"]),
        Route("/api/maps/jobs/{job_id}", map_status),
        Route("/api/maps/jobs/{job_id}/file", map_file),
        Route("/api/maps/tiles/{layer}/{z:int}/{x:int}/{y:int}.png", map_tile),
        WebSocketRoute("/ws", ws_endpoint),
    ], lifespan=lifespan)


app = create_app() if os.environ.get("MSG_NO_AUTOAPP") != "1" else None
