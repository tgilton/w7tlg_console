"""
Station Profile — QTH switching for a two-home operating setup.

W7TLG operates from two locations depending on season: La Quinta, CA in
winter, Boise, ID the rest of the year. Grid square and location affect
propagation queries (PSKReporter senderGrid), the AI advisor's location
context, and — per the operator — QSO confirmations and award tracking
(DXCC/WAS/grid awards are credited against the *actual* operating location
of each QSO, not a fixed home QTH).

This is a manual toggle, not auto-detected — the operator knows which
house they're in; a wrong auto-detection (e.g. from IP geolocation) would
silently corrupt award-tracking data, which is worse than requiring one
click.

IMPORTANT — this only affects w7tlg-console's own data (propagation
queries, AI advisor context, spot/seek award tracking once built).
RUMLogNG's own station/grid setting is separate and external; it does
NOT sync from this and must still be changed there by hand when QTH
changes. See w7tlg_console_integration memory notes.
"""

import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "station_profile.json"


@dataclass(frozen=True)
class StationProfile:
    id: str
    name: str            # display name, e.g. "La Quinta, CA (Winter)"
    grid: str             # Maidenhead grid square (6-char preferred)
    grid_precise: bool    # False = computed from city center, not the operator's exact GPS
    call: str
    city: str
    state: str            # US state/province code, "" if not applicable
    dxcc_entity: str = "United States"


# Boise grid (DN13WN) is confirmed from the console's own README/callsign records.
# La Quinta's 4-char field (DM13) matches the same value used consistently across
# this station's other tools (e.g. ft991a-panel, which targeted the same Coachella
# Valley region) but the 6-char subsquare below is only a city-center approximation
# — correct it once you have the shack's actual GPS-derived grid square.
PROFILES: dict[str, StationProfile] = {
    "la_quinta": StationProfile(
        id="la_quinta",
        name="La Quinta, CA (Winter)",
        grid="DM13up",
        grid_precise=False,
        call="W7TLG",
        city="La Quinta",
        state="CA",
    ),
    "boise": StationProfile(
        id="boise",
        name="Boise, ID",
        grid="DN13WN",
        grid_precise=True,
        call="W7TLG",
        city="Boise",
        state="ID",
    ),
}

DEFAULT_PROFILE_ID = "boise"


class StationProfileManager:
    """Holds the currently-selected profile, persisted across restarts."""

    def __init__(self):
        self._current_id: str = self._load() or DEFAULT_PROFILE_ID
        if self._current_id not in PROFILES:
            logger.warning(
                f"Persisted profile id {self._current_id!r} not found, "
                f"falling back to {DEFAULT_PROFILE_ID!r}"
            )
            self._current_id = DEFAULT_PROFILE_ID

    def _load(self) -> Optional[str]:
        try:
            if STATE_FILE.exists():
                data = json.loads(STATE_FILE.read_text())
                return data.get("current_profile_id")
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Could not load station profile state: {e}")
        return None

    def _save(self):
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps({"current_profile_id": self._current_id}))
        except OSError as e:
            logger.error(f"Could not persist station profile: {e}")

    @property
    def current(self) -> StationProfile:
        return PROFILES[self._current_id]

    def set_current(self, profile_id: str) -> StationProfile:
        if profile_id not in PROFILES:
            raise ValueError(f"Unknown station profile: {profile_id}")
        self._current_id = profile_id
        self._save()
        logger.info(f"Station profile switched to: {PROFILES[profile_id].name}")
        return self.current

    def list_profiles(self) -> list[StationProfile]:
        return list(PROFILES.values())

    def to_dict(self) -> dict:
        return {
            "current": asdict(self.current),
            "available": [asdict(p) for p in self.list_profiles()],
        }


# Module-level singleton — mirrors the pattern of other console-wide state
# (bridge/sdr in dashboard/server.py) rather than a class the caller must
# instantiate themselves.
station_profile = StationProfileManager()
