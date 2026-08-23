import os

from llmwiki.core import wiki_toggle as wt


def test_default_on_when_no_marker(tmp_path):
    assert wt.is_on(tmp_path, "sid-a") is True


def test_off_then_on_round_trip(tmp_path):
    wt.set_state(tmp_path, "sid-a", on=False)
    assert wt.is_on(tmp_path, "sid-a") is False
    wt.set_state(tmp_path, "sid-a", on=True)
    assert wt.is_on(tmp_path, "sid-a") is True


def test_off_is_sticky_until_flipped(tmp_path):
    wt.set_state(tmp_path, "sid-a", on=False)
    assert wt.is_on(tmp_path, "sid-a") is False
    assert wt.is_on(tmp_path, "sid-a") is False


def test_per_sid_isolation(tmp_path):
    wt.set_state(tmp_path, "sid-a", on=False)
    assert wt.is_on(tmp_path, "sid-a") is False
    assert wt.is_on(tmp_path, "sid-b") is True


def test_set_on_when_already_on_is_noop(tmp_path):
    wt.set_state(tmp_path, "sid-a", on=True)
    assert wt.is_on(tmp_path, "sid-a") is True


def test_empty_sid_is_noop_and_on(tmp_path):
    wt.set_state(tmp_path, "", on=False)
    assert wt.is_on(tmp_path, "") is True
    assert not (tmp_path / wt.TOGGLE_DIRNAME / ".off").exists()


def test_prune_removes_stale_and_keeps_fresh(tmp_path):
    wt.set_state(tmp_path, "old-sid", on=False)
    wt.set_state(tmp_path, "new-sid", on=False)
    old_marker = tmp_path / wt.TOGGLE_DIRNAME / "old-sid.off"
    new_marker = tmp_path / wt.TOGGLE_DIRNAME / "new-sid.off"
    stale = old_marker.stat().st_mtime - (wt.PRUNE_AGE_SEC + 3600)
    os.utime(old_marker, (stale, stale))
    wt.prune(tmp_path)
    assert not old_marker.exists()
    assert new_marker.exists()
    assert wt.is_on(tmp_path, "old-sid") is True
    assert wt.is_on(tmp_path, "new-sid") is False


def test_prune_missing_dir_is_noop(tmp_path):
    wt.prune(tmp_path)


def test_is_on_survives_unreadable_root(tmp_path):
    assert wt.is_on(tmp_path / "does-not-exist", "sid-a") is True
