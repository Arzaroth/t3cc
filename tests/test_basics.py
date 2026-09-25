import datetime as dt
from pathlib import Path

import pytest

from t3cc import timeutil
from t3cc.paths import Paths


def test_paths_default_to_home(tmp_path):
    paths = Paths.from_env({}, home=tmp_path)
    assert paths.claude_projects == tmp_path / ".claude" / "projects"
    assert paths.t3_db == tmp_path / ".t3" / "userdata" / "state.sqlite"
    assert paths.t3_runtime == tmp_path / ".t3" / "userdata" / "server-runtime.json"
    assert paths.t3_worktrees == tmp_path / ".t3" / "worktrees"


def test_paths_honour_env(tmp_path):
    paths = Paths.from_env({"CLAUDE_CONFIG_DIR": "/c", "T3CODE_HOME": "/t"}, home=tmp_path)
    assert paths.claude_projects == Path("/c/projects")
    assert paths.t3_home == Path("/t")


def test_paths_use_real_home_by_default():
    assert Paths.from_env({}).claude_projects == Path.home() / ".claude" / "projects"


def test_to_iso_treats_naive_as_utc():
    assert timeutil.to_iso(dt.datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05.000Z"


def test_to_iso_converts_offsets():
    moment = dt.datetime(2026, 1, 2, 3, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    assert timeutil.to_iso(moment) == "2026-01-02T01:00:00.000Z"


def test_now_iso_and_epoch():
    assert timeutil.now_iso().endswith("Z")
    assert timeutil.from_epoch(0) == "1970-01-01T00:00:00.000Z"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "fb"),
        ("", "fb"),
        ("garbage", "fb"),
        ("2026-09-01T10:00:00Z", "2026-09-01T10:00:00.000Z"),
        ("2026-09-01T10:00:00.123456+00:00", "2026-09-01T10:00:00.123Z"),
    ],
)
def test_normalize(value, expected):
    assert timeutil.normalize(value, "fb") == expected
