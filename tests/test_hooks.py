import pytest

from t3cc.errors import T3ccError
from t3cc.send import hooks


def test_config_path(tmp_path):
    assert hooks.config_path({"XDG_CONFIG_HOME": str(tmp_path)}) == tmp_path / "t3cc" / "config.toml"
    assert hooks.config_path({}).parts[-3:] == (".config", "t3cc", "config.toml")


def test_load_merges_from_general_to_specific(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        """
[hooks]
pre-send = "a"
post-send = "a"
post-import = "a"

[env.box.hooks]
pre-send = "b"
post-send = "b"

[repo."github.com/O/R".hooks]
post-send = "c"
"""
    )
    assert hooks.load(config, "box", "github.com/o/r") == {"pre-send": "b", "post-send": "c", "post-import": "a"}
    assert hooks.load(config, "other", None) == {"pre-send": "a", "post-send": "a", "post-import": "a"}
    assert hooks.load(tmp_path / "missing.toml", "box", None) == {}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[hooks\n", "t3cc config"),
        ('[hooks]\nbefore = "x"\n', "unknown hook 'before'"),
        ("[hooks]\npre-send = 1\n", "must be a command string"),
        ('hooks = "x"\n', "hooks must be a table"),
        ('[env]\nbox = "x"\n', "env.box must be a table"),
    ],
)
def test_load_rejects_bad_configs(tmp_path, text, message):
    config = tmp_path / "config.toml"
    config.write_text(text)
    with pytest.raises(T3ccError, match=message):
        hooks.load(config, "box", None)


def test_run(tmp_path):
    assert hooks.run(None, str(tmp_path), {}) is None
    assert hooks.run('test "$X" = 1', str(tmp_path), {"X": "1"}) is None
    assert hooks.run("echo out; exit 2", str(tmp_path), {}) == "`echo out; exit 2` failed (2): out"
