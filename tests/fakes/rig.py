"""Fake RigctldClient — same public call signatures as rig/rigctld_client.py's
RigctldClient, minus the real TCP connection to rigctld.

Behavior is matched method-by-method against the real class (confirmed by
reading rig/rigctld_client.py directly, not guessed): set_frequency/
set_mode/set_ptt only send a command and do NOT touch .state (the real rig
only learns the new value back on its next poll cycle) — everything else
(set_rf_power, set_preamp, set_att, set_if_shift, set_nb, set_nr,
set_mic_gain, set_comp, set_agc, set_nb_on, set_nr_on, set_dnf_on,
set_dt_gain, set_ssb_tx_bpf) updates .state immediately and fires the
registered state-change callbacks, exactly like the real client does after
a successful Hamlib 'L'/'U'/raw-EX write.

Use `push_state(**updates)` to simulate what a poll cycle would observe
(e.g. after a real radio confirms a frequency change), and `.calls` to
assert what was actually commanded.
"""
from __future__ import annotations

from typing import Optional

from rig.rigctld_client import RigState, StateCallback


class FakeRigctldClient:

    def __init__(
        self,
        host: str = '127.0.0.1',
        port: int = 4532,
        poll_interval: float = 0.5,
        reconnect_interval: float = 5.0,
    ):
        self.host = host
        self.port = port
        self.poll_interval = poll_interval
        self.reconnect_interval = reconnect_interval

        self.state = RigState()
        self._state_callbacks: list[StateCallback] = []
        self.started = False
        self.stopped = False

        # Every set_*/get_*/send_raw_cmd call, as (name, args, kwargs), in
        # the order made — assert against this to check "was this actually
        # commanded", independent of what .state ends up holding.
        self.calls: list[tuple[str, tuple, dict]] = []

        # Test hook: set False to make the next command report failure, the
        # same shape a real rigctld RPRT-rejected write produces.
        self.set_ok = True

    # ------------------------------------------------------------------
    # Public API — mirrors RigctldClient exactly
    # ------------------------------------------------------------------

    def on_state_change(self, cb: StateCallback):
        self._state_callbacks.append(cb)

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    # Standard VFO controls — real client does NOT update .state here;
    # the poll loop is the only writer of freq_hz/mode/ptt.
    async def set_frequency(self, freq_hz: int) -> bool:
        return self._record("set_frequency", freq_hz)

    async def set_mode(self, mode: str, passband: int = 0) -> bool:
        return self._record("set_mode", mode, passband)

    async def set_ptt(self, active: bool) -> bool:
        return self._record("set_ptt", active)

    # Level/func controls — real client updates .state + fires callbacks
    # immediately on a successful write.
    async def set_rf_power(self, pct: int) -> bool:
        return await self._record_and_apply("set_rf_power", (pct,), "rf_power_pct", pct)

    async def set_preamp(self, level: int) -> bool:
        ok = await self._record_and_apply("set_preamp", (level,), "preamp", level)
        if ok:
            self.state.update_derived()
        return ok

    async def set_att(self, db: int) -> bool:
        return await self._record_and_apply("set_att", (db,), "att_db", db)

    async def set_if_shift(self, hz: int) -> bool:
        return await self._record_and_apply("set_if_shift", (hz,), "if_shift_hz", hz)

    async def set_nb(self, level: float) -> bool:
        return await self._record_and_apply("set_nb", (level,), "nb_level", level)

    async def set_nr(self, level: float) -> bool:
        return await self._record_and_apply("set_nr", (level,), "nr_level", level)

    async def set_mic_gain(self, level: float) -> bool:
        return await self._record_and_apply("set_mic_gain", (level,), "mic_gain", level)

    async def set_comp(self, level: float) -> bool:
        return await self._record_and_apply("set_comp", (level,), "comp_level", level)

    async def set_agc(self, value: int) -> bool:
        return await self._record_and_apply("set_agc", (value,), "agc", value)

    async def set_nb_on(self, on: bool) -> bool:
        return await self._record_and_apply("set_nb_on", (on,), "nb_on", on)

    async def set_nr_on(self, on: bool) -> bool:
        return await self._record_and_apply("set_nr_on", (on,), "nr_on", on)

    async def set_dnf_on(self, on: bool) -> bool:
        return await self._record_and_apply("set_dnf_on", (on,), "dnf_on", on)

    # Raw CAT passthrough — pure reads/writes, no .state effect (matches
    # the real client: send_raw_cmd/get_dt_gain/get_ssb_tx_bpf never touch
    # self.state, only set_dt_gain/set_ssb_tx_bpf do).
    async def send_raw_cmd(self, cmd: str) -> Optional[str]:
        self._record("send_raw_cmd", cmd)
        return None

    async def get_dt_gain(self) -> Optional[int]:
        self._record("get_dt_gain")
        return self.state.dt_gain

    async def set_dt_gain(self, value: int) -> bool:
        return await self._record_and_apply("set_dt_gain", (value,), "dt_gain", value)

    async def get_ssb_tx_bpf(self) -> Optional[int]:
        self._record("get_ssb_tx_bpf")
        return self.state.ssb_tx_bpf

    async def set_ssb_tx_bpf(self, value: int) -> bool:
        return await self._record_and_apply("set_ssb_tx_bpf", (value,), "ssb_tx_bpf", value)

    # ------------------------------------------------------------------
    # Test helpers — not part of the real client's API
    # ------------------------------------------------------------------

    async def push_state(self, **updates):
        """Simulate a poll cycle observing new values (e.g. the radio's own
        confirmation of a frequency/mode/PTT change) and fire callbacks the
        way the real client's _poll_state/_poll_controls do."""
        for k, v in updates.items():
            setattr(self.state, k, v)
        self.state.update_derived()
        await self._fire_callbacks()

    async def _fire_callbacks(self):
        for cb in list(self._state_callbacks):
            await cb(self.state)

    def _record(self, name: str, *args, **kwargs) -> bool:
        self.calls.append((name, args, kwargs))
        return self.set_ok

    async def _record_and_apply(self, name: str, args: tuple, attr: str, value) -> bool:
        ok = self._record(name, *args)
        if ok:
            setattr(self.state, attr, value)
            await self._fire_callbacks()
        return ok
