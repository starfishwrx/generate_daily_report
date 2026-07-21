from __future__ import annotations

import re


_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(PHPSESSID\s*[=:]\s*)[^;\s&,'\"}]+"), r"\1<redacted>"),
    (re.compile(r"(?i)((?:Cookie|Authorization|Admin-Token|e_token)\s*[=:]\s*)[^\r\n]+"), r"\1<redacted>"),
    (re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1<redacted>"),
    (re.compile(r"(?i)((?:access_?token|token|webhook)\s*[=:]\s*)[^;\s&,'\"}]+"), r"\1<redacted>"),
)


def redact_sensitive_text(value: object) -> str:
    text = str(value or "")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
