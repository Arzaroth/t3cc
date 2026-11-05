"""The pull requests a shell command opened, by the rule T3 Code's PullRequestSyncReactor links them with."""

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

COMMAND_START = r"(?:^|[;&|({`\n]|\b(?:do|then|else)\s)\s*(?:(?:\w+=\S*|env|time|nohup|timeout\s+\S+)\s+)*"
CREATE_COMMANDS = {"github": r"\bgh\s+pr\s+create\b", "gitlab": r"\bglab\s+mr\s+create\b"}
CREATE_RUNS = {
    kind: re.compile(rf"{COMMAND_START}{command}(?=$|[\s;&|)])") for kind, command in CREATE_COMMANDS.items()
}
SHELL_WRAPPER = re.compile(r"^\s*(?:\S*/)?(?:ba|z|da|k)?sh\s+-l?c\s+(?=[\"'])([\s\S]*)$")
SHELL_WORD_PIECE = re.compile(r"\"((?:\\[\s\S]|[^\"\\])*)\"|'([^']*)'|\\([\s\S])|(\S)")
HEREDOC_BODY = re.compile(r"<<-?[ \t]*(['\"]?)(\w+)\1[^\n]*\n(?:(?:[\s\S]*?\n)?[ \t]*\2[ \t]*(?=\n|$)|[\s\S]*$)")
QUOTED_OR_COMMENT = re.compile(r"'[^']*'|\"(?:\\[\s\S]|[^\"\\])*\"|(?:^|(?<=[\s;&|(]))#[^\n]*", re.M)
QUOTED_WORD = re.compile(r"'([^\s'\\;&|()<>`$#]+)'|\"([^\s\"\\;&|()<>`$#]+)\"")
API_POST = r"(?=[^;&|\n]*\s(?:-X\s*|--method[\s=]+)POST\b)"
API_FIELDS = r"(?![^;&|\n]*\s(?:-X|--method)\b)(?=[^;&|\n]*\s(?:-[fF]|--(?:raw-)?field|--input)\b)"
API_WRITE = rf"(?:{API_POST}|{API_FIELDS})"
API_CREATES = [
    re.compile(rf"{COMMAND_START}gh\s+api\b{API_WRITE}(?=[^;&|\n]*\s(?:/?repos/)?[^\s/]+/[^\s/]+/pulls(?=$|[\s;&|)]))"),
    re.compile(
        rf"{COMMAND_START}glab\s+api\b{API_WRITE}(?=[^;&|\n]*\s/?projects/[^\s/]+/merge_requests(?=$|[\s;&|)]))"
    ),
]
COMMAND_SEPARATOR = re.compile(r"&&|\|\||[;|\n]")
LISTS_CHANGE_REQUESTS = re.compile(r"\b(?:gh\s+(?:pr\s+(?:list|status)|search\s+prs|api)|glab\s+(?:mr\s+list|api))\b")
URL_LINE = re.compile(r"^[ \t]*(https?://\S+)[ \t]*$", re.M)

GITCAFE_HOSTS = {"git.cafe", "staging.git.cafe"}
GITCAFE_PATH = re.compile(r"^/([^/]+/[^/]+)/pulls?/(\d+)(?:/|$)")
AZURE_PATH = re.compile(r"^/((?:[^/]+/)*_git/[^/]+)/pullrequest/(\d+)(?:/|$)")
CHANGE_REQUEST_PATHS = {
    "github": re.compile(r"^/([^/]+/[^/]+)/pull/(\d+)(?:/|$)"),
    "gitlab": re.compile(r"^/([^/]+(?:/[^/]+)+)/-/merge_requests/(\d+)(?:/|$)"),
}


@dataclass(frozen=True)
class CreatedPullRequest:
    host: str
    repository: str
    number: int
    url: str
    created_at: str = field(default="", compare=False)

    @property
    def key(self) -> str:
        return f"{self.host}/{self.repository}#{self.number}"


def _create_count(code: str) -> int:
    runs = [*CREATE_RUNS.values(), *API_CREATES]
    return sum(len(run.findall(code)) for run in runs)


def _shell_script(command: str) -> str:
    wrapped = SHELL_WRAPPER.match(command)
    if wrapped is None:
        return command
    pieces = []
    for double, single, escaped, bare in SHELL_WORD_PIECE.findall(wrapped.group(1)):
        if double:
            pieces.append(re.sub(r"\\([\"\\$`])", r"\1", double))
        else:
            pieces.append(single or escaped or bare)
    return "".join(pieces)


def _shell_segments(script: str) -> list[str]:
    segments, current, quote, index = [], "", None, 0
    while index < len(script):
        char = script[index]
        if quote is not None:
            current += char
            if char == "\\" and quote == '"':
                index += 1
                current += script[index : index + 1]
            elif char == quote:
                quote = None
        elif char == "\\":
            current += script[index : index + 2]
            index += 1
        elif (pair := script[index : index + 2]) in ("&&", "||") or char in ";|\n":
            segments.append(current)
            current = ""
            index += len(pair) - 1 if pair in ("&&", "||") else 0
        else:
            if char in "'\"":
                quote = char
            current += char
        index += 1
    segments.append(current)
    return segments


def _shell_code(script: str) -> str:
    unquoted = QUOTED_WORD.sub(lambda word: word.group(1) or word.group(2), HEREDOC_BODY.sub("<<", script))
    return QUOTED_OR_COMMENT.sub("''", unquoted)


def _parse_url(value: str) -> tuple[str, str, str, int] | None:
    """(host kind, host, repository, number) of a change request URL, as T3's definition registry claims it."""
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower()
    except ValueError:
        return None
    if url.scheme not in ("http", "https") or not host:
        return None
    claims = [("gitcafe", GITCAFE_PATH if host in GITCAFE_HOSTS else None), ("azure-devops", AZURE_PATH)]
    for kind, route in [*claims, *CHANGE_REQUEST_PATHS.items()]:
        match = route.match(url.path) if route else None
        if match and int(match.group(2)) > 0:
            return kind, host, match.group(1).lower(), int(match.group(2))
    return None


def created_by(command: str, output: str) -> list[CreatedPullRequest]:
    if not re.search(r"\b(?:gh\s+pr\s+create|glab\s+mr\s+create|gh\s+api|glab\s+api)\b", command):
        return []
    script = _shell_script(command)
    code = _shell_code(script)
    creates = _create_count(code)
    if creates == 0:
        return []
    other_commands = "\n".join(part for part in COMMAND_SEPARATOR.split(code) if _create_count(part) == 0)
    if LISTS_CHANGE_REQUESTS.search(other_commands):
        return []
    other_script = "\n".join(s for s in _shell_segments(script) if _create_count(_shell_code(s)) == 0)
    creators = {kind for kind, run in CREATE_RUNS.items() if run.search(code)}
    creators |= {kind for kind, run in zip(CREATE_RUNS, API_CREATES, strict=True) if run.search(code)}
    created: dict[str, CreatedPullRequest] = {}
    for url in URL_LINE.findall(output):
        parsed = _parse_url(url)
        if parsed is None or parsed[0] not in creators:
            continue
        _, host, repository, number = parsed
        if re.search(rf"(?:^|(?<=[\s#/=(\"'])){number}(?=$|[\s);&|/\"'])", other_script):
            continue
        pull_request = CreatedPullRequest(host, repository, number, url)
        created[pull_request.key] = pull_request
    return [] if len(created) > creates else list(created.values())
