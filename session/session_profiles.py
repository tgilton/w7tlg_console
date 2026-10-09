"""
Session Profiles — data model for the operator's "which operating style am
I in right now" selector.

Distinct from the rig's own CAT mode (USB/LSB/CW/AM/PKTUSB/FM — see the
"Mode" button row in dashboard/console.html). A session bundles a CAT
mode together with which external digital-mode app (if any) owns the
radio's audio devices and rigctld client slot right now. Switching
sessions is what coordinates the handoff between them — see
session/session_manager.py.

Deliberately NOT persisted to disk, unlike config/station_profile.py's
QTH profile. "Current session" is live external-process state (is
WSJT-X actually running right now) — a value on disk from a prior run
could never be trusted without re-verifying it anyway, and a wrong
"remembered" session would show a lit button that lied about reality
after a restart or an operator quitting an app outside the console.
Every server/browser restart starts with no session selected; the
first click always runs the full switch choreography.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SessionProfile:
    id: str
    name: str                          # button label — never rewritten to show state (DESIGN.md §1)
    rig_mode: str                      # hamlib mode string, e.g. "USB", "PKTUSB"
    passband_hz: int                   # 0 = rig default
    app_path: Optional[str]            # .app bundle launched with `open`; None = no external app (SSB)
    app_display_name: str = ""         # for progress messages, e.g. "Launching WSJT-X…"
    liveness: str = "none"             # "none" | "wsjtx_udp" | "js8call_tcp" — how to confirm the app actually came up
    quit_needs_confirm: bool = False   # real gate (see SessionManager) — unused by any profile yet
    extra_rig_settings: bool = False   # apply the DATA-U known-good baseline (AGC/NB/DNF/preamp/NR/EQ)


# Where each session's app is installed on this station — the one thing to
# edit if an app moves. Apps are launched by path, not bundle id, because
# the id belongs to whoever built the app: the gm5dna/homebrew-amateur-radio
# tap build of WSJT-X was "F6VY59P28F.org.ko3f.wsjtx", the official
# v3.3.0-beta1 that replaced it 2026-10-08 is "org.k1jt.wsjtx", and the
# hardcoded old id left the FT8 button unable to launch anything. The path
# survived that upgrade unchanged. Where an id is still needed (quit), it
# is read from the bundle's own Info.plist — see SessionManager._quit_app.
WSJTX_APP_PATH = "/Applications/wsjtx.app"
JS8CALL_APP_PATH = "/Applications/JS8Call.app"

PROFILES: dict[str, SessionProfile] = {
    "ssb": SessionProfile(
        id="ssb", name="SSB", rig_mode="USB", passband_hz=0,
        app_path=None,
    ),
    "ft8": SessionProfile(
        id="ft8", name="FT8 (WSJT-X)", rig_mode="PKTUSB", passband_hz=3000,
        app_path=WSJTX_APP_PATH, app_display_name="WSJT-X",
        liveness="wsjtx_udp", extra_rig_settings=True,
    ),
    # liveness="js8call_tcp" probes JS8Call's own JSON API TCP port
    # (2442, NOT WSJT-X's UDP protocol — JS8Call also has a separate
    # "WSJTXProtocolEnabled" broadcast setting that mimics WSJT-X's wire
    # format, confirmed present and already ON in this station's
    # JS8Call.ini, but a live 18s capture on :2237 showed it emits
    # nothing on an idle radio — event-driven, not a WSJT-X-style
    # periodic Heartbeat — so it's not a usable "did the app come up"
    # signal). Requires "Enable TCP Server" checked in JS8Call's
    # File > Settings > Reporting > API section (127.0.0.1:2442, the
    # default port shown there even while disabled).
    "js8": SessionProfile(
        id="js8", name="JS8Call", rig_mode="PKTUSB", passband_hz=3000,
        app_path=JS8CALL_APP_PATH, app_display_name="JS8Call",
        liveness="js8call_tcp", extra_rig_settings=True,
    ),
}

# Adding a future session (e.g. VARA HF / MacWinlink) is one new
# SessionProfile entry here — no other structural change anywhere in the
# stack. The frontend renders its button group from PROFILES via the
# broadcast payload, not from hardcoded HTML.
