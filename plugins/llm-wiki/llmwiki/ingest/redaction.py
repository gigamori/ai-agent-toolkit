# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Redaction / secret-scan (D16).

Runs in EVERY normalization front-end BEFORE content-hashing. Masks secrets and
absolute local paths; conservative default = redact + flag when suspicious.

I/O contract:
    redact(text: str) -> RedactionResult
      in : raw text (untrusted source content)
      out: RedactionResult { text: redacted text, flags: list[Flag], count: int }
           - every match is replaced by a fixed placeholder token
           - flags records (kind, placeholder, span_preview) for the human gate
           - count = number of redactions applied
    The function NEVER raises on content; an empty / non-str input yields an empty
    result so it is safe to call unconditionally in the pipeline.

Conservative default (D16/R8): patterns err toward over-masking. A redaction is a
flagged event; downstream (human gate + lint) reviews flags. False positives
are acceptable (text broken) over false negatives (secret leaked).

Placeholders are deterministic by kind so the same secret hashes the same way and
dedup (D18) stays valid.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# Fixed placeholder tokens — deterministic per kind (keeps dedup stable, D18).
PH_SECRET = "«REDACTED:SECRET»"
PH_ABSPATH = "«REDACTED:ABS_PATH»"


@dataclass
class Flag:
    kind: str          # "secret" | "abs_path"
    placeholder: str   # token substituted in
    preview: str       # short, already-masked preview for the human gate


@dataclass
class RedactionResult:
    text: str
    flags: list[Flag] = field(default_factory=list)
    count: int = 0


# --- secret patterns (conservative) ---------------------------------------
# Each entry: (kind, compiled regex). Order matters only for overlapping spans;
# we apply them sequentially, longest-token classes first.
_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    # AWS access key id
    ("secret", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # GitHub tokens (ghp_, gho_, ghu_, ghs_, ghr_, github_pat_)
    ("secret", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("secret", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    # Slack tokens
    ("secret", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    # Google API key
    ("secret", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    # OpenAI / Anthropic style keys
    ("secret", re.compile(r"\bsk-[A-Za-z0-9\-_]{20,}\b")),
    # Private key PEM blocks
    (
        "secret",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    # Bearer / Authorization header values
    ("secret", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]{16,}=*")),
    # Generic key=value secret assignments (api_key, secret, token, password)
    (
        "secret",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|token|passwd|password)\b\s*[:=]\s*"
            r"['\"]?[A-Za-z0-9\-._~+/]{8,}['\"]?"
        ),
    ),
]

# --- absolute-path patterns ------------------------------------------------
# Windows drive-letter paths (C:\..., C:/...), UNC (\\host\share), and POSIX
# absolute paths whose first segment is a well-known root (home dirs, /etc,
# /usr, /var, /root, /tmp, /opt, /mnt, /home, /Users). A bare leading slash is
# NOT masked (too many false positives on URLs / markdown), but home-rooted and
# system-rooted absolutes are.
_ABSPATH_PATTERNS: list[re.Pattern] = [
    re.compile(r"[A-Za-z]:[\\/][^\s'\"<>|]*"),          # drive-letter
    re.compile(r"\\\\[^\s'\"<>|]+"),                     # UNC
    re.compile(
        r"(?<![\w/])/(?:home|Users|root|etc|usr|var|tmp|opt|mnt|srv)"
        r"(?:/[^\s'\"<>|]*)?"
    ),                                                   # POSIX system/home roots
    re.compile(r"~(?:/[^\s'\"<>|]*)?"),                  # ~ home shorthand
]


def _apply(text: str, kind: str, placeholder: str, pattern: re.Pattern,
           flags: list[Flag]) -> str:
    def _sub(m: re.Match) -> str:
        flags.append(Flag(kind=kind, placeholder=placeholder, preview=placeholder))
        return placeholder

    return pattern.sub(_sub, text)


def redact(text: str) -> RedactionResult:
    """Mask secrets + absolute paths in `text`. See module docstring for contract."""
    if not isinstance(text, str) or not text:
        return RedactionResult(text=text if isinstance(text, str) else "", flags=[], count=0)

    flags: list[Flag] = []
    out = text
    # Secrets first (so an abs path inside a secret line is already masked).
    for kind, pat in _SECRET_PATTERNS:
        out = _apply(out, kind, PH_SECRET, pat, flags)
    for pat in _ABSPATH_PATTERNS:
        out = _apply(out, "abs_path", PH_ABSPATH, pat, flags)

    return RedactionResult(text=out, flags=flags, count=len(flags))


def is_clean(text: str) -> bool:
    """True if redact() would make no change (no secret / abs-path detected)."""
    return redact(text).count == 0
