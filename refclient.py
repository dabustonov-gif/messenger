"""Reference implementation of the client-side crypto (protocol v2: AES-256-GCM + X25519).

It mirrors android/.../crypto/E2E.kt byte for byte, so the test suite proves the
server relays exactly what the phone produces and that the format round-trips.
"""
import base64
import hashlib
import json
import os

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

BODY_AD = b"msgr-body-v2"
WRAP_CTX = b"msgr-wrap-v2"
FILE_AD = b"msgr-file-v2"


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def raw_pub(k) -> bytes:
    return k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


class Identity:
    def __init__(self, username: str):
        self.username = username
        self.sign = Ed25519PrivateKey.generate()
        self.box = X25519PrivateKey.generate()
        self.sign_pk = raw_pub(self.sign)
        self.box_pk = raw_pub(self.box)

    def registration(self, invite: str) -> dict:
        return {"username": self.username, "invite": invite,
                "sign_pk": b64(self.sign_pk), "box_pk": b64(self.box_pk),
                "box_sig": b64(self.sign.sign(b"msgr-boxkey-v1" + self.box_pk)),
                "reg_sig": b64(self.sign.sign(b"msgr-register-v1" + self.username.encode()))}


def verify_peer(user: dict) -> bytes:
    """Check the peer's X25519 key is signed by its Ed25519 key; return box_pk."""
    sign_pk, box_pk = base64.b64decode(user["sign_pk"]), base64.b64decode(user["box_pk"])
    Ed25519PublicKey.from_public_bytes(sign_pk).verify(base64.b64decode(user["box_sig"]), b"msgr-boxkey-v1" + box_pk)
    return box_pk


def _wrap_key(me: Identity, my_pk: bytes, their_pk: bytes, sender_pk: bytes, recipient_pk: bytes, salt: bytes) -> bytes:
    shared = me.box.exchange(X25519PublicKey.from_public_bytes(their_pk))
    return hashlib.sha256(WRAP_CTX + shared + salt + sender_pk + recipient_pk).digest()


def encrypt(me: Identity, recipients: dict[str, bytes], payload: dict) -> tuple[str, dict[str, str]]:
    msg_key, nonce = os.urandom(32), os.urandom(12)
    body = nonce + AESGCM(msg_key).encrypt(nonce, json.dumps(payload).encode(), BODY_AD)
    body_hash = hashlib.sha256(body).digest()
    keys = {}
    for name, pk in recipients.items():
        salt, n2 = os.urandom(16), os.urandom(12)
        wk = _wrap_key(me, me.box_pk, pk, me.box_pk, pk, salt)
        keys[name] = b64(salt + n2 + AESGCM(wk).encrypt(n2, msg_key, body_hash))
    return b64(body), keys


def decrypt(me: Identity, sender_box_pk: bytes, body_b64: str, key_b64: str) -> dict:
    body, wrapped = base64.b64decode(body_b64), base64.b64decode(key_b64)
    salt, n2, wct = wrapped[:16], wrapped[16:28], wrapped[28:]
    wk = _wrap_key(me, me.box_pk, sender_box_pk, sender_box_pk, me.box_pk, salt)
    msg_key = AESGCM(wk).decrypt(n2, wct, hashlib.sha256(body).digest())
    return json.loads(AESGCM(msg_key).decrypt(body[:12], body[12:], BODY_AD))


def encrypt_file(data: bytes) -> tuple[bytes, bytes]:
    key, nonce = os.urandom(32), os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, data, FILE_AD), key


def decrypt_file(blob: bytes, key: bytes) -> bytes:
    return AESGCM(key).decrypt(blob[:12], blob[12:], FILE_AD)


def sign_auth(me: Identity, nonce_b64: str) -> str:
    return b64(me.sign.sign(b"msgr-auth-v1" + base64.b64decode(nonce_b64)))
