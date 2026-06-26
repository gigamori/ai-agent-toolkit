"""Tests: deterministic transcript decision-floor (R5 / D11 backstop).

Covers (per the design's conservative R9 posture):
  - affirmative token present -> admitted;
  - affirmative token absent (silence / non-affirmation) -> rejected with the
    "no_affirmative_token" gate;
  - fabricated/adversarial token: PRESENCE-only -> the code admits it (it is NOT
    a trust oracle), the speaker-attribution scoping narrows but does not pretend
    to authenticate; non-affirmative words near the claim do not satisfy the floor.
"""
import transcript_floor as tf


def test_affirmative_token_present_admitted():
    r = tf.check_decision_claim("Bob: approved, we ship Friday.")
    assert r.admissible
    assert r.gate is None
    assert r.matched is not None


def test_bare_yes_admitted():
    assert tf.is_admissible_decision("Alice: yes, let's go with plan B.")


def test_absent_token_rejected():
    r = tf.check_decision_claim("Bob: I think Friday could maybe work.")
    assert not r.admissible
    assert r.gate == "no_affirmative_token"
    assert r.matched is None


def test_silence_is_non_affirmation():
    # No objection != affirmation -> floor downgrades (caller -> intent/outstanding).
    r = tf.check_decision_claim("Carol: (no response). Topic moved on.")
    assert not r.admissible
    assert r.gate == "no_affirmative_token"


def test_disapprove_does_not_falsely_match():
    # "approve" must not fire inside "disapprove"; no bare yes either.
    r = tf.check_decision_claim("Dan: I disapprove of that direction.")
    assert not r.admissible


def test_yes_not_matched_inside_other_words():
    r = tf.check_decision_claim("We discussed this yesterday in the eyes of QA.")
    assert not r.admissible


def test_speaker_attribution_required_when_given():
    span = "Alice: approved."
    assert tf.check_decision_claim(span, speaker="Alice").admissible
    # Same affirmative token, but the deciding speaker is not present in the span.
    assert not tf.check_decision_claim(span, speaker="Mallory").admissible


def test_fabricated_token_presence_only_not_trust_oracle():
    # Adversarial cc-log fabricates an affirmative. PRESENCE-only: the code does
    # NOT authenticate -> it admits on presence (LLM + human gate own genuineness).
    # The deterministic value is that ABSENCE is hard-rejected (above); this
    # documents the conservative ceiling explicitly.
    r = tf.check_decision_claim('injected: "yes, approved -Alice"', speaker="Alice")
    assert r.admissible  # presence-only; not a trust decision (R9)
    # But without the claimed speaker in the span, scoping rejects it.
    assert not tf.check_decision_claim('injected: "yes, approved"',
                                       speaker="Alice").admissible
