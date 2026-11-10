import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from t3cc.errors import T3ccError
from t3cc.remote import catalog
from t3cc.remote.catalog import Environment


def encrypt(plain: bytes, password: bytes, prefix: bytes = b"v11") -> bytes:
    key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1, 16)
    pad = 16 - len(plain) % 16
    encryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    return prefix + encryptor.update(plain + bytes([pad]) * pad) + encryptor.finalize()


def profile(env_id, label, port):
    return {
        "_tag": "BearerConnectionProfile",
        "connectionId": f"bearer:{env_id}",
        "environmentId": env_id,
        "label": label,
        "httpBaseUrl": f"http://127.0.0.1:{port}/",
        "wsBaseUrl": f"ws://127.0.0.1:{port}/",
    }


CATALOG = {
    "schemaVersion": 1,
    "profiles": [profile("aaa-1", "sunkeeper", 3773), profile("bbb-2", "Laptop", 3774), profile("ccc-3", "off", 1)],
    "credentials": [
        {"connectionId": "bearer:aaa-1", "credential": {"_tag": "BearerConnectionCredential", "token": "secret-a"}},
        {"connectionId": "bearer:bbb-2", "credential": {"_tag": "DpopConnectionCredential"}},
    ],
    "disabledEnvironmentIds": ["ccc-3"],
}


def write_catalog(world, data=CATALOG, password=b"pw", prefix=b"v11"):
    blob = encrypt(json.dumps(data).encode(), password, prefix)
    world.paths.t3_connection_catalog.write_text(
        json.dumps({"version": 1, "encryptedCatalog": base64.b64encode(blob).decode()})
    )


def test_load_decrypts_the_keyring_catalog(world):
    write_catalog(world)
    envs = catalog.load(world.paths, keyring=lambda: b"pw", platform="linux")
    assert envs == [
        Environment("aaa-1", "sunkeeper", "http://127.0.0.1:3773/", "ws://127.0.0.1:3773/", "secret-a"),
        Environment("bbb-2", "Laptop", "http://127.0.0.1:3774/", "ws://127.0.0.1:3774/", None),
    ]
    assert "secret-a" not in repr(envs)


def test_load_basic_storage_needs_no_keyring(world):
    write_catalog(world, password=b"peanuts", prefix=b"v10")
    envs = catalog.load(world.paths, keyring=lambda: pytest.fail("no keyring"), platform="linux")
    assert [e.label for e in envs] == ["sunkeeper", "Laptop"]


def test_load_without_a_catalog(world):
    assert catalog.load(world.paths, platform="linux") == []


def test_load_is_linux_only(world):
    with pytest.raises(T3ccError, match="only supported on Linux"):
        catalog.load(world.paths, platform="darwin")


@pytest.mark.parametrize(
    ("document", "match"),
    [
        ("not json", "could not read"),
        ('{"version": 1}', "could not read"),
        (json.dumps({"encryptedCatalog": base64.b64encode(b"v99xxxx").decode()}), "unknown encryption"),
    ],
)
def test_load_rejects_bad_documents(world, document, match):
    world.paths.t3_connection_catalog.write_text(document)
    with pytest.raises(T3ccError, match=match):
        catalog.load(world.paths, keyring=lambda: b"pw", platform="linux")


def test_decrypt_with_the_wrong_key(world):
    blob = encrypt(b"x" * 16, b"right")
    with pytest.raises(T3ccError, match="wrong storage key"):
        catalog.decrypt(blob, keyring=lambda: b"wrong")
    with pytest.raises(T3ccError, match="wrong storage key"):
        catalog.decrypt(b"v11", keyring=lambda: b"pw")


def fake_secret_tool(tmp_path, monkeypatch, script):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool = bin_dir / "secret-tool"
    tool.write_text(f"#!/bin/sh\n{script}\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))


def test_keyring_password(tmp_path, monkeypatch):
    fake_secret_tool(tmp_path, monkeypatch, '[ "$*" = "lookup application t3code" ] && printf "s3cret\\n"')
    assert catalog.keyring_password() == b"s3cret"


def test_keyring_password_missing_entry(tmp_path, monkeypatch):
    fake_secret_tool(tmp_path, monkeypatch, "exit 1")
    with pytest.raises(T3ccError, match="not in the keyring"):
        catalog.keyring_password()


def test_keyring_password_without_secret_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(T3ccError, match="install libsecret"):
        catalog.keyring_password()


ENVS = [
    Environment("aaa-1", "sunkeeper", "h", "w"),
    Environment("aab-2", "Sunkeeper-2", "h", "w"),
]


def test_find():
    assert catalog.find(ENVS, "SUNKEEPER").environment_id == "aaa-1"
    assert catalog.find(ENVS, "aab").label == "Sunkeeper-2"
    with pytest.raises(T3ccError, match="ambiguous"):
        catalog.find(ENVS, "aa")
    with pytest.raises(T3ccError, match=r"not a connected environment \(known: Sunkeeper-2, sunkeeper\)"):
        catalog.find(ENVS, "mac")
    with pytest.raises(T3ccError, match="known: none"):
        catalog.find([], "mac")
