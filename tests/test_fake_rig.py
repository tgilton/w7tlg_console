"""Proves the fake rig works and matches RigctldClient's real call surface —
T0's own validation bar (see REFACTOR_PLAN.md's T0 section)."""
import inspect

from rig.rigctld_client import RigctldClient
from tests.fakes.rig import FakeRigctldClient


def _public_methods(cls):
    return {
        name: member
        for name, member in inspect.getmembers(cls)
        if not name.startswith("_")
        and (inspect.isfunction(member) or isinstance(member, property))
    }


def test_fake_matches_real_public_signatures():
    """Every public method the real client exposes exists on the fake with
    the same parameter names — catches drift if either class changes."""
    real_methods = _public_methods(RigctldClient)
    fake_methods = _public_methods(FakeRigctldClient)

    for name, real_member in real_methods.items():
        assert name in fake_methods, f"fake is missing RigctldClient.{name}"
        if isinstance(real_member, property):
            continue
        real_params = list(inspect.signature(real_member).parameters)
        fake_params = list(inspect.signature(fake_methods[name]).parameters)
        assert real_params == fake_params, (
            f"{name} signature mismatch: real={real_params} fake={fake_params}")


async def test_fake_rig_reports_frequency(fake_rig):
    await fake_rig.push_state(freq_hz=14_074_000, mode="PKTUSB")
    assert fake_rig.state.freq_hz == 14_074_000
    assert fake_rig.state.mode == "PKTUSB"
    assert fake_rig.state.band != "??"  # update_derived ran


async def test_set_frequency_does_not_touch_state_until_polled(fake_rig):
    """Matches the real client: set_frequency only sends the command — the
    poll loop is the only writer of state.freq_hz."""
    ok = await fake_rig.set_frequency(7_074_000)
    assert ok is True
    assert fake_rig.state.freq_hz == 0
    assert fake_rig.calls == [("set_frequency", (7_074_000,), {})]


async def test_set_rf_power_updates_state_and_fires_callback(fake_rig):
    seen = []

    async def on_change(state):
        seen.append(state.rf_power_pct)

    fake_rig.on_state_change(on_change)
    ok = await fake_rig.set_rf_power(75)
    assert ok is True
    assert fake_rig.state.rf_power_pct == 75
    assert seen == [75]


async def test_set_ok_false_rejects_command(fake_rig):
    fake_rig.set_ok = False
    ok = await fake_rig.set_att(12)
    assert ok is False
    assert fake_rig.state.att_db == 0
