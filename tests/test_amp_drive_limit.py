"""Item 1: the AMP ON drive clamp — new ceiling, and no CAT write mid-TX.

Covers TX_GATING_AUDIT.md section 5's one unguarded hardware write: the
telemetry-driven mode sync called rig.set_rf_power() with no PTT check, at
~10Hz, so a CAT `L RFPOWER` could land inside a live transmission.

The clamp is deferred rather than skipped because the mode-sync call site
only fires while _mode != AMP_ON and sets _mode itself — a plain skip would
silently drop the clamp and leave the rig high for the next transmission
too.
"""
import amplifier.acom_bridge as ab
from amplifier.acom_bridge import (
    AcomBridge, OperatingMode, AMP_ON_DRIVE_LIMIT, MODE_DRIVE_LIMITS,
)
from amplifier.acom_protocol import AmpTelemetry


def _opr_rx_frame() -> AmpTelemetry:
    return AmpTelemetry(mode=0x60, mode_name="OPR/RX", hv1_v=48.0, flag_keyin=False)


async def _drive_to_amp_on(fake_amp):
    """The 3 consecutive OPR-class frames the mode-sync hysteresis needs."""
    for _ in range(3):
        await fake_amp.emit_telemetry(_opr_rx_frame())


def _power_calls(fake_rig):
    return [c for c in fake_rig.calls if c[0] == "set_rf_power"]


# ----------------------------------------------------------------------
# The limits themselves
# ----------------------------------------------------------------------

def test_amp_on_limit_is_15_and_amp_off_is_unchanged():
    assert AMP_ON_DRIVE_LIMIT == 15
    assert MODE_DRIVE_LIMITS[OperatingMode.AMP_ON] == 15
    # Barefoot operation and amp-off calibration must be untouched.
    assert MODE_DRIVE_LIMITS[OperatingMode.AMP_OFF] == 100


async def test_drive_limit_w_published_for_amp_off_is_still_full_range(fake_rig, fake_amp):
    bridge = AcomBridge(rig=fake_rig, amp=fake_amp)
    await bridge.start()
    await fake_rig.push_state(freq_hz=14_074_000, band="20m")
    assert bridge.station.drive_limit_w == 100


# ----------------------------------------------------------------------
# Clamping while receiving
# ----------------------------------------------------------------------

async def test_clamps_to_15_when_amp_goes_to_operate(fake_rig, fake_amp):
    bridge = AcomBridge(rig=fake_rig, amp=fake_amp)
    await bridge.start()
    await fake_rig.push_state(rf_power_pct=50, ptt=False)

    await _drive_to_amp_on(fake_amp)

    assert bridge._mode == OperatingMode.AMP_ON
    assert _power_calls(fake_rig) == [("set_rf_power", (15,), {})]
    assert fake_rig.state.rf_power_pct == 15
    assert bridge.station.drive_limit_w == 15


async def test_no_write_when_already_at_or_below_the_limit(fake_rig, fake_amp):
    bridge = AcomBridge(rig=fake_rig, amp=fake_amp)
    await bridge.start()
    await fake_rig.push_state(rf_power_pct=10, ptt=False)

    await _drive_to_amp_on(fake_amp)

    assert bridge._mode == OperatingMode.AMP_ON
    assert _power_calls(fake_rig) == []
    assert fake_rig.state.rf_power_pct == 10


# ----------------------------------------------------------------------
# The actual fix: never write mid-transmission
# ----------------------------------------------------------------------

async def test_no_cat_write_while_transmitting(fake_rig, fake_amp):
    bridge = AcomBridge(rig=fake_rig, amp=fake_amp)
    await bridge.start()
    await fake_rig.push_state(rf_power_pct=50, ptt=True)

    await _drive_to_amp_on(fake_amp)

    # Mode sync still happened — only the hardware write is held back.
    assert bridge._mode == OperatingMode.AMP_ON
    assert _power_calls(fake_rig) == []
    assert fake_rig.state.rf_power_pct == 50
    assert bridge._pending_drive_limit == 15


async def test_deferred_clamp_lands_on_tx_end(fake_rig, fake_amp):
    bridge = AcomBridge(rig=fake_rig, amp=fake_amp)
    await bridge.start()
    await fake_rig.push_state(rf_power_pct=50, ptt=True)
    await _drive_to_amp_on(fake_amp)
    assert _power_calls(fake_rig) == []

    await fake_rig.push_state(ptt=False)

    assert _power_calls(fake_rig) == [("set_rf_power", (15,), {})]
    assert fake_rig.state.rf_power_pct == 15
    assert bridge._pending_drive_limit is None


async def test_deferred_clamp_lands_from_amp_telemetry_if_tx_end_is_missed(
        fake_rig, fake_amp):
    """A rigctld desync can swallow the PTT falling edge entirely, so the
    rig-side retry never fires. The amp's own 10Hz telemetry is on a
    separate serial link and must be able to land the clamp by itself."""
    bridge = AcomBridge(rig=fake_rig, amp=fake_amp)
    await bridge.start()
    await fake_rig.push_state(rf_power_pct=50, ptt=True)
    await _drive_to_amp_on(fake_amp)
    assert bridge._pending_drive_limit == 15

    # PTT drops with NO rig state callback — exactly what a desync looks
    # like from the bridge's side.
    fake_rig.state.ptt = False
    await fake_amp.emit_telemetry(_opr_rx_frame())

    assert _power_calls(fake_rig) == [("set_rf_power", (15,), {})]
    assert bridge._pending_drive_limit is None


async def test_stuck_ptt_warns_once_after_the_grace_period(
        fake_rig, fake_amp, monkeypatch, caplog):
    """PTT reading stuck true must not park the clamp silently forever."""
    bridge = AcomBridge(rig=fake_rig, amp=fake_amp)
    await bridge.start()
    await fake_rig.push_state(rf_power_pct=50, ptt=True)
    await _drive_to_amp_on(fake_amp)
    assert bridge._pending_drive_limit == 15

    # Still transmitting, but longer ago than the grace period.
    bridge._pending_drive_limit_since -= ab.PENDING_DRIVE_LIMIT_WARN_S + 1

    with caplog.at_level("WARNING"):
        await fake_amp.emit_telemetry(_opr_rx_frame())
        await fake_amp.emit_telemetry(_opr_rx_frame())

    warnings = [r for r in caplog.records
                if r.levelname == "WARNING" and "drive clamp was deferred" in r.message]
    assert len(warnings) == 1, "should warn once per deferral, not once per frame"
    assert _power_calls(fake_rig) == []
    assert bridge._pending_drive_limit == 15


async def test_failed_write_is_retried_not_lost(fake_rig, fake_amp):
    bridge = AcomBridge(rig=fake_rig, amp=fake_amp)
    await bridge.start()
    await fake_rig.push_state(rf_power_pct=50, ptt=False)

    fake_rig.set_ok = False
    await _drive_to_amp_on(fake_amp)
    assert bridge._pending_drive_limit == 15
    assert fake_rig.state.rf_power_pct == 50

    fake_rig.set_ok = True
    await fake_amp.emit_telemetry(_opr_rx_frame())
    assert fake_rig.state.rf_power_pct == 15
    assert bridge._pending_drive_limit is None


# ----------------------------------------------------------------------
# The operator-initiated site uses the same guard
# ----------------------------------------------------------------------

async def test_set_operating_mode_also_defers_while_transmitting(fake_rig, fake_amp):
    bridge = AcomBridge(rig=fake_rig, amp=fake_amp)
    await bridge.start()
    await fake_rig.push_state(freq_hz=14_074_000, band="20m", rf_power_pct=80, ptt=True)

    ok, _ = await bridge.set_operating_mode(OperatingMode.AMP_ON, confirmed=True)

    assert ok
    assert _power_calls(fake_rig) == []
    assert bridge._pending_drive_limit == 15
