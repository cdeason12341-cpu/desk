"""Published-file encryption: PBKDF2-SHA256(APP_TOKEN, SALT, 200k) -> AES-256-GCM.
Envelope: {"v":1,"iv":b64,"ct":b64}. ct includes the 16-byte GCM tag (WebCrypto-compatible)."""
import base64, json, os
from functools import lru_cache
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

SALT = b"grok-bot-desk/v1/salt"
ITERATIONS = 200_000


@lru_cache(maxsize=4)
def derive_key(token: str) -> bytes:
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=SALT, iterations=ITERATIONS).derive(token.encode())


def encrypt_obj(obj, token: str) -> dict:
    iv = os.urandom(12)
    pt = json.dumps(obj, separators=(",", ":"), default=str).encode()
    ct = AESGCM(derive_key(token)).encrypt(iv, pt, None)
    return {"v": 1, "iv": base64.b64encode(iv).decode(), "ct": base64.b64encode(ct).decode()}


def decrypt_obj(env: dict, token: str):
    iv = base64.b64decode(env["iv"]); ct = base64.b64decode(env["ct"])
    return json.loads(AESGCM(derive_key(token)).decrypt(iv, ct, None))
