import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from t3cc import __version__

if TYPE_CHECKING:
    from t3cc.remote.shell import RemoteShell

PROTOCOL = 1
FEATURES = frozenset({"import.live", "import.pull-requests", "import.worktree"})
REPOSITORY = "git+https://github.com/Arzaroth/t3cc"


def describe() -> dict:
    return {"t3cc": __version__, "protocol": PROTOCOL, "features": sorted(FEATURES)}


def install_command(version: str = __version__) -> str:
    return f"uv tool install --force {REPOSITORY}@v{version}"


@dataclass(frozen=True)
class Peer:
    version: str | None
    protocol: int = 0
    features: frozenset[str] = field(default_factory=frozenset)

    def missing(self, required: set[str] | frozenset[str]) -> list[str]:
        return sorted(required - self.features)

    def status(self) -> str:
        if self.version is None:
            return f"no t3cc, install it with: {install_command()}"
        if not self.protocol:
            return f"t3cc {self.version}, too old, upgrade it with: {install_command()}"
        return f"t3cc {self.version}, protocol {self.protocol}"


def probe(shell: "RemoteShell") -> Peer:
    result = shell.run("t3cc capabilities")
    if result.ok:
        data = json.loads(result.output)
        return Peer(data["t3cc"], data["protocol"], frozenset(data["features"]))
    version = shell.run("t3cc --version")
    return Peer(version.output.split()[-1] if version.ok else None)
