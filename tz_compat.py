from __future__ import annotations

import re
from datetime import timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo


_FIXED_TZ = {
    "UTC": timezone.utc,
    "Etc/UTC": timezone.utc,
    "Asia/Shanghai": timezone(timedelta(hours=8), name="CST"),
    "PRC": timezone(timedelta(hours=8), name="CST"),
}

_OFFSET_RE = re.compile(r"^(?:UTC)?([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


def get_tzinfo(name: str | None) -> tzinfo:
    key = str(name or "").strip()
    if not key:
        return timezone.utc

    try:
        return ZoneInfo(key)
    except Exception:
        pass

    fixed = _FIXED_TZ.get(key)
    if fixed is not None:
        return fixed

    match = _OFFSET_RE.match(key)
    if match:
        sign = -1 if match.group(1) == "-" else 1
        hours = int(match.group(2))
        minutes = int(match.group(3) or "0")
        delta = timedelta(hours=hours, minutes=minutes) * sign
        return timezone(delta, name=key.upper())

    return timezone.utc
