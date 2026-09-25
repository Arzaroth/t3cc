import datetime as dt


def to_iso(moment: dt.datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def now_iso() -> str:
    return to_iso(dt.datetime.now(dt.UTC))


def from_epoch(seconds: float) -> str:
    return to_iso(dt.datetime.fromtimestamp(seconds, dt.UTC))


def normalize(value: str | None, fallback: str) -> str:
    if not value:
        return fallback
    try:
        return to_iso(dt.datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return fallback
