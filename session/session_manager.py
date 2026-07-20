"""
Session Manager — coordinated switch between operating "sessions"
(SSB, FT8/WSJT-X, and later VARA HF/Winlink).

Exists because nothing today coordinates the radio's CAT mode, the SDR's
voice/digital audio profile, and whichever external digital-mode app owns
the radio's audio devices and rigctld client slot — and that gap already
caused a real incident: an orphaned rigctld from another app's helper
silently hijacked the radio's serial port for hours before anyone
noticed. A session switch is the single coordinated, verified action that
replaces manually juggling all three: quit whatever app belongs to a
different session, set the right CAT mode (and, for profiles that need
it, the DATA-U known-good DSP baseline), launch the right app, and
confirm nothing is left fighting over shared resources (port 4532) —
reporting live progress the whole time.

Structurally modeled on amplifier/antenna_ab_test.py's AntennaAbTest: same
status/on_status()/_publish() observer shape, same awaiting_*/asyncio.Event
confirm-step pattern, same "one clear error, then stop" failure handling.

Deliberately does NOT touch sdr.audio directly — the existing
on_rig_state_for_audio_mode edge-trigger (dashboard/server.py) already
flips the SDR's voice/digital profile off the rig's own reported CAT
mode. Setting rig_mode here is enough; that machinery fires on its own.

Deliberately does NOT touch amplifier.acom_bridge.OperatingMode
(AMP_ON/AMP_OFF) — that's a separate, manually-controlled amp-safety
state machine and stays fully out of scope here.
"""

import asyncio
import logging
import time
from typing import Callable, Coroutine, Optional

from session.session_profiles import PROFILES, SessionProfile
from wsjtx.protocol import Status as WsjtxStatus
from wsjtx.udp_listener import WsjtxListener

logger = logging.getLogger(__name__)

StatusCallback = Callable[[dict], Coroutine]

_SUBPROCESS_TIMEOUT_S = 10.0
_LIVENESS_TIMEOUT_S = 20.0
_LIVENESS_POLL_S = 0.5
_WSJTX_STALE_S = 15.0   # mirrors console.html's WsjtxLink WSJTX_STALE_MS


class SessionManager:

    def __init__(self, bridge, sdr, wsjtx_listener: WsjtxListener):
        self.bridge = bridge
        self.sdr = sdr
        self.wsjtx_listener = wsjtx_listener

        self.status = "idle"                          # idle | switching | done | error
        self.current_session_id: Optional[str] = None  # None until a switch actually completes
        self.target_session_id: Optional[str] = None
        self.step = ""
        self.error_message = ""
        self.awaiting_confirm: Optional[str] = None

        self._status_callbacks: list[StatusCallback] = []
        self._task: Optional[asyncio.Task] = None
        self._confirm_event: Optional[asyncio.Event] = None
        self._wsjtx_last_seen: Optional[float] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_status(self, cb: StatusCallback):
        self._status_callbacks.append(cb)

    async def _publish(self):
        status = self.get_status()
        for cb in self._status_callbacks:
            try:
                await cb(status)
            except Exception as e:
                logger.error(f"Session status callback error: {e}")

    def get_status(self) -> dict:
        return {
            "status": self.status,
            "current_session_id": self.current_session_id,
            "target_session_id": self.target_session_id,
            "step": self.step,
            "error": self.error_message,
            "awaiting_confirm": self.awaiting_confirm,
            "profiles": [
                {"id": p.id, "name": p.name, "app_display_name": p.app_display_name}
                for p in PROFILES.values()
            ],
        }

    async def switch(self, target_id: str) -> tuple[bool, str]:
        if target_id not in PROFILES:
            return False, f"Unknown session: {target_id}"
        if self.status == "switching":
            return False, "A session switch is already in progress"
        if target_id == self.current_session_id:
            return False, f"Already in {PROFILES[target_id].name} session"
        ok, reason = self._ptt_ok()
        if not ok:
            return False, reason

        self.status = "switching"
        self.target_session_id = target_id
        self.error_message = ""
        self.step = ""
        self.awaiting_confirm = None
        self._task = asyncio.create_task(self._run(target_id))
        await self._publish()
        return True, f"Switching to {PROFILES[target_id].name}…"

    def confirm_quit(self) -> tuple[bool, str]:
        if self._confirm_event is None or self.awaiting_confirm is None:
            return False, "Not awaiting a quit confirmation"
        self._confirm_event.set()
        return True, "Confirmed"

    def wsjtx_is_live(self) -> bool:
        return (
            self._wsjtx_last_seen is not None
            and (time.monotonic() - self._wsjtx_last_seen) < _WSJTX_STALE_S
        )

    async def on_wsjtx_status(self, status: WsjtxStatus):
        """Registered on wsjtx_listener.on_status() in addition to the
        existing dashboard/server.py callback (that registration supports
        multiple callbacks already) — tracks our own liveness clock rather
        than reusing wsjtx_listener.connected, which is sticky/naive (set
        True on the first-ever packet, never reset except in .stop())."""
        self._wsjtx_last_seen = time.monotonic()

    # ------------------------------------------------------------------
    # Internal: PTT guard
    # ------------------------------------------------------------------

    def _ptt_ok(self) -> tuple[bool, str]:
        if self.bridge.rig.state.ptt:
            return False, "Cannot switch session while transmitting"
        return True, "ok"

    # ------------------------------------------------------------------
    # Internal: choreography
    # ------------------------------------------------------------------

    async def _run(self, target_id: str):
        target = PROFILES[target_id]
        outgoing = PROFILES.get(self.current_session_id) if self.current_session_id else None
        try:
            # Step 1: quit the outgoing app, if this switch changes which
            # app owns the audio devices/rigctld client slot.
            if outgoing and outgoing.app_bundle_id and outgoing.app_bundle_id != target.app_bundle_id:
                ok, reason = self._ptt_ok()
                if not ok:
                    return await self._fail(reason)

                if outgoing.quit_needs_confirm:
                    self.step = f"Waiting for confirmation to quit {outgoing.app_display_name}…"
                    await self._publish()
                    ok, reason = await self._await_confirm(outgoing.app_display_name)
                    if not ok:
                        return await self._fail(reason)

                self.step = f"Quitting {outgoing.app_display_name}…"
                await self._publish()
                ok, reason = await self._quit_app(outgoing.app_bundle_id)
                if not ok:
                    return await self._fail(f"Failed to quit {outgoing.app_display_name}: {reason}")

            # Step 2: set CAT mode (+ known-good DSP baseline if this
            # profile wants it).
            ok, reason = self._ptt_ok()
            if not ok:
                return await self._fail(reason)
            self.step = f"Setting rig mode → {target.rig_mode}…"
            await self._publish()
            ok = await self.bridge.rig.set_mode(target.rig_mode, target.passband_hz)
            if not ok:
                return await self._fail(f"Failed to set rig mode to {target.rig_mode}")

            if target.extra_rig_settings:
                self.step = "Applying known-good digital-mode baseline…"
                await self._publish()
                await self.bridge.rig.set_agc(2)          # FAST
                await self.bridge.rig.set_nb_on(False)
                await self.bridge.rig.set_dnf_on(False)
                await self.bridge.rig.set_preamp(0)       # IPO
                if self.sdr is not None and self.sdr.available:
                    self.sdr.audio.agc_mode = "fast"
                    self.sdr.audio.nr_enabled = False
                    self.sdr.audio.eq_enabled = False

            # Note: sdr.audio's voice/digital profile is deliberately not
            # touched here — dashboard/server.py's on_rig_state_for_audio_mode
            # fires on its own once the rig reports the new mode.

            # Step 3: launch the target app, if any.
            if target.app_bundle_id:
                ok, reason = self._ptt_ok()
                if not ok:
                    return await self._fail(reason)
                self.step = f"Launching {target.app_display_name}…"
                await self._publish()
                ok, reason = await self._launch_app(target.app_bundle_id)
                if not ok:
                    return await self._fail(f"Failed to launch {target.app_display_name}: {reason}")

                if target.liveness == "wsjtx_udp":
                    self.step = f"Waiting for {target.app_display_name}…"
                    await self._publish()
                    ok = await self._wait_for_wsjtx_liveness()
                    if not ok:
                        return await self._fail(
                            f"{target.app_display_name} launched but no UDP status "
                            f"received within {_LIVENESS_TIMEOUT_S:.0f}s")

            # Step 4: verify port 4532 didn't end up with a stray rigctld —
            # the exact failure mode that started this feature. Checked on
            # every switch, not just app-launching ones.
            self.step = "Checking rigctld…"
            await self._publish()
            ok, reason = await self._check_rigctld_port()
            if not ok:
                return await self._fail(reason)

            self.status = "done"
            self.current_session_id = target_id
            self.target_session_id = None
            self.step = ""
            await self._publish()

        except Exception as e:
            logger.exception("Session switch crashed")
            await self._fail(f"Unexpected error: {e}")

    async def _fail(self, reason: str):
        self.status = "error"
        self.error_message = reason
        self.target_session_id = None
        self.step = ""
        logger.warning(f"Session switch failed: {reason}")
        await self._publish()

    async def _await_confirm(self, label: str) -> tuple[bool, str]:
        self.awaiting_confirm = label
        self._confirm_event = asyncio.Event()
        await self._publish()
        try:
            while not self._confirm_event.is_set():
                ok, reason = self._ptt_ok()
                if not ok:
                    return False, reason
                try:
                    await asyncio.wait_for(self._confirm_event.wait(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue
            return True, "ok"
        finally:
            self.awaiting_confirm = None
            self._confirm_event = None
            await self._publish()

    async def _wait_for_wsjtx_liveness(self) -> bool:
        deadline = time.monotonic() + _LIVENESS_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.wsjtx_is_live():
                return True
            await asyncio.sleep(_LIVENESS_POLL_S)
        return self.wsjtx_is_live()

    # ------------------------------------------------------------------
    # Internal: app launch/quit — real timeouts, non-blocking by
    # construction (asyncio.create_subprocess_exec never touches the
    # event loop with a blocking call; see HANDOFF.md's SDR-startup-wedge
    # lesson for why this matters).
    # ------------------------------------------------------------------

    async def _launch_app(self, bundle_id: str) -> tuple[bool, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "open", "-b", bundle_id,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_SUBPROCESS_TIMEOUT_S)
            if proc.returncode != 0:
                return False, stderr.decode(errors="replace").strip() or f"exit {proc.returncode}"
            return True, "ok"
        except asyncio.TimeoutError:
            return False, f"timed out after {_SUBPROCESS_TIMEOUT_S:.0f}s"

    async def _quit_app(self, bundle_id: str) -> tuple[bool, str]:
        # AppleScript "quit" (not pkill) gives the app its normal shutdown
        # path — matters if a future profile's app has real unsaved state.
        # A non-zero return here commonly just means "wasn't running",
        # which isn't a failure worth blocking the switch over.
        script = f'tell application id "{bundle_id}" to quit'
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await asyncio.wait_for(proc.communicate(), timeout=_SUBPROCESS_TIMEOUT_S)
            return True, "ok"
        except asyncio.TimeoutError:
            return False, f"timed out after {_SUBPROCESS_TIMEOUT_S:.0f}s"

    async def _check_rigctld_port(self) -> tuple[bool, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "lsof", "-nP", "-iTCP:4532", "-sTCP:LISTEN",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except asyncio.TimeoutError:
            return False, "lsof check for port 4532 timed out"
        lines = [l for l in stdout.decode(errors="replace").splitlines()[1:] if l.strip()]
        if len(lines) != 1:
            return False, f"Expected exactly one process on :4532, found {len(lines)}"
        if "rigctld" not in lines[0]:
            return False, f"Unexpected process bound to :4532: {lines[0].split()[0]}"
        return True, "ok"
