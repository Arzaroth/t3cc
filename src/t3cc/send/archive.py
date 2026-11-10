import hashlib
import io
import json
import tarfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from t3cc.errors import T3ccError

FORMAT = 1
MANIFEST = "manifest.json"
TRANSCRIPT = "transcript.jsonl"
ARTIFACTS = "artifacts"
PAYLOAD = "payload"


@dataclass(frozen=True)
class Manifest:
    session_id: str
    source_cwd: str
    source_artifacts: str
    transcript_sha256: str
    git: dict | None = None
    target_root: str | None = None
    clone: str | None = None
    hooks: dict[str, str] = field(default_factory=dict)
    payload: list[str] = field(default_factory=list)
    format: int = FORMAT

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1)

    @classmethod
    def from_json(cls, text: str) -> "Manifest":
        data = json.loads(text)
        if data.get("format") != FORMAT:
            raise T3ccError(f"unsupported session archive format {data.get('format')}")
        return cls(**data)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


def build(transcript: Path, manifest: Manifest, payload: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz") as tar:
        _add(tar, MANIFEST, manifest.to_json().encode())
        _add(tar, TRANSCRIPT, transcript.read_bytes())
        artifacts = transcript.with_suffix("")
        if artifacts.is_dir():
            tar.add(artifacts, arcname=ARTIFACTS)
        for name, data in payload.items():
            _add(tar, f"{PAYLOAD}/{name}", data)
    return buffer.getvalue()


def extract(archive: Path, into: Path) -> Manifest:
    try:
        with tarfile.open(archive, mode="r:xz") as tar:
            tar.extractall(into, filter="data")
    except (tarfile.TarError, OSError, EOFError) as error:
        raise T3ccError(f"could not read the session archive {archive}: {error}") from None
    manifest = Manifest.from_json((into / MANIFEST).read_text())
    if sha256((into / TRANSCRIPT).read_bytes()) != manifest.transcript_sha256:
        raise T3ccError("the session archive's transcript does not match its manifest")
    return manifest
