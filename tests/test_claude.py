import json
from pathlib import Path

import pytest

from conftest import assistant, user
from t3cc.claude import transcript
from t3cc.claude.store import ClaudeStore
from t3cc.claude.synth import CONTINUATION_PROMPT, Turn, build_session, merge_turns
from t3cc.errors import T3ccError


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("  hi  ", "hi"),
        (None, ""),
        (42, ""),
        (
            [
                {"type": "text", "text": " a "},
                {"type": "tool_use", "text": "no"},
                {"type": "output_text", "text": "b"},
                {"type": "input_text", "text": "  "},
                {"type": "text"},
                "loose",
            ],
            "a\nb",
        ),
    ],
)
def test_extract_text(content, expected):
    assert transcript.extract_text(content) == expected


def test_parse_follows_t3_rules(world):
    sid = "11111111-1111-4111-8111-111111111111"
    path = world.transcript(
        "/repo",
        [
            "not json",
            "[1, 2]",
            {"type": "queue-operation"},
            user("hidden", isMeta=True),
            user("side", isSidechain=True),
            assistant("summary", isCompactSummary=True),
            user("first line\nsecond", ts="2026-09-01T10:00:00Z", gitBranch="feat"),
            assistant("", model="<synthetic>"),
            assistant([{"type": "tool_use", "name": "Bash"}]),
            assistant("answer", model="claude-fable-5"),
            {"type": "ai-title", "aiTitle": "AI title"},
            {"type": "attachment", "message": "not a dict"},
            user("bad ts", ts="nope"),
        ],
        session_id=sid,
    )
    t = transcript.parse(path)
    assert t.session_id == sid
    assert t.title == "AI title"
    assert t.model == "claude-fable-5"
    assert t.cwd == "/repo"
    assert t.branch == "feat"
    assert [(m.role, m.text) for m in t.messages] == [
        ("user", "first line\nsecond"),
        ("assistant", "answer"),
        ("user", "bad ts"),
    ]
    assert t.messages[0].created_at == "2026-09-01T10:00:00.000Z"
    assert t.messages[2].created_at == t.updated_at
    assert t.has_user_message


def test_parse_prefers_custom_title(world):
    path = world.transcript("/r", [user("x"), {"aiTitle": "ai"}, {"customTitle": "mine"}])
    assert transcript.parse(path).title == "mine"


def test_parse_derives_title_from_first_user_message(world):
    path = world.transcript("/r", [assistant("hello"), user("  " + "x" * 150 + "\nmore")])
    assert transcript.parse(path).title == "x" * 100


def test_parse_without_messages(world):
    path = world.transcript("/r", [{"type": "mode"}], stamp_cwd=False)
    t = transcript.parse(path)
    assert t.title == "Imported thread"
    assert t.cwd is None
    assert t.messages == ()
    assert not t.has_user_message


def test_store_encodes_every_non_alnum_in_project_dirs():
    store = ClaudeStore(Path("/p"))
    assert store.project_dir("/home/a/.t3/w_x") == Path("/p/-home-a--t3-w-x")
    assert store.session_path("/a", "sid") == Path("/p/-a/sid.jsonl")


def test_store_listing_and_lookup(world, tmp_path):
    store = ClaudeStore(world.paths.claude_projects)
    small = world.transcript("/a", [user("x")], "aaaa1111-0000-4000-8000-000000000000")
    big = world.transcript("/b", [user("x" * 500)], "aaaa1111-0000-4000-8000-000000000000")
    other = world.transcript("/a", [user("y")], "bbbb2222-0000-4000-8000-000000000000")
    assert set(store.all_transcripts()) == {small, big, other}
    assert store.by_session_id("aaaa1111-0000-4000-8000-000000000000") == big
    assert store.by_session_id("missing") is None
    assert store.find("aaaa") == big
    assert store.find(str(other)) == other
    loose = tmp_path / "loose.jsonl"
    loose.write_text("")
    assert store.find(str(loose)) == loose
    with pytest.raises(T3ccError, match="no Claude Code session"):
        store.find("zzzz")
    with pytest.raises(T3ccError, match="ambiguous"):
        store.find("")


def test_store_copy_into(world):
    store = ClaudeStore(world.paths.claude_projects)
    source = world.transcript("/a", [user("x")])
    dest = store.copy_into(source, "/b")
    assert dest == store.project_dir("/b") / source.name
    assert dest is not None
    assert dest.read_text() == source.read_text()
    assert store.copy_into(source, "/b") is None


def test_store_write_session_refuses_overwrite(world):
    store = ClaudeStore(world.paths.claude_projects)
    path = store.write_session("/w", "sid", [{"a": 1}, {"b": "é"}])
    assert [json.loads(line) for line in path.read_text().splitlines()] == [{"a": 1}, {"b": "é"}]
    with pytest.raises(T3ccError, match="overwrite"):
        store.write_session("/w", "sid", [])


def test_merge_turns():
    merged = merge_turns(
        [
            Turn("assistant", "a1", "t0"),
            Turn("assistant", " a2 ", "t1"),
            Turn("user", "  ", "t2"),
            Turn("user", "u1", "t3"),
        ]
    )
    assert [(t.role, t.text, t.timestamp) for t in merged] == [
        ("user", CONTINUATION_PROMPT, "t0"),
        ("assistant", "a1\n\na2", "t0"),
        ("user", "u1", "t3"),
    ]
    assert merge_turns([]) == []


def test_build_session_chains_records():
    sid, records = build_session(
        [Turn("user", "q", "t0"), Turn("assistant", "a", "t1")], cwd="/c", branch=None, model="m", title="T"
    )
    first, second, title = records
    assert first["parentUuid"] is None and first["message"] == {"role": "user", "content": "q"}
    assert second["parentUuid"] == first["uuid"]
    assert second["message"]["content"] == [{"type": "text", "text": "a"}]
    assert second["message"]["model"] == "m"
    assert {first["sessionId"], second["sessionId"], title["sessionId"]} == {sid}
    assert first["gitBranch"] == "" and first["cwd"] == "/c"
    assert title == {"type": "ai-title", "aiTitle": "T", "sessionId": sid}
