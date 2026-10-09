"""Session apps launch by path, not bundle id.

Upgrading WSJT-X from the gm5dna homebrew-tap build to the official
v3.3.0-beta1 changed its bundle id from "F6VY59P28F.org.ko3f.wsjtx" to
"org.k1jt.wsjtx", and the hardcoded old id left the FT8 button failing
with "LSCopyApplicationURLsForBundleIdentifier() failed" and un-lighting
itself. Reported by Terry 2026-10-09.

The profile now carries the app's path, `open` is given that path, and
the one place that still needs an id (the AppleScript quit) reads it from
the bundle's own Info.plist. Nothing here launches or quits a real app:
asyncio.create_subprocess_exec is replaced with a recorder throughout.
"""
import asyncio
import plistlib
from types import SimpleNamespace

import pytest

from session.session_manager import SessionManager
from session.session_profiles import PROFILES


class _FakeProc:
    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self):
        return b"", self._stderr


@pytest.fixture
def manager(fake_rig):
    return SessionManager(
        bridge=SimpleNamespace(rig=fake_rig), sdr=None, wsjtx_listener=None)


@pytest.fixture
def spawned(monkeypatch):
    """Every argv handed to asyncio.create_subprocess_exec, in order."""
    calls = []

    async def _fake_exec(*argv, **kwargs):
        calls.append(argv)
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    return calls


def _make_app(tmp_path, bundle_id, name="Fake.app"):
    app = tmp_path / name
    (app / "Contents").mkdir(parents=True)
    with open(app / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleIdentifier": bundle_id}, f)
    return str(app)


# ── launch ──────────────────────────────────────────────────────────────

async def test_launch_opens_the_app_by_path(manager, spawned, tmp_path):
    app = _make_app(tmp_path, "org.example.whatever")

    ok, reason = await manager._launch_app(app)

    assert (ok, reason) == (True, "ok")
    assert spawned == [("open", app)]


async def test_launch_does_not_care_what_the_bundle_id_is(manager, spawned, tmp_path):
    """The bug: the same app at the same path under a new id must still
    launch."""
    for bundle_id in ("F6VY59P28F.org.ko3f.wsjtx", "org.k1jt.wsjtx"):
        app = _make_app(tmp_path, bundle_id, name=f"{bundle_id}.app")
        ok, _ = await manager._launch_app(app)
        assert ok is True

    assert all("-b" not in argv for argv in spawned)


async def test_launch_of_a_missing_app_names_the_path_and_spawns_nothing(
        manager, spawned, tmp_path):
    missing = str(tmp_path / "Gone.app")

    ok, reason = await manager._launch_app(missing)

    assert ok is False
    assert missing in reason
    assert spawned == []


async def test_launch_reports_opens_own_error(manager, monkeypatch, tmp_path):
    async def _failing_exec(*argv, **kwargs):
        return _FakeProc(returncode=1, stderr=b"open said no\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _failing_exec)

    ok, reason = await manager._launch_app(_make_app(tmp_path, "org.example.x"))

    assert (ok, reason) == (False, "open said no")


# ── quit: the one remaining use of a bundle id ──────────────────────────

async def test_quit_reads_the_bundle_id_from_info_plist(manager, spawned, tmp_path):
    app = _make_app(tmp_path, "org.k1jt.wsjtx")

    ok, _ = await manager._quit_app(app)

    assert ok is True
    assert spawned == [
        ("osascript", "-e", 'tell application id "org.k1jt.wsjtx" to quit')]


async def test_quit_of_an_app_that_is_not_installed_is_not_a_failure(
        manager, spawned, tmp_path):
    """Same standing as "wasn't running" — it must not block the switch."""
    ok, _ = await manager._quit_app(str(tmp_path / "Gone.app"))

    assert ok is True
    assert spawned == []


# ── the profiles themselves ─────────────────────────────────────────────

def test_no_profile_carries_a_hardcoded_bundle_id():
    for profile in PROFILES.values():
        assert not hasattr(profile, "app_bundle_id")
        assert profile.app_path is None or profile.app_path.endswith(".app")
    assert PROFILES["ft8"].app_path == "/Applications/wsjtx.app"
