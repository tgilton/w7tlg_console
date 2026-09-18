"""T2a: amp HV/mode-divergence cross-check + telemetry error_code fix.

Covers AUDIT.md Finding 3 (HIGH) — an OPR-class telemetry mode byte with a
collapsed HV rail during TX should trip TX inhibit even though no hard
fault bit fired — and Finding 5 (LOW) — parse_full_telemetry's
double-assignment of t.error_code/error_param, where the second write
(bytes 63-65) silently clobbered the first (bytes 60-62) and also collided
with data[63] already being used for fan_speed/active_lpf.
"""
from amplifier.acom_bridge import (
    AcomBridge, OperatingMode, HV_COLLAPSE_FRAMES, HV_COLLAPSE_THRESHOLD_V,
)
from amplifier.acom_protocol import AmpTelemetry, parse_full_telemetry


def _opr_tx_frame(hv1_v: float) -> AmpTelemetry:
    """Synthetic OPR/TX telemetry frame with a given HV rail voltage."""
    return AmpTelemetry(
        mode=0x70, mode_name="OPR/TX", hv1_v=hv1_v, flag_keyin=True,
    )


def _opr_rx_frame(hv1_v: float = 48.0) -> AmpTelemetry:
    """Synthetic OPR/RX (amp in OPERATE, not transmitting) frame."""
    return AmpTelemetry(
        mode=0x60, mode_name="OPR/RX", hv1_v=hv1_v, flag_keyin=False,
    )


async def _drive_to_amp_on(fake_amp, hv1_v: float = 48.0):
    """Feed the 3 consecutive OPR-class frames the bridge's own mode-sync
    hysteresis requires before it trusts AMP_ON (acom_bridge.py:626-638)."""
    for _ in range(3):
        await fake_amp.emit_telemetry(_opr_rx_frame(hv1_v))


async def test_hv_collapse_during_tx_inhibits_after_confirm_frames(fake_rig, fake_amp):
    bridge = AcomBridge(rig=fake_rig, amp=fake_amp)
    await bridge.start()
    await _drive_to_amp_on(fake_amp)
    assert bridge._mode == OperatingMode.AMP_ON
    assert bridge.station.tx_inhibited is False

    # Fewer than HV_COLLAPSE_FRAMES of collapsed HV must not yet trip —
    # mirrors the transient-filtering behavior of the mode-sync hysteresis.
    for _ in range(HV_COLLAPSE_FRAMES - 1):
        await fake_amp.emit_telemetry(_opr_tx_frame(hv1_v=0.0))
    assert bridge.station.tx_inhibited is False

    # The confirming frame should trip the inhibit.
    await fake_amp.emit_telemetry(_opr_tx_frame(hv1_v=0.0))
    assert bridge.station.tx_inhibited is True
    assert "HV" in bridge.station.tx_inhibit_reason


async def test_healthy_hv_during_tx_never_inhibits(fake_rig, fake_amp):
    bridge = AcomBridge(rig=fake_rig, amp=fake_amp)
    await bridge.start()
    await _drive_to_amp_on(fake_amp)

    for _ in range(10):
        await fake_amp.emit_telemetry(_opr_tx_frame(hv1_v=48.0))
    assert bridge.station.tx_inhibited is False


async def test_low_hv_without_keyin_does_not_inhibit(fake_rig, fake_amp):
    """AMP_ON with no TX in progress (flag_keyin False) is just the amp
    idling in OPERATE — the cross-check is TX-gated, not a general HV
    floor, per the audit's stated mitigation ("while AMP_ON and
    flag_keyin...")."""
    bridge = AcomBridge(rig=fake_rig, amp=fake_amp)
    await bridge.start()
    await _drive_to_amp_on(fake_amp, hv1_v=0.0)

    for _ in range(10):
        await fake_amp.emit_telemetry(_opr_rx_frame(hv1_v=0.0))
    assert bridge.station.tx_inhibited is False


async def test_hv_collapse_counter_resets_on_recovery(fake_rig, fake_amp):
    bridge = AcomBridge(rig=fake_rig, amp=fake_amp)
    await bridge.start()
    await _drive_to_amp_on(fake_amp)

    for _ in range(HV_COLLAPSE_FRAMES - 1):
        await fake_amp.emit_telemetry(_opr_tx_frame(hv1_v=0.0))
    # HV recovers before the counter confirms — must not carry over.
    await fake_amp.emit_telemetry(_opr_tx_frame(hv1_v=48.0))
    assert bridge._hv_collapse_frames == 0

    for _ in range(HV_COLLAPSE_FRAMES - 1):
        await fake_amp.emit_telemetry(_opr_tx_frame(hv1_v=0.0))
    assert bridge.station.tx_inhibited is False


async def test_hv_at_threshold_boundary_not_treated_as_collapsed(fake_rig, fake_amp):
    bridge = AcomBridge(rig=fake_rig, amp=fake_amp)
    await bridge.start()
    await _drive_to_amp_on(fake_amp)

    for _ in range(10):
        await fake_amp.emit_telemetry(
            _opr_tx_frame(hv1_v=HV_COLLAPSE_THRESHOLD_V))
    assert bridge.station.tx_inhibited is False


def _telemetry_frame(overrides: dict[int, int]) -> bytes:
    """68-byte 0x2F payload with every relevant byte zeroed except the
    ones under test, so unrelated struct.unpack_from calls don't error."""
    data = bytearray(68)
    for offset, value in overrides.items():
        data[offset] = value
    return bytes(data)


def test_error_code_reads_from_bytes_60_62_not_63_65():
    # error_code=0x00 at byte 60 (the "confirmed 0x00" real-data case), but
    # byte 63 carries a nonzero value for fan_speed/active_lpf — this used
    # to get reinterpreted as error_code by the second, dead-clobbering
    # assignment.
    data = _telemetry_frame({60: 0x00, 63: 0x86})
    t = parse_full_telemetry(data)
    assert t.error_code == 0x00
    assert t.fan_speed == 0x08
    assert t.active_lpf == 0x06


def test_error_code_nonzero_is_not_overwritten_by_fan_speed_byte():
    data = _telemetry_frame({60: 0x07, 63: 0x86})
    t = parse_full_telemetry(data)
    assert t.error_code == 0x07
    assert t.fan_speed == 0x08
    assert t.active_lpf == 0x06
