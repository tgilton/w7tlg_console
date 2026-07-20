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
    app_bundle_id: Optional[str]       # macOS bundle id for `open -b`; None = no external app (SSB)
    app_display_name: str = ""         # for progress messages, e.g. "Launching WSJT-X…"
    liveness: str = "none"             # "none" | "wsjtx_udp" — how to confirm the app actually came up
    quit_needs_confirm: bool = False   # real gate (see SessionManager) — unused by any profile yet
    extra_rig_settings: bool = False   # apply the DATA-U known-good baseline (AGC/NB/DNF/preamp/NR/EQ)


# WSJT-X bundle id confirmed live on this station via
# `mdls -name kMDItemCFBundleIdentifier /Applications/wsjtx.app` — this
# build is from the gm5dna/homebrew-amateur-radio tap, not a generic id.
PROFILES: dict[str, SessionProfile] = {
    "ssb": SessionProfile(
        id="ssb", name="SSB", rig_mode="USB", passband_hz=0,
        app_bundle_id=None,
    ),
    "ft8": SessionProfile(
        id="ft8", name="FT8 (WSJT-X)", rig_mode="PKTUSB", passband_hz=3000,
        app_bundle_id="F6VY59P28F.org.ko3f.wsjtx", app_display_name="WSJT-X",
        liveness="wsjtx_udp", extra_rig_settings=True,
    ),
}

# Adding a future session (e.g. VARA HF / MacWinlink) is one new
# SessionProfile entry here — no other structural change anywhere in the
# stack. The frontend renders its button group from PROFILES via the
# broadcast payload, not from hardcoded HTML.
