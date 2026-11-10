from t3cc import __version__

PROTOCOL = 1
FEATURES = frozenset({"import.live", "import.pull-requests", "import.worktree"})


def describe() -> dict:
    return {"t3cc": __version__, "protocol": PROTOCOL, "features": sorted(FEATURES)}
