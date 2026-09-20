"""Integration-level proof for T0: AcomBridge (the actual production
consumer of RigctldClient + AcomSerial, see amplifier/acom_bridge.py) can be
constructed and driven entirely against the fakes, with no live hardware.
Confirms the fakes' call signatures match real call sites, not just their
own standalone tests."""
from amplifier.acom_bridge import AcomBridge, OperatingMode


async def test_bridge_starts_and_stops_against_fakes(fake_rig, fake_amp):
    bridge = AcomBridge(rig=fake_rig, amp=fake_amp)

    await bridge.start()
    assert fake_rig.started is True
    assert fake_amp.started is True

    await bridge.stop()
    assert fake_rig.stopped is True
    assert fake_amp.stopped is True


async def test_bridge_reacts_to_fake_amp_telemetry(fake_rig, fake_amp):
    from amplifier.acom_protocol import AmpTelemetry

    bridge = AcomBridge(rig=fake_rig, amp=fake_amp)
    await bridge.start()

    # Raw telemetry mode byte 0x60 = "OPR/RX" (see parse_full_telemetry's
    # mode_map) — hv1_v is copied to station.amp_hv_v unconditionally,
    # independent of mode-sync hysteresis.
    telemetry = AmpTelemetry(mode=0x60, mode_name="OPR/RX", hv1_v=48.0)
    await fake_amp.emit_telemetry(telemetry)

    assert bridge.station.amp_hv_v == 48.0
