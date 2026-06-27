# W7TLG Station Startup Procedure

## Background — why this is complex

The ACOM 1200S has an internal ATU module with its own microcontroller. On cold
power-on, the ATU module boots separately from the amp's main board. The ATU
will not come out of bypass until the main board has told it which antenna is
selected and what frequency the radio is on.

In normal warm-start conditions the main board delivers this automatically and
the ATU initializes silently. On a **cold start** (first power-on of the day),
the ATU MCU takes longer to reach readiness — by the time it's listening, the
main board has already sent the initialization packet and moved on. The ATU then
sits in bypass with "ATU Unassigned" on the front panel.

**The fix (manual, until automated):** switching the amp's CAT setting from OFF
to RS232 forces the main board to retransmit the antenna + frequency
initialization packet to the ATU. Even with nothing connected to the RS232 port,
this one toggle is enough to wake the ATU module. CAT is then switched back to
OFF to clear the resulting CAT ERROR.

The console software prevents a secondary cause: it no longer sends
`cmd_select_band` until after the amp has fully initialized (first telemetry
received). Previously this early command also triggered "ATU Unassigned" by
asking the ATU to re-assign before it was ready.

---

## Equipment power sequence

**Always power on in this order.** RF source must never be live into an
uninitialized amp.

1. **ACOM 1200S** — rear power switch ON, wait for front panel to light red,
   then press front power button. Wait for "TEST" in CW and the amp to settle
   on STANDBY.
2. **FT-991A** — power on, set RF output to 10W initially.
3. **Mac / rigctld / console** — start software after hardware is ready.

**Shutdown order** is the reverse: software first, then radio, then amp (front
button to standby, rear switch off).

---

## Cold-start ATU wakeup procedure

Do this on the first start of the day, or any time the amp shows
"ATU Unassigned" / "Communication to ATU failing" on the front panel.

**Step 1 — start the console normally**

```
~/start_rigctld.sh
```
Then start the console. Watch the log. You should see within ~10 seconds:

```
Amp ready — syncing band to radio
ACOM WARNING: ATU POWER SWITCH ALARM AT POWER ON
```

`ATU POWER SWITCH ALARM AT POWER ON` is the ATU's relay feedback being noisy
on a cold start — this is normal and expected. If instead you see
`ATU NOT RESPONDING` or `AMP-ATU COMMUNICATION ERROR`, the ATU module did not
boot at all; wait 2 more minutes and try the CAT toggle below.

**Step 2 — CAT toggle on amp front panel**

On the ACOM 1200S front panel:

- Go to **Preferences → CAT → change from OFF to RS232**
- Go to **Preferences → ATU → enable / assign ATU**
  - The amp will attempt ATU initialization. You may briefly see "ATU
    communication" errors on the display — this is normal.
- The amp front panel will say something like:
  *"ATU will be in bypass until ANT and FREQ are selected"*
- Go to **Preferences → CAT → change back to OFF**

**Step 3 — give ATU its frequency and antenna context**

- Tune the radio to your operating frequency (e.g. 14.074 for 20m FT8).
- Key up for 2–3 seconds (use WSJT-X Tune button or radio's built-in tune
  tone). This gives the ATU the RF event it needs to select the frequency
  segment and load tune memory.
- On the amp front panel, select the antenna (e.g. A1F for the SS-25).
- The amp should now show normal STANDBY with no ATU errors.

**Step 4 — watch the console log clear**

```
ACOM cleared: ATU POWER SWITCH ALARM AT POWER ON
ACOM fault status: OK — all clear
```

A `CAT ERROR` warning may appear and persist for a few minutes after step 2 —
this is normal and clears on its own once the amp settles.

**Step 5 — switch console to AMP_ON and verify SWR**

- In the console, click **AMP_ON** and confirm.
- Key up for 2–3 seconds at 10W on your operating frequency.
- Check `amp_swr` in the console. Under 2.0 is good.
- If SWR is acceptable, bring power up to your normal operating level.

---

## Antenna notes

| Port | Antenna         | ATU needed? | Notes                              |
|------|-----------------|-------------|------------------------------------|
| A1F  | SS-25 Vertical  | No          | Feedpoint matcher handles 20/17/15 |
| A2F  | Unconnected     | —           | Disabled, do not use               |
| A3R  | 40m EFHW        | Yes         | High SWR on non-resonant bands without ATU — do not use amp without ATU |
| A4R  | Dummy Load      | No          | 10-second hard TX cutoff enforced by console |

---

## Console fault messages reference

| Message | Meaning | Action |
|---------|---------|--------|
| `ATU POWER SWITCH ALARM AT POWER ON` | ATU relay feedback noisy on cold start | Normal — usually clears after first TX or within a few minutes |
| `NO ANTENNA SETTINGS PREPARED` | ATU alive but has no tune memory for current freq/antenna | Run tune cycle (key up briefly) |
| `ATU NOT RESPONDING` | ATU module not communicating with main board | Do CAT toggle procedure (Step 2) |
| `AMP-ATU COMMUNICATION ERROR` | Same as above, from main board side | Do CAT toggle procedure (Step 2) |
| `CAT ERROR` | Amp expects RS232 CAT device, none connected | Normal after CAT toggle — clears on its own |
| `RF DETECTED AT WRONG TIME` | RF seen by amp while in STANDBY | Make sure console is in AMP_ON before transmitting |

---

## Future automation (pending)

The console captures the SETTINGS message (0x12) bytes at startup and after
each settings change. Once we have two snapshots — one with CAT=OFF and one
with CAT=RS232 — the console can send the toggle automatically at startup,
removing the need to touch the amp front panel. Watch the log for:

```
ACOM SETTINGS (0x12): XX XX XX ... — CAT/ATU config snapshot
```
