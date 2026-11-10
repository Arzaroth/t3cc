import base64
import contextlib
import re
import shlex
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from t3cc.errors import T3ccError
from t3cc.remote.rpc import Connection, RpcTimeout

LINE_LIMIT = 4000
WRITE_LIMIT = 60000


@dataclass(frozen=True)
class Result:
    output: str
    code: int

    @property
    def ok(self) -> bool:
        return self.code == 0


class RemoteShell:
    """A POSIX shell on the environment, run in one of its terminals."""

    def __init__(self, conn: Connection, cwd: str = "/"):
        self._conn = conn
        self._cwd = cwd
        terminal = f"t3cc-{uuid.uuid4().hex[:12]}"
        self._ids = {"threadId": terminal, "terminalId": terminal}
        self._attach: str | None = None
        self._buffer = ""

    def __enter__(self) -> "RemoteShell":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def open(self) -> None:
        self._conn.call("terminal.open", {**self._ids, "cwd": self._cwd, "cols": 200, "rows": 50})
        self._attach = self._conn.send("terminal.attach", self._ids, stream=True)
        self.write("exec env PS1= PS2= sh\r")
        self.write("stty -echo\r")
        self.run("true")

    def close(self) -> None:
        with contextlib.suppress(T3ccError):
            self._conn.call("terminal.close", self._ids, timeout=10)

    def write(self, data: str) -> None:
        for start in range(0, len(data), WRITE_LIMIT):
            self._conn.call("terminal.write", {**self._ids, "data": data[start : start + WRITE_LIMIT]})

    def _read(self, timeout: float) -> None:
        assert self._attach is not None
        message = self._conn.next_message(self._attach, timeout)
        if message.get("_tag") != "Chunk":
            raise T3ccError(f"{self._conn.label}: the remote terminal closed")
        for value in message.get("values", []):
            kind = value.get("type")
            if kind == "output":
                self._buffer += value["data"]
            elif kind in ("exited", "closed"):
                raise T3ccError(f"{self._conn.label}: the remote shell exited")

    def run(self, command: str, timeout: float | None = None) -> Result:
        marker = uuid.uuid4().hex
        scratch = f"/tmp/.t3cc-{marker}"
        line = (
            f"printf '\\n{marker}B\\n'; ( {command} ) >{scratch} 2>&1; r=$?; base64 <{scratch}; "
            f"rm -f {scratch}; printf '\\n{marker}E%s\\n' \"$r\"\r"
        )
        if len(line) > LINE_LIMIT:
            raise T3ccError("remote command too long")
        self.write(line)
        pattern = re.compile(marker + r"B\r?\n(.*?)\r?\n" + marker + r"E(\d+)", re.S)
        deadline = time.monotonic() + (self._conn.timeout if timeout is None else timeout)
        while not (match := pattern.search(self._buffer)):
            remaining = deadline - time.monotonic()
            try:
                self._read(max(remaining, 0.001))
            except RpcTimeout:
                raise T3ccError(f"{self._conn.label}: remote command timed out: {command}") from None
        self._buffer = self._buffer[match.end() :]
        output = base64.b64decode(re.sub(r"\s", "", match.group(1))).decode(errors="replace")
        return Result(output, int(match.group(2)))

    def check(self, command: str, timeout: float | None = None) -> str:
        result = self.run(command, timeout)
        if not result.ok:
            raise T3ccError(f"{self._conn.label}: `{command}` failed ({result.code}): {result.output.strip()}")
        return result.output

    def upload(self, data: bytes, dest: str, progress: Callable[[int, int], None] | None = None) -> None:
        marker = uuid.uuid4().hex
        self.write(f"base64 -d >{shlex.quote(dest)} <<'{marker}'; t3cc_upload=$?\r")
        encoded = base64.b64encode(data).decode()
        lines = [encoded[i : i + LINE_LIMIT] for i in range(0, len(encoded), LINE_LIMIT)]
        per_write = WRITE_LIMIT // (LINE_LIMIT + 1)
        for start in range(0, len(lines), per_write):
            self.write("\r".join(lines[start : start + per_write]) + "\r")
            if progress:
                progress(min(len(data), (start + per_write) * LINE_LIMIT * 3 // 4), len(data))
        self.write(f"{marker}\r")
        self.check("exit $t3cc_upload")

    def sha256(self, path: str) -> str:
        quoted = shlex.quote(path)
        output = self.check(f"sha256sum {quoted} 2>/dev/null || shasum -a 256 {quoted}")
        return output.split()[0]
