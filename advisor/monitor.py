"""
Monitor — propagation change detection and Claude-generated alert text.

Ported from ft991a-panel's monitor.py, same detection thresholds and
cooldown logic. The one adaptation: alert explanations reference the
operator's actual current QTH/grid (via station_profile) rather than a
hardcoded "Indio, CA (DM13)" — relevant now that this console switches
between two real locations (Task 7).
"""

from __future__ import annotations

import time
from typing import Optional

import anthropic

from config.station_profile import station_profile

MODEL = "claude-sonnet-4-5"

SPOT_JUMP_THRESHOLD = 50
SPOT_DROP_THRESHOLD = 50
KP_ALERT_THRESHOLD = 4.0
HIGH_BANDS = ["10m", "12m"]
ALERT_COOLDOWN = 900  # 15 minutes


class PropagationMonitor:
    def __init__(self):
        self._client = anthropic.Anthropic()
        self._previous_bands: dict = {}
        self._previous_kp: Optional[float] = None
        self._alert_cooldowns: dict = {}

    def _on_cooldown(self, key: str) -> bool:
        return (time.time() - self._alert_cooldowns.get(key, 0)) < ALERT_COOLDOWN

    def _set_cooldown(self, key: str):
        self._alert_cooldowns[key] = time.time()

    def detect_changes(self, new_bands: dict, new_kp: Optional[float]) -> list[dict]:
        alerts = []

        for band, new_data in new_bands.items():
            old_data = self._previous_bands.get(band, {})
            old_count = old_data.get("count", 0)
            new_count = new_data.get("count", 0)
            old_activity = old_data.get("activity", "dead")
            new_activity = new_data.get("activity", "dead")

            if band in HIGH_BANDS:
                if (old_activity in ["dead", "low", "unknown"]
                        and new_activity in ["moderate", "high"]
                        and not self._on_cooldown(f"highband_{band}")):
                    alerts.append({
                        "type": "highband", "band": band,
                        "old_count": old_count, "new_count": new_count,
                        "dxcc": new_data.get("dxcc", []),
                        "message": f"{band} is lighting up with {new_count} spots",
                    })
                    self._set_cooldown(f"highband_{band}")
                    continue

            gain = new_count - old_count
            if (gain >= SPOT_JUMP_THRESHOLD
                    and new_activity in ["moderate", "high"]
                    and old_activity in ["dead", "low", "unknown"]
                    and not self._on_cooldown(f"opening_{band}")):
                alerts.append({
                    "type": "opening", "band": band,
                    "old_count": old_count, "new_count": new_count,
                    "dxcc": new_data.get("dxcc", []),
                    "message": f"{band} opening — gained {gain} spots",
                })
                self._set_cooldown(f"opening_{band}")

            drop = old_count - new_count
            if (drop >= SPOT_DROP_THRESHOLD
                    and old_activity in ["moderate", "high"]
                    and new_activity in ["dead", "low"]
                    and not self._on_cooldown(f"closing_{band}")):
                alerts.append({
                    "type": "closing", "band": band,
                    "old_count": old_count, "new_count": new_count,
                    "message": f"{band} closing — lost {drop} spots",
                })
                self._set_cooldown(f"closing_{band}")

        if (new_kp is not None and self._previous_kp is not None
                and new_kp >= KP_ALERT_THRESHOLD and self._previous_kp < KP_ALERT_THRESHOLD
                and not self._on_cooldown("kp_spike")):
            alerts.append({
                "type": "kp_spike",
                "old_kp": self._previous_kp, "new_kp": new_kp,
                "message": f"Kp spiked to {new_kp:.1f} — geomagnetic disturbance",
            })
            self._set_cooldown("kp_spike")

        self._previous_bands = new_bands
        self._previous_kp = new_kp
        return alerts

    def explain_alert(self, alert: dict, prop_state: dict) -> str:
        """Synchronous — call via asyncio.to_thread from async code, same
        as ft991a-panel did via run_in_executor."""
        solar = prop_state.get("solar", {})
        bands = prop_state.get("bands", {})
        profile = station_profile.current

        active_bands = []
        for b in ["160m", "80m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m"]:
            bd = bands.get(b, {})
            if bd.get("count", 0) > 0:
                active_bands.append(f"{b}:{bd['count']}spots")

        context = f"""Propagation alert for {profile.call} ({profile.grid}, {profile.city}, {profile.state}):

Alert: {alert['message']}
Solar: SFI={solar.get('sfi')}, Kp={solar.get('kp')}
Active bands: {', '.join(active_bands)}
"""

        if alert["type"] in ["opening", "highband"]:
            dxcc = alert.get("dxcc", [])
            if dxcc:
                context += f"DXCC entities now heard on {alert['band']}: {', '.join(dxcc)}\n"
            context += (
                f"\nIn 2-3 sentences: explain why this opening is significant for DX from "
                f"{profile.city}, {profile.state}, what entities are reachable, and whether "
                f"{profile.call} should QSY there now."
            )
        elif alert["type"] == "closing":
            context += (
                f"\nIn 2-3 sentences: explain what caused {alert['band']} to close, "
                f"what to expect next, and which band to move to instead."
            )
        elif alert["type"] == "kp_spike":
            context += (
                f"\nIn 2-3 sentences: explain what this Kp spike means for HF conditions "
                f"from {profile.city}, {profile.state}, which bands are most affected, "
                f"and what strategy to use."
            )

        message = self._client.messages.create(
            model=MODEL,
            max_tokens=200,
            system="You are an expert amateur radio operator and HF propagation specialist. "
                   "Give brief, specific, actionable advice. No preamble.",
            messages=[{"role": "user", "content": context}],
        )
        return message.content[0].text


# Module-level singleton, matching the rest of the codebase's convention.
propagation_monitor = PropagationMonitor()
