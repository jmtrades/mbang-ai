"""Secret scrubbing for every error, log and audit line."""
from __future__ import annotations

import re

# (pattern, replacement)
_RULES = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "[REDACTED]"),
    (re.compile(r"EAA[A-Za-z0-9]{10,}"), "[REDACTED]"),
    (re.compile(r"ya29\.[A-Za-z0-9_\-\.]{8,}"), "[REDACTED]"),
    (re.compile(r"1//[A-Za-z0-9_\-]{8,}"), "[REDACTED]"),
    (re.compile(r"\b(access_token|refresh_token|client_secret|appsecret_proof|api_key|password|token)=[^&\s\"']+", re.I),
     r"\1=[REDACTED]"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.=:/+]{6,}", re.I), "Bearer [REDACTED]"),
    (re.compile(r"(\"?(?:x-api-key|authorization)\"?\s*[:=]\s*\"?)[^\"\s,}]{6,}", re.I), r"\1[REDACTED]"),
]


def redact(text: object) -> str:
    s = str(text)
    for pat, rep in _RULES:
        s = pat.sub(rep, s)
    return s
