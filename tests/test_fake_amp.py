"""Proves the fake amp works and matches AcomSerial's real call surface —
T0's own validation bar (see REFACTOR_PLAN.md's T0 section)."""
import inspect

from amplifier.acom_protocol import AmpTelemetry, FaultStatus
from amplifier.acom_serial import AcomSerial
from tests.fakes.amp import FakeAcomSerial


def _public_methods(cls):
    return {
        name: member
        for name, member in inspect.getmembers(cls)
        if not name.startswith("_")
        and (inspect.isfunction(member) or isinstance(member, property))
    }


def test_fake_matches_real_public_signatures():
    real_methods = _public_methods(AcomSerial)
    fake_methods = _public_methods(FakeAcomSerial)

    for name, real_member in real_methods.items():
        assert name in fake_methods, f"fake is missing AcomSerial.{name}"
        if isinstance(real_member, property):
            continue
        real_params = list(inspect.signature(real_member).parameters)
        fake_params = list(inspect.signature(fake_methods[name]).parameters)
        assert real_params == fake_params, (
            f"{name} signature mismatch: real={real_params} fake={fake_params}")


async def test_fake_amp_reports_telemetry(fake_amp):
    seen = []

    async def on_telemetry(t):
        seen.append(t)

    fake_amp.on_telemetry(on_telemetry)
    await fake_amp.simulate_connect(True)
    assert fake_amp.connected is True

    telemetry = AmpTelemetry(mode=2, mode_name="OPR", hv1_v=48.0, flag_keyin=True)
    await fake_amp.emit_telemetry(telemetry)
    assert seen == [telemetry]


async def test_fake_amp_reports_fault(fake_amp):
    seen = []

    async def on_fault(f):
        seen.append(f)

    fake_amp.on_fault(on_fault)
    fault = FaultStatus(hard_faults=["OUTPUT RELAY OPEN SHOULD BE CLOSED"])
    await fake_amp.emit_fault(fault)
    assert seen == [fault]
    assert seen[0].has_hard_fault is True


async def test_send_fails_when_not_connected(fake_amp):
    ok = await fake_amp.send(b"\x00")
    assert ok is False
    assert fake_amp.sent_frames == []


async def test_send_records_frame_when_connected(fake_amp):
    await fake_amp.simulate_connect(True)
    ok = await fake_amp.send(b"\xAA\xBB")
    assert ok is True
    assert fake_amp.sent_frames == [b"\xAA\xBB"]
