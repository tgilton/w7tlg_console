"""Diagnostic logging for the open question in TX_GATING_AUDIT.md 2.2:
what does rigctld's `t` return for this rig on mic/foot-switch PTT?

Hamlib's ptt_t is 0=OFF, 1=ON, 2=ON_MIC, 3=ON_DATA. parse_ptt_reply
accepts 0-3; the fast PTT monitor compares against '1' alone. If mic PTT
reports 2, the fast gate never fires for voice TX. This machine has hamlib
as binaries only, so one real transmission has to answer it.

Behaviour change: none. This is log output only, and it must stay cheap —
the fast monitor calls it every 5ms.
"""
import pytest

import rig.rigctld_client as rc


@pytest.fixture(autouse=True)
def clean_seen():
    rc._PTT_VALUES_SEEN.clear()
    yield
    rc._PTT_VALUES_SEEN.clear()


def _messages(caplog):
    return [r.message for r in caplog.records if "seen for the first time" in r.message]


def test_logs_each_distinct_nonzero_value_once(caplog):
    with caplog.at_level("INFO"):
        for _ in range(50):
            rc.note_ptt_reply_value("1", "fast PTT monitor")
        for _ in range(50):
            rc.note_ptt_reply_value("2", "fast PTT monitor")

    msgs = _messages(caplog)
    assert len(msgs) == 2, "once per distinct value, not once per poll"
    assert "'1'" in msgs[0] and "RIG_PTT_ON" in msgs[0]
    assert "'2'" in msgs[1] and "ON_MIC" in msgs[1]


def test_zero_and_blank_are_never_logged(caplog):
    with caplog.at_level("INFO"):
        for raw in ("0", " 0 ", "", "   ", None):
            rc.note_ptt_reply_value(raw, "main poll")
    assert _messages(caplog) == []


def test_data_ptt_value_is_named(caplog):
    with caplog.at_level("INFO"):
        rc.note_ptt_reply_value("3", "main poll")
    assert "ON_DATA" in _messages(caplog)[0]


def test_a_straggler_is_reported_as_a_straggler_not_a_ptt_state(caplog):
    """A desynced reply like a frequency landing in the `t` slot should be
    logged once and labelled for what it is, not silently counted as PTT."""
    with caplog.at_level("INFO"):
        rc.note_ptt_reply_value("14074000", "main poll")
    msg = _messages(caplog)[0]
    assert "desynced straggler" in msg


def test_logging_is_capped_so_a_desync_storm_cannot_flood(caplog):
    with caplog.at_level("INFO"):
        for i in range(200):
            rc.note_ptt_reply_value(str(1000 + i), "main poll")
    assert len(_messages(caplog)) == rc._PTT_VALUES_LOG_CAP


def test_the_reader_is_named_so_the_two_paths_can_be_told_apart(caplog):
    with caplog.at_level("INFO"):
        rc.note_ptt_reply_value("2", "fast PTT monitor")
    assert "fast PTT monitor" in _messages(caplog)[0]


def test_it_does_not_change_how_the_reply_is_parsed():
    """Pinned separately: the diagnostic must not alter behaviour. '2' and
    '3' still parse as TX for the main poll, exactly as before."""
    assert rc.parse_ptt_reply("2") is True
    assert rc.parse_ptt_reply("3") is True
    assert rc.parse_ptt_reply("0") is False
    assert rc.parse_ptt_reply("14074000") is None
