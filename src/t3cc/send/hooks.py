import os
import subprocess
import tomllib
from collections.abc import Mapping
from pathlib import Path

from t3cc.errors import T3ccError

NAMES = ("pre-send", "post-checkout", "post-import", "post-send")
REMOTE = ("post-checkout", "post-import")


def config_path(env: Mapping[str, str] = os.environ) -> Path:
    base = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else Path.home() / ".config"
    return base / "t3cc" / "config.toml"


def _section(data: dict, *keys: str) -> dict:
    for depth, key in enumerate(keys, 1):
        data = data.get(key, {})
        if not isinstance(data, dict):
            raise T3ccError(f"t3cc config: {'.'.join(keys[:depth])} must be a table")
    return data


def load(path: Path, env_label: str, origin: str | None) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as error:
        raise T3ccError(f"t3cc config {path}: {error}") from None
    merged: dict[str, str] = {}
    layers = [_section(data, "hooks"), _section(data, "env", env_label, "hooks")]
    if origin:
        repos = _section(data, "repo")
        key = next((name for name in repos if name.lower() == origin.lower()), None)
        if key is not None:
            layers.append(_section(repos, key, "hooks"))
    for layer in layers:
        for name, command in layer.items():
            if name not in NAMES:
                raise T3ccError(f"t3cc config: unknown hook {name!r} (known: {', '.join(NAMES)})")
            if not isinstance(command, str):
                raise T3ccError(f"t3cc config: hook {name} must be a command string")
            merged[name] = command
    return merged


def run(command: str | None, cwd: str, variables: Mapping[str, str]) -> str | None:
    if not command:
        return None
    result = subprocess.run(
        ["sh", "-c", command],
        cwd=cwd,
        env={**os.environ, **variables},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return None
    return f"`{command}` failed ({result.returncode}): {(result.stderr or result.stdout).strip()}"
