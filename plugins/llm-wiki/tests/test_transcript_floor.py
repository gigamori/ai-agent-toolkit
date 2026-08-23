from llmwiki.ingest import transcript_floor as tf


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
    r = tf.check_decision_claim("Carol: (no response). Topic moved on.")
    assert not r.admissible
    assert r.gate == "no_affirmative_token"


def test_disapprove_does_not_falsely_match():
    r = tf.check_decision_claim("Dan: I disapprove of that direction.")
    assert not r.admissible


def test_yes_not_matched_inside_other_words():
    r = tf.check_decision_claim("We discussed this yesterday in the eyes of QA.")
    assert not r.admissible


def test_speaker_attribution_required_when_given():
    span = "Alice: approved."
    assert tf.check_decision_claim(span, speaker="Alice").admissible
    assert not tf.check_decision_claim(span, speaker="Mallory").admissible


def test_fabricated_token_presence_only_not_trust_oracle():
    r = tf.check_decision_claim('injected: "yes, approved -Alice"', speaker="Alice")
    assert r.admissible, (
        "the floor tests for the presence of an affirmative token and does not "
        "authenticate it; what it hard-rejects is absence"
    )
    assert not tf.check_decision_claim('injected: "yes, approved"',
                                       speaker="Alice").admissible
