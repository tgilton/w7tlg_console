"""Stage 0 of the UI redesign — the stale session bug.

SessionManager.current_session_id is set when a switch completes and was
never cleared by anything. Quitting WSJT-X with Cmd-Q left it reading
"ft8" forever, so switch("ft8") kept short-circuiting on "Already in FT8
(WSJT-X) session" and refused to relaunch, with the console's FT8 button
still lit (launching WSJT-X by hand worked — the console just would not do
it). Reported by Terry 2026-09-20.

The fix re-asks the profile's own liveness probe instead of trusting the
stored id, so "already in this session" now means "and its app is still
running". These tests drive the guard directly: _run is stubbed out
throughout, because what's under test is the decision switch() makes
before the choreography starts, not the choreography itself (which shells
out to `open -b` and lsof and has no business running in a test).
"""
from types import SimpleNamespace

import pytest

from session.session_manager import SessionManager
from session.session_profiles import PROFILES


@pytest.fixture
def manager(fake_rig, monkeypatch):
    """A SessionManager whose switch choreography is a no-op, so switch()'s
    own guard is the only thing exercised."""
    async def _noop_run(self, target_id):
        return

    monkeypatch.setattr(SessionManager, "_run", _noop_run)
    return SessionManager(
        bridge=SimpleNamespace(rig=fake_rig), sdr=None, wsjtx_listener=None)


def _mark_current(manager, session_id):
    """What a completed switch does (session_manager.py, end of _run)."""
    manager.current_session_id = session_id


# ── wsjtx_udp: the profile the bug was actually reported against ────────

async def test_dead_wsjtx_lets_ft8_be_selected_again(manager):
    """The bug. FT8 is the current session, WSJT-X has been Cmd-Q'd, so no
    UDP status has arrived inside the staleness window."""
    _mark_current(manager, "ft8")
    manager._wsjtx_last_seen = None   # never seen, or long since expired

    ok, reply = await manager.switch("ft8")

    assert ok is True
    assert "Switching to" in reply
    await manager._task


async def test_live_wsjtx_still_reports_already_in_session(manager, monkeypatch):
    """The guard must not simply be deleted — a double-click on a session
    that really is running still has to be rejected, or every stray click
    re-runs the whole switch (and re-applies the DATA-U baseline over
    whatever the operator has since tuned by hand)."""
    _mark_current(manager, "ft8")
    monkeypatch.setattr(manager, "wsjtx_is_live", lambda: True)

    ok, reply = await manager.switch("ft8")

    assert ok is False
    assert reply == "Already in FT8 (WSJT-X) session"
    assert manager.status == "idle"   # never started a switch


# ── js8call_tcp: same hole, same fix, different probe ───────────────────

async def test_dead_js8call_lets_js8_be_selected_again(manager, monkeypatch):
    async def _closed(self=None):
        return False

    monkeypatch.setattr(manager, "_js8call_api_is_open", _closed)
    _mark_current(manager, "js8")

    ok, _ = await manager.switch("js8")

    assert ok is True
    await manager._task


async def test_live_js8call_still_reports_already_in_session(manager, monkeypatch):
    async def _open(self=None):
        return True

    monkeypatch.setattr(manager, "_js8call_api_is_open", _open)
    _mark_current(manager, "js8")

    ok, reply = await manager.switch("js8")

    assert ok is False
    assert reply == "Already in JS8Call session"


# ── liveness "none": unchanged ──────────────────────────────────────────

async def test_ssb_keeps_todays_already_in_session_behavior(manager):
    """SSB owns no external app, so nothing can die behind the console's
    back and there is nothing to re-probe. Its guard must behave exactly as
    it did before the fix."""
    assert PROFILES["ssb"].liveness == "none"
    assert PROFILES["ssb"].app_bundle_id is None
    _mark_current(manager, "ssb")

    ok, reply = await manager.switch("ssb")

    assert ok is False
    assert reply == "Already in SSB session"
    assert manager.status == "idle"


# ── _app_is_live itself ─────────────────────────────────────────────────

async def test_app_is_live_dispatches_on_the_profiles_own_liveness(manager, monkeypatch):
    """One definition of "live" per profile, shared with the launch-time
    wait — not a second, drifting copy."""
    monkeypatch.setattr(manager, "wsjtx_is_live", lambda: False)

    async def _open(self=None):
        return True

    monkeypatch.setattr(manager, "_js8call_api_is_open", _open)

    assert await manager._app_is_live(PROFILES["ft8"]) is False
    assert await manager._app_is_live(PROFILES["js8"]) is True
    assert await manager._app_is_live(PROFILES["ssb"]) is True


async def test_switching_to_a_different_session_is_unaffected(manager):
    """The guard only ever fires on target == current; a normal switch to
    another session must not consult liveness at all."""
    _mark_current(manager, "ft8")
    manager._wsjtx_last_seen = None

    ok, _ = await manager.switch("ssb")

    assert ok is True
    await manager._task
