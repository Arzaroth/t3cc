import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    claude_projects: Path
    t3_home: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ, home: Path | None = None) -> "Paths":
        home = home or Path.home()
        claude_config = Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR") else home / ".claude"
        t3_home = Path(env["T3CODE_HOME"]) if env.get("T3CODE_HOME") else home / ".t3"
        return cls(claude_projects=claude_config / "projects", t3_home=t3_home)

    @property
    def t3_db(self) -> Path:
        return self.t3_home / "userdata" / "state.sqlite"

    @property
    def t3_runtime(self) -> Path:
        return self.t3_home / "userdata" / "server-runtime.json"

    @property
    def t3_worktrees(self) -> Path:
        return self.t3_home / "worktrees"


def claude_project_dir(projects: Path, cwd: str | Path) -> Path:
    return projects / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
