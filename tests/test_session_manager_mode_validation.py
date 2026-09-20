"""T1 follow-up — session/session_manager.py's rig-mode-setting call site
(SessionManager._run, around the "Setting rig mode" step) now also runs
Tier A (is_valid_hw_mode) before calling bridge.rig.set_mode, closing the
one write path T1 originally left unguarded. Every shipped SessionProfile
is valid today (confirmed separately against PROFILES), so this only ever
fires against a deliberately-misconfigured profile, exercised here via a
monkeypatched PROFILES entry rather than a real one."""
from types import SimpleNamespace

from rig.rigctld_client import is_valid_hw_mode
from session.session_manager import PROFILES, SessionManager
from session.session_profiles import PROFILES as REAL_PROFILES, SessionProfile


def _bad_profile() -> SessionProfile:
    return SessionProfile(
        id="badmode", name="Bad Mode Test", rig_mode="GARBAGE",
        passband_hz=0, app_bundle_id=None,
    )


async def test_switch_to_profile_with_invalid_rig_mode_is_rejected(fake_rig, monkeypatch):
    """Exits via the new Tier A guard, before _run ever reaches the
    subprocess-based app-launch/rigctld-port-check steps further down —
    exercising just the guarded call site, not the whole switch choreography."""
    monkeypatch.setitem(PROFILES, "badmode", _bad_profile())
    bridge = SimpleNamespace(rig=fake_rig)
    manager = SessionManager(bridge=bridge, sdr=None, wsjtx_listener=None)

    ok, _ = await manager.switch("badmode")
    assert ok is True  # switch() only reports "started"; the check runs inside _run
    await manager._task

    assert manager.status == "error"
    assert "invalid rig_mode" in manager.error_message
    assert fake_rig.calls == []  # never reached RigctldClient.set_mode


def test_every_shipped_session_profile_has_a_valid_rig_mode():
    """Confirms the new guard is a no-op for real profiles — it should only
    ever fire against a misconfigured one, never against what ships today."""
    for profile in REAL_PROFILES.values():
        assert is_valid_hw_mode(profile.rig_mode) is True, profile.rig_mode
