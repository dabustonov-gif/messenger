import base64
import os
import sys

import pytest

os.environ["MSG_NO_AUTOAPP"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from starlette.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
import refclient as rc  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    app = main.create_app(str(tmp_path / "t.db"))
    with TestClient(app) as c:
        yield c


def invite(c):
    import secrets, time
    code = secrets.token_urlsafe(8)
    main.st().db.execute("INSERT INTO invites VALUES(?,?,NULL)", (code, int(time.time())))
    return code


def signup(c, name):
    me = rc.Identity(name)
    r = c.post("/api/register", json=me.registration(invite(c)))
    assert r.status_code == 200, r.text
    nonce = c.post("/api/auth/challenge", json={"username": name}).json()["nonce"]
    r = c.post("/api/auth/verify", json={"username": name, "signature": rc.sign_auth(me, nonce)})
    assert r.status_code == 200, r.text
    me.token = r.json()["token"]
    me.h = {"Authorization": f"Bearer {me.token}"}
    return me


def peer_pk(c, me, name):
    return rc.verify_peer(c.get(f"/api/users/{name}", headers=me.h).json())


def ws_login(c, me):
    ws = c.websocket_connect("/ws")
    ws.__enter__()
    ws.send_json({"op": "auth", "token": me.token})
    assert ws.receive_json()["op"] == "ready"
    return ws


def test_registration_rules(client):
    me = rc.Identity("alice")
    assert client.post("/api/register", json=me.registration("nope")).status_code == 403
    reg = me.registration(invite(client))
    reg["box_sig"] = base64.b64encode(b"\0" * 64).decode()
    assert client.post("/api/register", json=reg).status_code == 400
    code = invite(client)
    assert client.post("/api/register", json=me.registration(code)).status_code == 200
    # invite is single-use, username unique
    assert client.post("/api/register", json=rc.Identity("bob").registration(code)).status_code == 403
    assert client.post("/api/register", json=rc.Identity("alice").registration(invite(client))).status_code == 409


def test_login_wrong_key_rejected(client):
    signup(client, "alice")
    impostor = rc.Identity("alice")
    nonce = client.post("/api/auth/challenge", json={"username": "alice"}).json()["nonce"]
    r = client.post("/api/auth/verify", json={"username": "alice", "signature": rc.sign_auth(impostor, nonce)})
    assert r.status_code == 401
    assert client.get("/api/users/alice").status_code == 401


def test_direct_message_online_and_offline(client):
    alice, bob = signup(client, "alice"), signup(client, "bob")
    wa = ws_login(client, alice)
    body, keys = rc.encrypt(alice, {"bob": peer_pk(client, alice, "bob")}, {"t": "text", "text": "привет", "g": None})
    wa.send_json({"op": "send", "cid": "c1", "body": body, "keys": keys})
    sent = wa.receive_json()
    assert sent["op"] == "sent" and sent["cid"] == "c1"
    # stored server-side as ciphertext only
    raw = main.st().db.execute("SELECT body FROM messages").fetchone()[0]
    assert "привет".encode() not in raw

    wb = ws_login(client, bob)  # bob was offline: gets queued message on connect
    m = wb.receive_json()
    assert m["op"] == "msg" and m["from"] == "alice"
    assert rc.decrypt(bob, peer_pk(client, bob, "alice"), m["body"], m["key"])["text"] == "привет"
    wb.send_json({"op": "ack", "ids": [m["id"]]})
    wb.send_json({"op": "ping"})
    assert wb.receive_json()["op"] == "pong"
    assert main.st().db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0  # deleted after ack

    # live delivery while online
    body, keys = rc.encrypt(alice, {"bob": peer_pk(client, alice, "bob")}, {"t": "text", "text": "ещё", "g": None})
    wa.send_json({"op": "send", "cid": "c2", "body": body, "keys": keys})
    assert wa.receive_json()["op"] == "sent"
    m = wb.receive_json()
    assert rc.decrypt(bob, peer_pk(client, bob, "alice"), m["body"], m["key"])["text"] == "ещё"
    wa.__exit__(None, None, None); wb.__exit__(None, None, None)


def test_tampering_detected(client):
    alice, bob = signup(client, "alice"), signup(client, "bob")
    body, keys = rc.encrypt(alice, {"bob": bob.box_pk}, {"t": "text", "text": "x"})
    other_body, _ = rc.encrypt(alice, {"bob": bob.box_pk}, {"t": "text", "text": "evil"})
    with pytest.raises(Exception):
        rc.decrypt(bob, alice.box_pk, other_body, keys["bob"])  # body swap breaks key AD
    eve = rc.Identity("eve")
    with pytest.raises(Exception):
        rc.decrypt(bob, eve.box_pk, body, keys["bob"])  # wrong claimed sender


def test_group_and_membership(client):
    a, b, c3 = signup(client, "alice"), signup(client, "bob"), signup(client, "carol")
    wa, wb, wc = ws_login(client, a), ws_login(client, b), ws_login(client, c3)
    g = client.post("/api/groups", headers=a.h, json={"name": "Семья", "members": ["bob", "carol"]}).json()
    assert set(g["members"]) == {"alice", "bob", "carol"}
    for ws in (wa, wb, wc):
        assert ws.receive_json()["op"] == "group"

    pks = {n: peer_pk(client, a, n) for n in ("bob", "carol")}
    body, keys = rc.encrypt(a, pks, {"t": "text", "text": "всем", "g": g["id"]})
    wa.send_json({"op": "send", "cid": "g1", "group_id": g["id"], "body": body, "keys": keys})
    assert wa.receive_json()["op"] == "sent"
    for me, ws in ((b, wb), (c3, wc)):
        m = ws.receive_json()
        assert m["group_id"] == g["id"]
        assert rc.decrypt(me, a.box_pk, m["body"], m["key"])["text"] == "всем"

    # remove carol; a stale client that still wraps for carol is refused
    r = client.delete(f"/api/groups/{g['id']}/members/carol", headers=a.h)
    assert set(r.json()["members"]) == {"alice", "bob"}
    for ws in (wa, wb, wc):
        assert ws.receive_json()["op"] == "group"
    wa.send_json({"op": "send", "cid": "g2", "group_id": g["id"], "body": body, "keys": keys})
    err = wa.receive_json()
    assert err["error"] == "members_changed" and "carol" not in err["group"]["members"]
    # non-member cannot post; non-owner cannot remove others
    wc.send_json({"op": "send", "cid": "x", "group_id": g["id"], "body": body, "keys": {"alice": keys["bob"], "bob": keys["bob"]}})
    assert wc.receive_json()["error"] == "not_member"
    assert client.delete(f"/api/groups/{g['id']}/members/alice", headers=b.h).status_code == 403
    for ws in (wa, wb, wc):
        ws.__exit__(None, None, None)


def test_encrypted_file(client):
    a, b = signup(client, "alice"), signup(client, "bob")
    data = os.urandom(300_000)
    blob, fkey = rc.encrypt_file(data)
    r = client.post("/api/blobs", headers=a.h, content=blob)
    assert r.status_code == 200
    blob_id = r.json()["blob_id"]
    body, keys = rc.encrypt(a, {"bob": b.box_pk}, {"t": "file", "blob": blob_id, "k": rc.b64(fkey),
                                                    "name": "photo.jpg", "mime": "image/jpeg", "size": len(data)})
    meta = rc.decrypt(b, a.box_pk, body, keys["bob"])
    got = client.get(f"/api/blobs/{meta['blob']}", headers=b.h).content
    assert rc.decrypt_file(got, base64.b64decode(meta["k"])) == data
    assert client.get(f"/api/blobs/{blob_id}").status_code == 401


def test_admin_invites(client):
    a, b = signup(client, "alice"), signup(client, "bob")  # first user is admin
    assert client.post("/api/invites", headers=a.h).status_code == 200
    assert client.post("/api/invites", headers=b.h).status_code == 403
    assert [u["username"] for u in client.get("/api/users?q=b", headers=a.h).json()["users"]] == ["bob"]


def test_map_api_without_gdal(client):
    a = signup(client, "alice")
    assert client.get("/api/maps/layers").status_code == 401
    r = client.get("/api/maps/layers", headers=a.h).json()
    assert set(r) >= {"satellite", "genshtab", "gdal"}
    if not r["gdal"]:   # в тестовой среде без GDAL карты честно отключаются, мессенджер работает
        assert client.post("/api/maps/jobs", headers=a.h, json={"bbox": [37, 55, 37.1, 55.1]}).status_code == 503
