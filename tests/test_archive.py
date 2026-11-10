import io
import json
import tarfile

import pytest

from t3cc.errors import T3ccError
from t3cc.send import archive
from t3cc.send.archive import Manifest


def manifest_for(data: bytes, **extra) -> Manifest:
    return Manifest("sid", "/src", "/projects/x/sid", archive.sha256(data), **extra)


def test_round_trip(tmp_path):
    transcript = tmp_path / "sid.jsonl"
    transcript.write_bytes(b'{"cwd":"/src"}\n')
    (tmp_path / "sid" / "tool-results").mkdir(parents=True)
    (tmp_path / "sid" / "tool-results" / "r.txt").write_text("result")
    manifest = manifest_for(transcript.read_bytes(), payload=["diff"], hooks={"post-import": "true"})
    data = archive.build(transcript, manifest, {"diff": b"patch"})
    (tmp_path / "a.tar.xz").write_bytes(data)
    out = tmp_path / "out"
    assert archive.extract(tmp_path / "a.tar.xz", out) == manifest
    assert (out / "artifacts" / "tool-results" / "r.txt").read_text() == "result"
    assert (out / "payload" / "diff").read_bytes() == b"patch"


def test_round_trip_without_artifacts(tmp_path):
    transcript = tmp_path / "sid.jsonl"
    transcript.write_bytes(b"{}\n")
    (tmp_path / "a.tar.xz").write_bytes(archive.build(transcript, manifest_for(b"{}\n"), {}))
    archive.extract(tmp_path / "a.tar.xz", tmp_path / "out")
    assert not (tmp_path / "out" / "artifacts").exists()


def test_extract_rejects_a_tampered_transcript(tmp_path):
    transcript = tmp_path / "sid.jsonl"
    transcript.write_bytes(b"{}\n")
    (tmp_path / "a.tar.xz").write_bytes(archive.build(transcript, manifest_for(b"other"), {}))
    with pytest.raises(T3ccError, match="does not match"):
        archive.extract(tmp_path / "a.tar.xz", tmp_path / "out")


def test_extract_rejects_garbage_and_other_formats(tmp_path):
    (tmp_path / "bad.tar.xz").write_bytes(b"nope")
    with pytest.raises(T3ccError, match="could not read the session archive"):
        archive.extract(tmp_path / "bad.tar.xz", tmp_path / "out")
    with pytest.raises(T3ccError, match="unsupported session archive format 2"):
        Manifest.from_json(json.dumps({"format": 2}))


def test_extract_refuses_paths_outside(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz") as tar:
        info = tarfile.TarInfo("../escape")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    (tmp_path / "evil.tar.xz").write_bytes(buffer.getvalue())
    with pytest.raises(T3ccError, match="could not read"):
        archive.extract(tmp_path / "evil.tar.xz", tmp_path / "out")
