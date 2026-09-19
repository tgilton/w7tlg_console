"""Item 3: the FAST_PTT_MONITOR / FAST_PTT_POLL_MS settings.

The watchdog stays in the tree and stays on by default — the failure that
added it (commit c5f2ee2: an audio feedback loop that built for ~2s into
the transmitted signal) is real and recorded. What these settings buy is
the ability to run one session without it and compare the log, which is
how findings G and J get settled.

The parsing rule that matters: anything unclear falls back to the default.
A typo in .env must not silently disable the watchdog.
"""
import importlib

import pytest

import dashboard.server as server


# ----------------------------------------------------------------------
# _env_flag
# ----------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " On "])
def test_flag_truthy_words(monkeypatch, raw):
    monkeypatch.setenv("W7TLG_TEST_FLAG", raw)
    assert server._env_flag("W7TLG_TEST_FLAG", False) is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off", " Off "])
def test_flag_falsy_words(monkeypatch, raw):
    monkeypatch.setenv("W7TLG_TEST_FLAG", raw)
    assert server._env_flag("W7TLG_TEST_FLAG", True) is False


def test_flag_unset_uses_default(monkeypatch):
    monkeypatch.delenv("W7TLG_TEST_FLAG", raising=False)
    assert server._env_flag("W7TLG_TEST_FLAG", True) is True
    assert server._env_flag("W7TLG_TEST_FLAG", False) is False


@pytest.mark.parametrize("raw", ["", "maybe", "ON!", "2", "disabled", "yeah"])
def test_flag_garbage_falls_back_to_default_and_warns(monkeypatch, caplog, raw):
    """A typo must never be read as "off" — the default wins and says so."""
    monkeypatch.setenv("W7TLG_TEST_FLAG", raw)
    with caplog.at_level("WARNING"):
        assert server._env_flag("W7TLG_TEST_FLAG", True) is True
    assert any("not a yes/no value" in r.message for r in caplog.records)


# ----------------------------------------------------------------------
# _env_int
# ----------------------------------------------------------------------

def test_int_valid_value(monkeypatch):
    monkeypatch.setenv("W7TLG_TEST_INT", "25")
    assert server._env_int("W7TLG_TEST_INT", 5, 1, 1000) == 25


def test_int_unset_uses_default(monkeypatch):
    monkeypatch.delenv("W7TLG_TEST_INT", raising=False)
    assert server._env_int("W7TLG_TEST_INT", 5, 1, 1000) == 5


@pytest.mark.parametrize("raw", ["", "fast", "5ms", "5.5", "-", "1e3"])
def test_int_garbage_falls_back_to_default(monkeypatch, caplog, raw):
    monkeypatch.setenv("W7TLG_TEST_INT", raw)
    with caplog.at_level("WARNING"):
        assert server._env_int("W7TLG_TEST_INT", 5, 1, 1000) == 5
    assert any("not a number" in r.message for r in caplog.records)


@pytest.mark.parametrize("raw", ["0", "-5", "1001", "999999"])
def test_int_out_of_range_falls_back_to_default(monkeypatch, caplog, raw):
    """A 0ms poll would be a busy loop against the rig link; an absurdly
    large one would quietly make the watchdog useless without saying so."""
    monkeypatch.setenv("W7TLG_TEST_INT", raw)
    with caplog.at_level("WARNING"):
        assert server._env_int("W7TLG_TEST_INT", 5, 1, 1000) == 5
    assert any("outside 1-1000" in r.message for r in caplog.records)


# ----------------------------------------------------------------------
# Defaults, as shipped
# ----------------------------------------------------------------------

def test_defaults_are_monitor_on_at_5ms(monkeypatch):
    """Shipped default must stay ON. The watchdog is removed only on the
    strength of a hardware test, not by an .env file drifting."""
    monkeypatch.delenv("FAST_PTT_MONITOR", raising=False)
    monkeypatch.delenv("FAST_PTT_POLL_MS", raising=False)
    reloaded = importlib.reload(server)
    try:
        assert reloaded.FAST_PTT_MONITOR_ENABLED is True
        assert reloaded._FAST_PTT_POLL_MS == 5
    finally:
        importlib.reload(server)


def test_env_can_turn_the_monitor_off_and_slow_the_poll(monkeypatch):
    monkeypatch.setenv("FAST_PTT_MONITOR", "off")
    monkeypatch.setenv("FAST_PTT_POLL_MS", "25")
    reloaded = importlib.reload(server)
    try:
        assert reloaded.FAST_PTT_MONITOR_ENABLED is False
        assert reloaded._FAST_PTT_POLL_MS == 25
    finally:
        monkeypatch.delenv("FAST_PTT_MONITOR", raising=False)
        monkeypatch.delenv("FAST_PTT_POLL_MS", raising=False)
        importlib.reload(server)


# ----------------------------------------------------------------------
# Nothing safety-related depends on the watchdog
# ----------------------------------------------------------------------

def test_amp_guards_do_not_reference_the_fast_monitor():
    """The claim the README makes, pinned: AcomBridge decides TX from the
    rig poll and the amp's own telemetry. If a guard ever started reading
    dashboard.server's watchdog state, turning the watchdog off would
    quietly weaken an interlock."""
    from pathlib import Path
    bridge_src = Path("amplifier/acom_bridge.py").read_text()
    assert "fast_ptt" not in bridge_src.lower()
    assert "dashboard.server" not in bridge_src
    assert "FAST_PTT_MONITOR" not in bridge_src


def test_rig_client_tx_freeze_does_not_reference_the_fast_monitor():
    from pathlib import Path
    rig_src = Path("rig/rigctld_client.py").read_text()
    assert "fast_ptt" not in rig_src.lower()
    assert "dashboard.server" not in rig_src
