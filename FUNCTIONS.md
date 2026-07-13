# FUNCTIONS.md — W7TLG Console

What every control does, how often it is used, and what hardware it talks to.

This is the operator knowledge no model has. It exists so that layout and
hierarchy decisions are inferences rather than guesses.

**Frequency** column drives visual weight and placement:

- `HOT` — touched constantly while operating. Must be reachable without
  scrolling, hunting, or a second click. Deserves size.
- `SESSION` — set once when starting on a band, then left alone.
- `RARE` — setup, calibration, diagnostics. May be collapsed or tucked away.
- `READ` — monitoring only, never touched. Should be scannable, not clickable.

**Path** is the hardware or service the control actually reaches.

---

## Header / global

| Control | Does what | Frequency | Path |
|---|---|---|---|
| Rig / Amp / SDR / WSJT-X dots | Connection status per subsystem | READ | all |
| Inhibit | ? | ? | ? |
| Allow | ? | ? | ? |
| Monitor ↗ | Opens ? | ? | ? |
| Propagation ↗ | Opens ? | ? | ? |
| Spot/Seek ↗ | Opens ? | ? | ? |
| Advisor ↗ | Opens AI band advisor | ? | ? |

> Inhibit/Allow appear to be the most consequential controls in the app and
> currently sit in the header at the same weight as four link buttons. Worth
> saying plainly what they gate.

---

## Left column — rig and DSP

### Mode

| Control | Does what | Frequency | Path |
|---|---|---|---|
| USB / LSB / DATA-U / CW / AM / FM | Sets rig mode | SESSION | FT-991A via rigctld |

### Filter

| Control | Does what | Frequency | Path |
|---|---|---|---|
| NAR / WID | ? | ? | FT-991A |
| Width slider (3000 Hz) | RX filter bandwidth | ? | FT-991A |

### Digital audio

| Control | Does what | Frequency | Path |
|---|---|---|---|
| DT GAIN | ? Digital tx gain? | ? | ? |

### SSB audio

| Control | Does what | Frequency | Path |
|---|---|---|---|
| MIC | Mic gain | ? | FT-991A |
| COMP | Speech compression | ? | FT-991A |
| TX BW — WIDE / RAG-CHEW / DX | TX bandwidth profile | ? | FT-991A |

### Noise reduction

| Control | Does what | Frequency | Path |
|---|---|---|---|
| NB | Noise blanker | ? | FT-991A |
| DNF | Digital notch filter | ? | FT-991A |
| DNR | Digital noise reduction | ? | FT-991A |
| DNR level (15) | DNR strength | ? | FT-991A |

### AGC / gain

| Control | Does what | Frequency | Path |
|---|---|---|---|
| AGC — OFF / FAST / SLOW | AGC time constant | SESSION | FT-991A |
| VOL (400%?) | ? Which audio path — rig AF, or SDR/DeepFilterNet? | ? | ? |
| Preamp — IPO / A1 / A2 | RX preamp stage | SESSION | FT-991A |
| ATT | RX attenuator — currently no control, only a value | ? | FT-991A |

> `VOL 400%` — is this a scaling bug, or a real 4× software gain? Flagged in
> DESIGN.md §10.

---

## Center — spectrum and tuning

| Control | Does what | Frequency | Path |
|---|---|---|---|
| VFO readout | Tuned frequency; click-to-edit | HOT | FT-991A |
| Band / mode / RX / FT8 / DIGI AUDIO pills | Status? Or clickable? | ? | ? |
| S-meter bar | RX signal strength | READ | ? |
| S9 Cal (-75) | Calibration offset for S-meter | RARE | ? |
| ALC / RADIO PO / SWR bars | TX telemetry | READ | FT-991A |
| Tune | ? Starts amp/tuner tune cycle? | ? | ACOM 06AT? |
| Whole Band | Zooms spectrum to full band | ? | SDR |
| Center | Centers spectrum on VFO | ? | SDR |
| Audio: Live | ? Toggles audio source? | ? | ? |
| SPAN | Spectrum width | ? | SDR |
| AUTO | Auto floor tracking | ? | SDR |
| FLOOR | Waterfall noise floor | ? | SDR |
| GAIN | Waterfall contrast | ? | SDR |
| AVG | Spectrum averaging | ? | SDR |
| Palette (Default) | Waterfall colormap | RARE | SDR |
| EQ — Bass / Mid / Treble | RX audio EQ | ? | ? DeepFilterNet path? |

---

## Right column — amplifier and antenna

### Amp (ACOM 1200S)

| Control | Does what | Frequency | Path |
|---|---|---|---|
| Standby | ? Indicator, or button? | ? | ACOM 1200S |
| Fwd P / Rev P / SWR / Drive P / PAM1 T | Amp telemetry | READ | ACOM 1200S |
| HV / I | Amp HV rail and current | READ | ACOM 1200S |
| Fault status | Active faults | READ | ACOM 1200S |
| AMP OFF / AMP ON | Amp operating mode | SESSION | ACOM 1200S |
| Exciter drive (6 W) | Drive level into amp | ? | FT-991A |

### Antenna

| Control | Does what | Frequency | Path |
|---|---|---|---|
| A1F / A2F / A3R / A4R | Antenna select (A2F appears disabled — why?) | ? | ACOM 06AT switch |
| Next Ant | Cycles to next antenna | ? | ACOM 06AT |

> Which physical antenna is each port? Naming them (e.g. `A1F — DXF vertical`,
> `A3R — 67ft EFHW`) would make this column self-documenting.

### Antenna A/B test

| Control | Does what | Frequency | Path |
|---|---|---|---|
| Manual switching | ? | RARE | ? |
| A1F / A3R / A4R checkboxes | Antennas to include in test | RARE | ? |
| Rounds | Number of A/B cycles | RARE | ? |
| Dwell s | Seconds per antenna | RARE | ? |
| MHz range | Band segment to test over | RARE | ? |
| Start / Stop | Runs the test | RARE | ? |

---

## Questions the layout depends on

Answer these and the hierarchy writes itself.

1. **What are you actually staring at while operating FT8?** The waterfall?
   The decode list (not currently in this view)? The amp telemetry?

2. **What is the single most dangerous thing to get wrong?** Presumably
   transmitting into the wrong antenna, or into a fault. Whatever it is, it
   should be the most visually prominent state in the app.

3. **Which controls do you touch mid-QSO** — meaning under time pressure,
   without looking? Those must be large and in fixed positions.

4. **Which of these are set once and never revisited?** They can collapse
   behind a disclosure and stop competing for attention.

5. **Is the PKTUSB preset represented here?** It bundles AGC/NB/DNF/IPO/DNR/EQ.
   If it exists, it should be one button — and it should visibly override the
   individual controls it sets.
