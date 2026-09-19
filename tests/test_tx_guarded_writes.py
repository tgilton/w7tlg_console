"""Item 2, part 2: the hardware writes that now use the shared TX check.

Covers TX_GATING_AUDIT.md section 5's rows A1/A2 (band select — the only
console->relay path), A3 (antenna cycle) and R1 (the drive clamp).

The point of each test below is the same: the rig says RX, the amp says RF
is live, and the write must not go out. That combination is the desync case
a rig-only guard cannot see, and A1 is the row where it reaches relays.
"""
from amplifier.acom_bridge import AcomBridge, OperatingMode
from amplifier.acom_protocol import AmpTelemetry, Band, cmd_select_band


def _frame(**kw) -> AmpTelemetry:
    base = dict(mode=0x60, mode_name="OPR/RX", hv1_v=48.0, flag_keyin=False,
                fwd_power_w=0.0, input_power_w=0.0)
    base.update(kw)
    return AmpTelemetry(**base)


def _keyed() -> AmpTelemetry:
    return _frame(mode=0x70, mode_name="OPR/TX", flag_keyin=True, fwd_power_w=230.0)


async def _ready_bridge(fake_rig, fake_amp, band="20m", freq=14_074_000):
    """Bridge with the amp connected and past its _amp_ready gate, parked on
    a known band so a later frequency move is a real band change."""
    bridge = AcomBridge(rig=fake_rig, amp=fake_amp)
    await bridge.start()
    await fake_amp.simulate_connect(True)
    await fake_rig.push_state(freq_hz=freq, band=band)
    await fake_amp.emit_telemetry(_frame())
    fake_amp.sent_frames.clear()
    return bridge


# ----------------------------------------------------------------------
# A1 — band select on frequency change
# ----------------------------------------------------------------------

async def test_band_select_goes_out_normally(fake_rig, fake_amp):
    bridge = await _ready_bridge(fake_rig, fake_amp)
    await fake_rig.push_state(freq_hz=7_074_000, band="40m")
    assert cmd_select_band(Band.B40M) in fake_amp.sent_frames


async def test_band_select_is_withheld_when_only_the_amp_says_tx(
        fake_rig, fake_amp):
    """The desync case. rigctld reads RX; the amp's own telemetry says it is
    keyed and making 230W. No relay command may go out."""
    bridge = await _ready_bridge(fake_rig, fake_amp)
    await fake_amp.emit_telemetry(_keyed())
    assert fake_rig.state.ptt is False
    assert bridge.is_transmitting() is True

    await fake_rig.push_state(freq_hz=7_074_000, band="40m")

    assert cmd_select_band(Band.B40M) not in fake_amp.sent_frames


async def test_withheld_band_select_is_not_lost(fake_rig, fake_amp):
    """It must not be dropped — the amp would be left on the old band while
    the radio has moved, which is the very thing the guard protects against."""
    bridge = await _ready_bridge(fake_rig, fake_amp)
    await fake_amp.emit_telemetry(_keyed())
    await fake_rig.push_state(freq_hz=7_074_000, band="40m")
    assert cmd_select_band(Band.B40M) not in fake_amp.sent_frames
    assert bridge._band_recheck_pending is True

    # TX ends, seen only on the amp's link — no rig state change at all.
    await fake_amp.emit_telemetry(_frame())

    assert cmd_select_band(Band.B40M) in fake_amp.sent_frames
    assert bridge._band_recheck_pending is False


async def test_withheld_band_select_lands_on_a_rig_state_update_too(
        fake_rig, fake_amp):
    bridge = await _ready_bridge(fake_rig, fake_amp)
    await fake_amp.emit_telemetry(_keyed())
    await fake_rig.push_state(freq_hz=7_074_000, band="40m")
    assert cmd_select_band(Band.B40M) not in fake_amp.sent_frames

    await fake_amp.emit_telemetry(_frame())        # amp goes quiet
    fake_amp.sent_frames.clear()
    bridge._band_recheck_pending = True            # as if still owed
    await fake_rig.push_state(mode="USB")          # any rig update

    assert bridge._band_recheck_pending is False


async def test_recheck_rereads_the_rig_rather_than_replaying_a_tx_reading(
        fake_rig, fake_amp):
    """A frequency read taken during TX can be the split TX VFO, so the
    deferred work is a re-check, not a replay of that value."""
    bridge = await _ready_bridge(fake_rig, fake_amp)
    await fake_amp.emit_telemetry(_keyed())
    # A bogus TX-time reading lands and is refused.
    await fake_rig.push_state(freq_hz=7_074_000, band="40m")
    assert bridge._band_recheck_pending is True
    # By the time TX ends the radio is really on 20m, where it started.
    fake_rig.state.freq_hz = 14_074_000
    fake_rig.state.band = "20m"

    await fake_amp.emit_telemetry(_frame())

    assert cmd_select_band(Band.B40M) not in fake_amp.sent_frames


# ----------------------------------------------------------------------
# A2 — the amp-ready band sync
# ----------------------------------------------------------------------

async def test_amp_ready_band_sync_is_deferred_and_then_sent(fake_rig, fake_amp):
    bridge = AcomBridge(rig=fake_rig, amp=fake_amp)
    await bridge.start()
    await fake_amp.simulate_connect(True)
    # Band resolves while receiving, but nothing is sent yet: _handle_freq_change
    # holds the select back until the amp's first telemetry frame arrives.
    await fake_rig.push_state(freq_hz=14_074_000, band="20m")
    assert bridge._current_acom_band == Band.B20M
    assert cmd_select_band(Band.B20M) not in fake_amp.sent_frames
    await fake_rig.push_state(ptt=True)

    # First telemetry frame crosses the _amp_ready gate while TX is up.
    await fake_amp.emit_telemetry(_frame())
    assert cmd_select_band(Band.B20M) not in fake_amp.sent_frames
    assert bridge._pending_band_select == Band.B20M

    await fake_rig.push_state(ptt=False)
    assert cmd_select_band(Band.B20M) in fake_amp.sent_frames
    assert bridge._pending_band_select is None


# ----------------------------------------------------------------------
# A3 — antenna cycle
# ----------------------------------------------------------------------

async def test_antenna_switch_refused_when_only_the_amp_says_tx(
        fake_rig, fake_amp):
    bridge = await _ready_bridge(fake_rig, fake_amp)
    await fake_amp.emit_telemetry(_keyed())
    assert fake_rig.state.ptt is False

    ok, reason = await bridge.next_antenna()

    assert ok is False
    assert "TX is active" in reason
    assert "amp-keyin" in reason
    assert fake_amp.sent_frames == []


async def test_goto_antenna_refused_when_only_the_amp_says_tx(fake_rig, fake_amp):
    bridge = await _ready_bridge(fake_rig, fake_amp)
    await fake_amp.emit_telemetry(_keyed())

    ok, reason = await bridge.goto_antenna(1, confirm_timeout_s=0.01,
                                            max_attempts_per_hop=1)

    assert ok is False
    assert "TX is active" in reason
    assert fake_amp.sent_frames == []


# ----------------------------------------------------------------------
# R1 — the drive clamp
# ----------------------------------------------------------------------

async def test_drive_clamp_deferred_when_only_the_amp_says_tx(
        fake_rig, fake_amp):
    bridge = await _ready_bridge(fake_rig, fake_amp)
    await fake_rig.push_state(rf_power_pct=50)
    await fake_amp.emit_telemetry(_keyed())

    # Three OPR-class frames drive the mode sync, which clamps.
    for _ in range(3):
        await fake_amp.emit_telemetry(_keyed())

    assert bridge._mode == OperatingMode.AMP_ON
    assert [c for c in fake_rig.calls if c[0] == "set_rf_power"] == []
    assert bridge._pending_drive_limit == 15

    await fake_amp.emit_telemetry(_frame())

    assert [c for c in fake_rig.calls if c[0] == "set_rf_power"] == [
        ("set_rf_power", (15,), {})]


async def test_stale_telemetry_does_not_block_writes_forever(fake_rig, fake_amp):
    """The freshness bound's whole purpose, exercised through a real write
    path: a frozen link stuck on a keyed frame must not veto band select."""
    bridge = await _ready_bridge(fake_rig, fake_amp)
    await fake_amp.emit_telemetry(_keyed())
    await fake_rig.push_state(freq_hz=7_074_000, band="40m")
    assert cmd_select_band(Band.B40M) not in fake_amp.sent_frames

    # Telemetry stops arriving; the last frame still says keyed.
    import amplifier.acom_bridge as ab
    bridge._last_telemetry_at -= ab.AMP_TELEMETRY_FRESH_S + 0.5
    await fake_rig.push_state(mode="USB")

    assert cmd_select_band(Band.B40M) in fake_amp.sent_frames
