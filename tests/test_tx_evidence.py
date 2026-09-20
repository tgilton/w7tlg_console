"""Item 2, part 1: the shared multi-source "is it transmitting?" check.

Every TX guard in acom_bridge defends against a false POSITIVE PTT. The
dangerous failure is the opposite — PTT reading RX while RF is live, which
is what lets cmd_select_band reach the amp's relays mid-transmission
(TX_GATING_AUDIT.md section 5, row A1). tx_evidence() is an OR over
sources that fail independently: the rigctld PTT reading, and three
readings off the amp's own serial link.

One-directional by design: any source saying TX wins, none can veto.

This file covers the predicate alone. The call sites that use it are a
separate commit.
"""
import amplifier.acom_bridge as ab
from amplifier.acom_bridge import AcomBridge
from amplifier.acom_protocol import AmpTelemetry


def _frame(**kw) -> AmpTelemetry:
    base = dict(mode=0x60, mode_name="OPR/RX", hv1_v=48.0, flag_keyin=False,
                fwd_power_w=0.0, input_power_w=0.0)
    base.update(kw)
    return AmpTelemetry(**base)


def _expire_tx_hang(bridge):
    """Wind past the predicate's release-only hang time — see
    TX_HANG_TIME_S. Real code waits; tests move the clock."""
    if bridge._tx_last_asserted_at is not None:
        bridge._tx_last_asserted_at -= ab.TX_HANG_TIME_S + 0.1


async def _bridge(fake_rig, fake_amp) -> AcomBridge:
    """Bridge with a connected amp and one fresh telemetry frame, so the
    amp-derived sources are eligible to be consulted at all."""
    bridge = AcomBridge(rig=fake_rig, amp=fake_amp)
    await bridge.start()
    await fake_amp.simulate_connect(True)
    await fake_amp.emit_telemetry(_frame())
    return bridge


# ----------------------------------------------------------------------
# Each source on its own
# ----------------------------------------------------------------------

async def test_quiet_station_is_not_transmitting(fake_rig, fake_amp):
    bridge = await _bridge(fake_rig, fake_amp)
    assert bridge.tx_evidence() == ()
    assert bridge.is_transmitting() is False


async def test_rig_ptt_alone_asserts_tx(fake_rig, fake_amp):
    bridge = await _bridge(fake_rig, fake_amp)
    await fake_rig.push_state(ptt=True)
    assert bridge.tx_evidence() == ("rig-ptt",)
    assert bridge.is_transmitting() is True


async def test_amp_keyin_alone_asserts_tx_even_with_ptt_reading_rx(
        fake_rig, fake_amp):
    """The case the whole item exists for: rigctld says RX, the amp says
    it is keyed. RF is live and the console must know it."""
    bridge = await _bridge(fake_rig, fake_amp)
    await fake_amp.emit_telemetry(_frame(flag_keyin=True))
    assert fake_rig.state.ptt is False
    assert bridge.tx_evidence() == ("amp-keyin",)
    assert bridge.is_transmitting() is True


async def test_forward_power_alone_asserts_tx(fake_rig, fake_amp):
    bridge = await _bridge(fake_rig, fake_amp)
    await fake_amp.emit_telemetry(_frame(fwd_power_w=230.0))
    assert bridge.tx_evidence() == ("amp-fwd-power",)
    assert bridge.is_transmitting() is True


async def test_drive_power_alone_asserts_tx(fake_rig, fake_amp):
    bridge = await _bridge(fake_rig, fake_amp)
    await fake_amp.emit_telemetry(_frame(input_power_w=6.1))
    assert bridge.tx_evidence() == ("amp-drive-power",)
    assert bridge.is_transmitting() is True


async def test_all_sources_are_reported_together(fake_rig, fake_amp):
    bridge = await _bridge(fake_rig, fake_amp)
    await fake_rig.push_state(ptt=True)
    await fake_amp.emit_telemetry(
        _frame(flag_keyin=True, fwd_power_w=230.0, input_power_w=6.1))
    assert bridge.tx_evidence() == (
        "rig-ptt", "amp-keyin", "amp-fwd-power", "amp-drive-power")


# ----------------------------------------------------------------------
# Freshness gating — the failure mode the OR would otherwise introduce
# ----------------------------------------------------------------------

async def test_stale_telemetry_does_not_latch_tx_forever(fake_rig, fake_amp):
    """A serial link that freezes mid-transmission leaves flag_keyin set in
    the last frame received. Without a freshness bound that would pin
    TX-true for the rest of the session and block every band select,
    antenna move and drive clamp."""
    bridge = await _bridge(fake_rig, fake_amp)
    await fake_amp.emit_telemetry(_frame(flag_keyin=True, fwd_power_w=230.0))
    assert bridge.is_transmitting() is True

    # Nothing new arrives for longer than the freshness window.
    bridge._last_telemetry_at -= ab.AMP_TELEMETRY_FRESH_S + 0.5
    assert bridge.tx_evidence() == ("tx-hang",)   # hang, not latch
    _expire_tx_hang(bridge)

    assert bridge.tx_evidence() == ()
    assert bridge.is_transmitting() is False


async def test_disconnected_amp_is_not_consulted(fake_rig, fake_amp):
    bridge = await _bridge(fake_rig, fake_amp)
    await fake_amp.emit_telemetry(_frame(flag_keyin=True))
    assert bridge.is_transmitting() is True

    await fake_amp.simulate_connect(False)
    _expire_tx_hang(bridge)

    assert bridge.tx_evidence() == ()
    assert bridge.is_transmitting() is False


async def test_stale_amp_never_suppresses_the_rig_reading(fake_rig, fake_amp):
    """Freshness gates the amp sources only — it must never be able to turn
    a live rig-ptt reading into "not transmitting"."""
    bridge = await _bridge(fake_rig, fake_amp)
    await fake_rig.push_state(ptt=True)
    bridge._last_telemetry_at -= ab.AMP_TELEMETRY_FRESH_S + 0.5
    await fake_amp.simulate_connect(False)

    assert bridge.tx_evidence() == ("rig-ptt",)
    assert bridge.is_transmitting() is True


async def test_amp_sources_unavailable_before_any_telemetry(fake_rig, fake_amp):
    bridge = AcomBridge(rig=fake_rig, amp=fake_amp)
    await bridge.start()
    await fake_amp.simulate_connect(True)
    bridge.station.amp_ptt_active = True   # as if a field were set by hand
    assert bridge._last_telemetry_at is None
    assert bridge.tx_evidence() == ()


# ----------------------------------------------------------------------
# Logging: on change only, naming the source
# ----------------------------------------------------------------------

async def test_tx_true_is_logged_once_not_per_frame(fake_rig, fake_amp, caplog):
    bridge = await _bridge(fake_rig, fake_amp)

    with caplog.at_level("INFO"):
        for _ in range(5):
            await fake_amp.emit_telemetry(_frame(flag_keyin=True, fwd_power_w=230.0))

    rising = [r for r in caplog.records if "TX true" in r.message]
    assert len(rising) == 1, "must log the edge, not every telemetry frame"
    assert "amp-keyin" in rising[0].message
    assert "amp-fwd-power" in rising[0].message


async def test_tx_false_names_every_source_that_spoke(fake_rig, fake_amp, caplog):
    bridge = await _bridge(fake_rig, fake_amp)
    await fake_rig.push_state(ptt=True)

    with caplog.at_level("INFO"):
        # Drive only registers on the loud frames, so the union across the
        # transmission is richer than any single frame.
        await fake_amp.emit_telemetry(_frame(flag_keyin=True))
        await fake_amp.emit_telemetry(_frame(flag_keyin=True, fwd_power_w=230.0,
                                             input_power_w=6.1))
        await fake_rig.push_state(ptt=False)
        await fake_amp.emit_telemetry(_frame())
        _expire_tx_hang(bridge)
        await fake_amp.emit_telemetry(_frame())

    falling = [r for r in caplog.records if "TX false" in r.message]
    assert len(falling) == 1
    for expected in ("rig-ptt", "amp-keyin", "amp-fwd-power", "amp-drive-power"):
        assert expected in falling[0].message


async def test_source_set_changing_mid_tx_does_not_relog(fake_rig, fake_amp, caplog):
    """fwd_w crosses zero on the ramps within a single transmission. Only
    the boolean edge is an event; the source set shifting is not."""
    bridge = await _bridge(fake_rig, fake_amp)

    with caplog.at_level("INFO"):
        await fake_amp.emit_telemetry(_frame(flag_keyin=True))
        await fake_amp.emit_telemetry(_frame(flag_keyin=True, fwd_power_w=230.0))
        await fake_amp.emit_telemetry(_frame(flag_keyin=True))

    assert len([r for r in caplog.records if "TX true" in r.message]) == 1
    assert len([r for r in caplog.records if "TX false" in r.message]) == 0


async def test_sources_seen_do_not_leak_into_the_next_transmission(
        fake_rig, fake_amp, caplog):
    bridge = await _bridge(fake_rig, fake_amp)
    await fake_amp.emit_telemetry(_frame(flag_keyin=True, fwd_power_w=230.0))
    await fake_amp.emit_telemetry(_frame())
    _expire_tx_hang(bridge)
    await fake_amp.emit_telemetry(_frame())

    with caplog.at_level("INFO"):
        await fake_rig.push_state(ptt=True)
        await fake_rig.push_state(ptt=False)
        _expire_tx_hang(bridge)
        await fake_amp.emit_telemetry(_frame())

    falling = [r for r in caplog.records if "TX false" in r.message]
    assert len(falling) == 1
    assert "rig-ptt" in falling[0].message
    assert "amp-fwd-power" not in falling[0].message


# ----------------------------------------------------------------------
# Hang time — the envelope problem
# ----------------------------------------------------------------------

async def test_envelope_flicker_does_not_drop_tx_mid_burst(fake_rig, fake_amp):
    """The SSB voice case, and not only that case.

    fwd_w and drive_w follow the RF envelope: they fall toward zero between
    words in SSB while flag_keyin stays on for the whole PTT. Measured on
    the 2026-09-19 DATA-mode logs, forward power already reads zero for up
    to 5 consecutive frames (~500ms) inside one transmission, on the ramps.

    Here forward power is the ONLY source — rig PTT reads RX (desync) and
    flag_keyin is false — and it flickers on/off every ~100ms. TX must stay
    true through the whole burst.
    """
    bridge = await _bridge(fake_rig, fake_amp)
    assert fake_rig.state.ptt is False

    for i in range(20):                      # ~2s of 100ms frames
        on = (i % 2 == 0)
        await fake_amp.emit_telemetry(
            _frame(flag_keyin=False, fwd_power_w=230.0 if on else 0.0))
        assert bridge.is_transmitting() is True, f"dropped TX at frame {i}"
        if not on:
            # During a gap the hang is what is holding it, by name.
            assert bridge.tx_evidence() == ("tx-hang",)
        else:
            assert bridge.tx_evidence() == ("amp-fwd-power",)


async def test_tx_releases_about_a_second_after_the_last_nonzero_reading(
        fake_rig, fake_amp):
    bridge = await _bridge(fake_rig, fake_amp)
    await fake_amp.emit_telemetry(_frame(fwd_power_w=230.0))
    assert bridge.is_transmitting() is True

    # Quiet frames keep arriving, but nothing asserts any more.
    await fake_amp.emit_telemetry(_frame())
    assert bridge.is_transmitting() is True          # still inside the hang

    # Just short of the window: still held.
    bridge._tx_last_asserted_at -= ab.TX_HANG_TIME_S - 0.1
    assert bridge.is_transmitting() is True

    # Just past it: released.
    bridge._tx_last_asserted_at -= 0.2
    assert bridge.is_transmitting() is False
    assert bridge.tx_evidence() == ()


async def test_hang_time_extends_tx_but_never_starts_it(fake_rig, fake_amp):
    """Release-only. A station that has never transmitted must not read TX,
    and the hang must not resurrect one that already released."""
    bridge = await _bridge(fake_rig, fake_amp)
    assert bridge._tx_last_asserted_at is None
    assert bridge.is_transmitting() is False

    await fake_amp.emit_telemetry(_frame(fwd_power_w=230.0))
    _expire_tx_hang(bridge)
    await fake_amp.emit_telemetry(_frame())
    assert bridge.is_transmitting() is False


async def test_hang_is_bounded_not_a_latch(fake_rig, fake_amp):
    """A source that stops for good releases after one second — the hang
    must not become the same failure as stale telemetry latching TX."""
    bridge = await _bridge(fake_rig, fake_amp)
    await fake_rig.push_state(ptt=True)
    await fake_rig.push_state(ptt=False)
    assert bridge.is_transmitting() is True

    _expire_tx_hang(bridge)

    assert bridge.is_transmitting() is False
