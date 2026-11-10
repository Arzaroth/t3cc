import re
import shlex
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from t3cc.errors import T3ccError
from t3cc.remote.shell import RemoteShell

CHANNEL = "channel"
CODE = re.compile(r"\b(\d+-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+)\b")
PEER_TO_PEER = {
    "wyrm": ("wyrm send --hide-progress", "wyrm receive --hide-progress --accept-file -o {dest} {code}"),
    "wormhole": ("wormhole send --hide-progress", "wormhole receive --hide-progress --accept-file -o {dest} {code}"),
}
NAMES = (*PEER_TO_PEER, CHANNEL)

Which = Callable[[str], str | None]
Progress = Callable[[int, int], None]


@dataclass
class Transfer:
    used: str
    warnings: list[str] = field(default_factory=list)


def available(shell: RemoteShell, which: Which = shutil.which) -> list[str]:
    tools = [name for name in PEER_TO_PEER if which(name) and shell.run(f"command -v {name}").ok]
    return [*tools, CHANNEL]


def _peer_to_peer(name: str, local: Path, dest: str, shell: RemoteShell, timeout: float) -> None:
    send, receive = PEER_TO_PEER[name]
    proc = subprocess.Popen(
        [*shlex.split(send), str(local)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    try:
        code = None
        assert proc.stdout is not None
        for line in proc.stdout:
            if match := CODE.search(line):
                code = match.group(1)
                break
        if code is None:
            raise T3ccError(f"{name} send printed no code (exit {proc.wait()})")
        shell.check(receive.format(dest=shlex.quote(dest), code=code), timeout=timeout)
        if proc.wait(timeout=60) != 0:
            raise T3ccError(f"{name} send failed (exit {proc.returncode})")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def send(
    local: Path,
    dest: str,
    shell: RemoteShell,
    *,
    sha256: str,
    requested: str | None = None,
    which: Which = shutil.which,
    progress: Progress | None = None,
    timeout: float = 3600,
) -> Transfer:
    offered = available(shell, which)
    if requested and requested not in offered:
        raise T3ccError(f"transport {requested} is not available on both sides (available: {', '.join(offered)})")
    order = [requested] if requested else offered
    transfer = Transfer(used="")
    for name in order:
        try:
            if name == CHANNEL:
                shell.upload(local.read_bytes(), dest, progress)
            else:
                _peer_to_peer(name, local, dest, shell, timeout)
            if shell.sha256(dest) != sha256:
                raise T3ccError(f"the archive {name} delivered does not match")
        except T3ccError as error:
            if requested:
                raise
            transfer.warnings.append(f"{name}: {error}; trying the next transport")
            continue
        transfer.used = name
        return transfer
    raise T3ccError("; ".join(transfer.warnings))
