import base64
import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from t3cc.errors import T3ccError
from t3cc.paths import Paths

KEYRING_APPLICATION = "t3code"
BASIC_PASSWORD = b"peanuts"


@dataclass(frozen=True)
class Environment:
    environment_id: str
    label: str
    http_url: str
    ws_url: str
    token: str | None = field(default=None, repr=False)


def keyring_password() -> bytes:
    try:
        result = subprocess.run(
            ["secret-tool", "lookup", "application", KEYRING_APPLICATION], capture_output=True, check=False
        )
    except FileNotFoundError:
        raise T3ccError("secret-tool not found: install libsecret to read T3 Code's connections") from None
    password = result.stdout.strip()
    if result.returncode != 0 or not password:
        raise T3ccError("T3 Code's storage key is not in the keyring (secret-tool lookup application t3code)")
    return password


def decrypt(blob: bytes, keyring: Callable[[], bytes] = keyring_password) -> bytes:
    prefix, body = blob[:3], blob[3:]
    if prefix == b"v11":
        password = keyring()
    elif prefix == b"v10":
        password = BASIC_PASSWORD
    else:
        raise T3ccError(f"T3 Code's connections use an unknown encryption ({prefix!r})")
    key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1, 16)
    decryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    padded = decryptor.update(body) + decryptor.finalize()
    pad = padded[-1] if padded else 0
    if not 1 <= pad <= 16 or padded[-pad:] != bytes([pad]) * pad:
        raise T3ccError("could not decrypt T3 Code's connections: wrong storage key")
    return padded[:-pad]


def load(
    paths: Paths, keyring: Callable[[], bytes] = keyring_password, platform: str = sys.platform
) -> list[Environment]:
    if platform != "linux":
        raise T3ccError("reading T3 Code's connections is only supported on Linux for now")
    path = paths.t3_connection_catalog
    if not path.is_file():
        return []
    try:
        document = json.loads(path.read_text())
        catalog = json.loads(decrypt(base64.b64decode(document["encryptedCatalog"]), keyring))
    except (ValueError, KeyError, TypeError) as error:
        raise T3ccError(f"could not read {path}: {error}") from None
    disabled = set(catalog.get("disabledEnvironmentIds", []))
    tokens = {
        c["connectionId"]: c["credential"].get("token")
        for c in catalog.get("credentials", [])
        if c.get("credential", {}).get("_tag") == "BearerConnectionCredential"
    }
    return [
        Environment(
            environment_id=profile["environmentId"],
            label=profile["label"],
            http_url=profile["httpBaseUrl"],
            ws_url=profile["wsBaseUrl"],
            token=tokens.get(profile["connectionId"]),
        )
        for profile in catalog.get("profiles", [])
        if profile["environmentId"] not in disabled
    ]


def find(environments: list[Environment], name: str) -> Environment:
    lowered = name.lower()
    matches = [e for e in environments if e.label.lower() == lowered] or [
        e for e in environments if e.environment_id.startswith(name)
    ]
    if len(matches) == 1:
        return matches[0]
    known = ", ".join(sorted(e.label for e in environments)) or "none"
    reason = "is ambiguous" if matches else "is not a connected environment"
    raise T3ccError(f"{name!r} {reason} (known: {known})")
