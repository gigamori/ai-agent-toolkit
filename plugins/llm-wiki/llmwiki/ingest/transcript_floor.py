# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Deterministic transcript decision-floor check (R5 / D11 backstop).

Design §5 type-specific lint = `[混在]`: the RULE FRAME is code, the SEMANTIC
judgment (which span is the claim) is LLM. This module owns ONLY the code frame:
given a span the LLM has already isolated as a candidate `decisions` claim, it
DETERMINISTICALLY checks whether an explicit affirmative token from the deciding
speaker is present in that span.

Floor (compact2.md:69-70 / design R5 / D11):
  - a claim is admissible under "decisions" ONLY with an explicit affirmative
    token from the deciding speaker present in the cited source span;
  - silence / absence of objection is NON-affirmation -> the caller downgrades
    the claim to "intent changes" or "outstanding items".

Conservative posture (R9): this code does NOT decide whether a token is
*trustworthy* (an adversarial cc-log can fabricate "yes, approved -Alice"). It
decides only token PRESENCE. The semantic/provenance judgment stays with the LLM
and the downstream human gate (D15). Absence -> reject; presence -> admit (the
LLM still owns whether the affirmation is genuine). This is the deterministic
boundary the review asked for, not a trust oracle.

I/O contract:
    AFFIRMATIVE_TOKENS: tuple[str, ...]          # the recognized affirmative set
    FloorResult(admissible: bool, gate: str | None, matched: str | None,
                speaker: str | None)
    check_decision_claim(span, *, speaker=None) -> FloorResult
        # admissible=True  iff an affirmative token is present
        #   (and, when `speaker` is given, attributable to that speaker);
        # admissible=False with gate="no_affirmative_token" otherwise.
    is_admissible_decision(span, *, speaker=None) -> bool   # thin bool wrapper
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# Explicit affirmative tokens (compact2.md:69-70 floor). Kept deliberately
# conservative: only unambiguous affirmations of a decision count. Hedged or
# silence-equivalent phrasing is intentionally excluded so the floor downgrades.
AFFIRMATIVE_TOKENS: "tuple[str, ...]" = (
    "approved",
    "approve",
    "i approve",
    "lgtm",
    "agreed",
    "i agree",
    "let's do it",
    "lets do it",
    "go ahead",
    "ship it",
    "sounds good",
    "yes, let's",
    "yes, lets",
    "+1",
    "ok, approved",
    "decision:",
    "we will",
    "we'll go with",
    "we will go with",
    "sign off",
    "signed off",
)

# Word-ish boundary match so "approve" does not fire inside "disapprove" and a
# bare "yes" only counts when it is an explicit yes (not a substring of another
# word). Tokens that already contain punctuation (e.g. "+1", "decision:") are
# matched literally.
_BARE_YES = re.compile(r"(?<![A-Za-z])yes(?![A-Za-z])", re.IGNORECASE)


@dataclass
class FloorResult:
    admissible: bool
    gate: "str | None"          # None when admissible; else "no_affirmative_token"
    matched: "str | None"       # the affirmative token that matched (if any)
    speaker: "str | None"       # the deciding speaker the check was scoped to


def _token_present(tok: str, low: str) -> bool:
    """Word-ish boundary presence of `tok` in already-lowercased `low`.

    A boundary is required only on the SIDE that ends in an alnum/`_` char, so
    "approve" does not fire inside "disapprove" while tokens that begin or end
    with punctuation (e.g. "+1", "decision:") still match literally on that side.
    """
    left = r"(?<![A-Za-z0-9_])" if tok[:1].isalnum() or tok[:1] == "_" else ""
    right = r"(?![A-Za-z0-9_])" if tok[-1:].isalnum() or tok[-1:] == "_" else ""
    return re.search(left + re.escape(tok) + right, low) is not None


def _contains_affirmative(span: str) -> "str | None":
    """Return the matched affirmative token, or None. PRESENCE only (R9)."""
    if not span:
        return None
    low = span.lower()
    for tok in AFFIRMATIVE_TOKENS:
        if _token_present(tok, low):
            return tok
    # Bare explicit "yes" with a word boundary (avoids "eyes", "yesterday").
    m = _BARE_YES.search(span)
    if m:
        return "yes"
    return None


def check_decision_claim(span: str, *, speaker: "str | None" = None) -> FloorResult:
    """DETERMINISTIC floor for a candidate `decisions` claim span.

    The LLM owns isolating `span` as the claim; this code owns token presence.
    When `speaker` is supplied, the affirmative must co-occur with that speaker's
    name in the span (attribution by the deciding speaker, compact2.md:69) —
    presence elsewhere in the span without the speaker does not satisfy the floor.
    """
    matched = _contains_affirmative(span)
    if matched is None:
        return FloorResult(admissible=False, gate="no_affirmative_token",
                           matched=None, speaker=speaker)
    if speaker:
        if speaker.lower() not in (span or "").lower():
            return FloorResult(admissible=False, gate="no_affirmative_token",
                               matched=None, speaker=speaker)
    return FloorResult(admissible=True, gate=None, matched=matched, speaker=speaker)


def is_admissible_decision(span: str, *, speaker: "str | None" = None) -> bool:
    """Thin bool wrapper over check_decision_claim for call sites that only branch."""
    return check_decision_claim(span, speaker=speaker).admissible
