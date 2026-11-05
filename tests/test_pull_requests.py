import pytest

from t3cc.claude.pull_requests import CreatedPullRequest, created_by

GH = "https://github.com/owner/repository/pull"

T3_CASES = [
    ('gh pr create --base main --head feature --title "fix: 7 things" --body-file /tmp/body.md', f"{GH}/124\n", [124]),
    (
        '/bin/zsh -lc "git push -u origin feature 2>&1 | tail -2 && gh pr create --fill"',
        f"remote: Create a pull request for 'feature' on GitHub by visiting:\nremote:      {GH}/new/feature\n{GH}/124",
        [124],
    ),
    (
        "/bin/zsh -lc \"cat > /tmp/body.md <<'EOF'\nSet the \"'`hidden`'\" flag.\nEOF\n"
        'gh pr create --fill --body-file /tmp/body.md"',
        f"{GH}/62\n",
        [62],
    ),
    ("gh pr view 120 --json url --jq .url && gh pr create --fill", f"{GH}/120\n{GH}/124", [124]),
    (
        "gh pr create --base main --head a --fill && gh pr create --base a --head b --fill",
        f"{GH}/124\n{GH}/125",
        [124, 125],
    ),
    (
        "cargo test 2>&1 | tail -1 && timeout 60 gh pr create -R owner/repository --fill",
        f"test result: ok\n{GH}/1",
        [1],
    ),
    (
        "glab mr create --fill --yes",
        "!3 Fix the thing (feature)\n https://gitlab.com/owner/repository/-/merge_requests/3\n",
        [3],
    ),
    ("timeout 60 gh pr create --fill", f"{GH}/60", [60]),
    ("gh pr create#typo; gh pr view --json url --jq .url", f"{GH}/124", []),
    ("cat > /tmp/notes.md <<'EOF'\nEOF\ngh pr create --fill", f"{GH}/124", [124]),
    ("gh pr view 124 --json url --jq .url", f"{GH}/124", []),
    ("sqlite3 audit.db \"select output from items where input like '%gh pr create%'\"", f"{GH}/124", []),
    ("echo 'example: gh pr create; gh pr create --fill' && gh pr view --json url --jq .url", f"{GH}/124", []),
    ("gh pr view --json url --jq .url && gh pr create --fill", f"{GH}/120\n{GH}/124", []),
    (
        "cat > notes.md <<'EOF'\nOpen it with gh pr create.\nEOF\ngh pr list --json url --jq '.[].url'",
        f"{GH}/124",
        [],
    ),
    ("gh pr view --json url --jq .url;# note; gh pr create --fill", f"{GH}/124", []),
    ("gh pr list --json url --jq '.[].url' && gh pr create --fill > /dev/null", f"{GH}/120", []),
    ("gh pr create --fill", "https://gitlab.com/owner/repository/-/merge_requests/3", []),
    ("printf '%s\\n' 'https://github.com/owner/repository/pull/120'; gh pr create --fill > /dev/null", f"{GH}/120", []),
]


@pytest.mark.parametrize(("command", "output", "linked"), T3_CASES)
def test_links_what_t3_links(command, output, linked):
    assert [pr.number for pr in created_by(command, output)] == linked


def test_created_pull_request_keys_are_lowercase():
    output = "https://Git.Example.COM/Group/Sub/Repo/-/merge_requests/39\n"
    assert created_by("glab mr create --fill", output) == [
        CreatedPullRequest("git.example.com", "group/sub/repo", 39, output.strip())
    ]
    assert created_by("glab mr create --fill", output)[0].key == "git.example.com/group/sub/repo#39"


@pytest.mark.parametrize(
    "output",
    [
        "https://git.cafe/owner/repository/pull/3",
        "https://dev.azure.com/org/project/_git/repo/pullrequest/3",
        "https://example.com/owner/repository/pulls/3",
        "https://github.com/owner/repository/pull/0",
        "ftp://github.com/owner/repository/pull/3",
        "https:///owner/repository/pull/3",
        "https://[broken/owner/repository/pull/3",
    ],
)
def test_ignores_urls_no_create_command_prints(output):
    assert created_by("gh pr create --fill", output) == []


def test_shell_wrapper_keeps_escaped_and_bare_pieces():
    command = '/bin/sh -c \'gh pr create --fill\'\\ "--title \\"x\\""'
    assert [pr.number for pr in created_by(command, f"{GH}/9")] == [9]


def test_escapes_do_not_split_commands():
    command = r'gh pr create --title "a \"b\"" && echo \;'
    assert [pr.number for pr in created_by(command, f"{GH}/5")] == [5]


GL = "https://gitlab.com/owner/repository/-/merge_requests"
TO_URL = "| python3 -c \"import json,sys; print(json.load(sys.stdin)['web_url'])\""


@pytest.mark.parametrize(
    ("command", "output", "linked"),
    [
        (
            "cat > /tmp/mr.md <<'EOF'\nStacked on !41.\nEOF\n"
            'glab api -X POST "projects/owner%2Frepository/merge_requests" -f source_branch=b '
            f'-f "description=$(cat /tmp/mr.md)" 2>&1 {TO_URL}',
            f"{GL}/42",
            [42],
        ),
        ("sleep 5; glab api --method=POST projects/7/merge_requests -f title=x", f"{GL}/43", [43]),
        ("gh api repos/owner/repository/pulls -f title=x -f head=b --jq .html_url", f"{GH}/7", [7]),
        ("gh api /repos/owner/repository/pulls --input body.json --jq .html_url", f"{GH}/7", [7]),
        ("gh api repos/owner/repository/pulls --jq '.[].html_url'", f"{GH}/7", []),
        ("glab api projects/7/merge_requests -X GET -f state=opened", f"{GL}/43", []),
        ("glab api -X POST projects/7/merge_requests/43/notes -f body=hi", f"{GL}/43", []),
        ("glab api -X POST projects/7/merge_requests -f title=x && glab api projects/7/merge_requests", f"{GL}/3", []),
        ("echo 'glab api -X POST projects/7/merge_requests -f title=x'", f"{GL}/3", []),
    ],
)
def test_links_what_api_calls_create(command, output, linked):
    assert [pr.number for pr in created_by(command, output)] == linked
