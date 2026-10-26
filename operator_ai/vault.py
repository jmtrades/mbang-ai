"""Encrypted per-tenant vault: AES-256-GCM, a fresh 96-bit nonce on every write, the tenant id
as associated data, and a per-tenant key file with mode 600. APIs only ever say whether a
field is set; values are never logged, printed or returned."""
from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import audit

_lock = threading.Lock()
MAGIC = b"OPV1"


class VaultError(Exception):
    pass


def _key_path(tenant) -> Path:
    return tenant.dir / "vault.key"


def _blob_path(tenant) -> Path:
    return tenant.dir / "vault.bin"


def _key(tenant) -> bytes:
    p = _key_path(tenant)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(AESGCM.generate_key(bit_length=256))
    os.chmod(p, 0o600)
    k = p.read_bytes()
    if len(k) != 32:
        raise VaultError("vault key file is corrupt")
    return k


def _read(tenant) -> dict:
    p = _blob_path(tenant)
    if not p.exists():
        return {}
    raw = p.read_bytes()
    if not raw.startswith(MAGIC):
        raise VaultError("not a vault file")
    nonce, ct = raw[4:16], raw[16:]
    try:
        pt = AESGCM(_key(tenant)).decrypt(nonce, ct, tenant.id.encode())
    except Exception:  # noqa: BLE001 - wrong key, wrong tenant (AD) or tampering
        raise VaultError("vault could not be decrypted for this tenant")
    return json.loads(pt)


def _write(tenant, data: dict) -> None:
    nonce = secrets.token_bytes(12)  # fresh nonce on every write
    ct = AESGCM(_key(tenant)).encrypt(nonce, json.dumps(data).encode(), tenant.id.encode())
    p = _blob_path(tenant)
    tmp = p.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(MAGIC + nonce + ct)
    os.replace(tmp, p)
    os.chmod(p, 0o600)


def put(tenant, fields: dict[str, str], actor: str = "system") -> list[str]:
    with _lock:
        data = _read(tenant)
        for k, v in fields.items():
            if v is None or v == "":
                data.pop(k, None)
            else:
                data[k] = str(v)
        _write(tenant, data)
    audit.log(tenant, actor, "vault.save", {"fields": sorted(fields)})  # field names only
    return sorted(fields)


def get(tenant, field: str) -> str | None:
    """Server-side use only (e.g. building an HTTP request). Never return this to a client."""
    return _read(tenant).get(field)


def status(tenant, fields: list[str] | None = None) -> dict[str, str]:
    data = _read(tenant)
    keys = fields if fields is not None else sorted(data)
    return {k: ("set" if data.get(k) else "not set") for k in keys}


def ensure_practice_secret(tenant, field: str) -> str:
    """A random per-tenant practice secret so webhooks are never unsigned before connection."""
    v = get(tenant, field)
    if not v:
        v = secrets.token_hex(32)
        put(tenant, {field: v}, actor="system")
    return v
