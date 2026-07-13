# FUNCTIONS.md — W7TLG Console

What every control does, how often it is used, and what hardware it reaches.

This is the operator knowledge no model has. It exists so that layout and
hierarchy decisions are inferences rather than guesses.

**Frequency** drives visual weight and placement:

- `HOT` — touched or watched constantly while operating. Large, always visible.
- `SESSION` — set when starting on a band or mode, then left alone.
- `RARE` — setup, calibration, diagnostics, experiments. May be collapsed.
- `READ` — monitoring only. Scannable, not clickable.

Frequency is **mode-dependent**. See §1 — a control that is HOT on SSB may be
irrelevant on FT8. Where the two differ, both are given.

`[CC]` means the answer is derivable from the source. Claude Code should read
`server.py` / `console.html` and fill it in rather than asking the operator.

---

## 1. Three operating modes — and nothing hides

The console is used in three distinct modes: **digital** (DATA-U / FT8),
**voice** (SSB), and **CW**. Each leans on a different subset of controls.

**All controls stay visible at all times.** The operator switches between modes
quickly and needs to reach any control without waiting for it to reappear.
Controls that hide, dim, move, or reorder destroy muscle memory — an instrument
you have to re-read on every glance is not an instrument. Layout is static.

Hierarchy is therefore expressed through **size, weight, grouping, and
position** — never through visibility. This is a stricter constraint than
hiding, and it is the right one.

Mode-specific emphasis, for reference (this informs *placement*, not visibility):

| Block | Digital | Voice | CW |
|---|---|---|---|
| Waterfall / spectrum | primary | secondary | secondary |
| Amp telemetry | primary | primary | primary |
| SSB Audio (MIC, COMP, TX BW) | unused | primary | unused |
| Noise filters (NB, DNF, DNR) | unused | primary | used |
| AGC / VOL | VOL only | primary | primary |
| Preamp / ATT | rig RX only | rig RX only | rig RX only |
| Filter width (NAR/WID) | set once | set once | **narrow — HOT** |

### CW controls are missing

The console has a `CW` mode button and no CW controls. There is no keyer speed,
no break-in / QSK, no sidetone pitch, no CW filter preset.

**TODO (operator):** are you driving these from the FT-991A front panel, or
should the console expose them? If CW is a real operating mode, this is a gap —
not a styling issue.

CW is also used *instrumentally*: switching to CW to key the amp for a tune
cycle. See §4, Mode.

## 2. Startup is a distinct phase

Before operating, the operator performs a **scan**, not a set of adjustments:

1. Radio and amp are already powered on (outside this app).
2. If the amp reports **ATU Unassigned Error**, it must be cleared on the ACOM
   front panel. The console cannot fix it — but it should say so plainly.
3. Verify mode, sliders, and general state are as expected.
4. Check band / frequency, change if needed.
5. Operate.

Steps 2–3 are a checklist currently scattered across three columns. A startup
state summary — everything nominal / these N things are not — would replace a
manual sweep of the whole UI. This is the clearest new-feature opportunity in
the app.

---

## 3. Header / global

| Control | Does what | Frequency | Path |
|---|---|---|---|
| Rig / Amp / SDR / WSJT-X dots | Connection status per subsystem | READ (HOT at startup) | all |
| Inhibit | Amp has raised a fault and refuses to operate | READ — **critical** | ACOM 1200S |
| Allow | Operator clears the condition and re-permits the amp | HOT — **critical** | ACOM 1200S |
| Monitor ↗ | `[CC]` | `[CC]` | `[CC]` |
| Propagation ↗ | `[CC]` | `[CC]` | `[CC]` |
| Spot/Seek ↗ | `[CC]` | `[CC]` | `[CC]` |
| Advisor ↗ | AI band advisor | `[CC]` | `[CC]` |

### Inhibit / Allow are separated from the fault they report

This is a **workflow defect**, not a styling one.

`Inhibit` means the amp has faulted and will not operate. To recover, the
operator diagnoses the condition, fixes it, and presses `Allow`.

But the *fault* is reported in `FAULT STATUS`, mid-way down the right column,
and `Allow` is a small button in the header sitting among four navigation links.
When the amp inhibits mid-session, the operator must look in one place to learn
why and a completely different place to recover.

**Inhibit / Allow belong with FAULT STATUS.** They are one control loop:
*fault raised → cause shown → operator clears → operator permits*. They should
read as a single unit, and they should carry the visual weight of the most
consequential control pair in the application — which, on a station with a
1200S, they are.

---

## 4. Left column — rig and DSP

### Mode

| Control | Does what | Frequency | Path |
|---|---|---|---|
| USB / LSB / DATA-U / CW / AM / FM | Rig mode | SESSION | FT-991A via rigctld |

Rarely changed mid-session, with one recurring exception: **switching to CW to
key the amp for a tune cycle**. That is a real workflow, and it means CW is not
a "mode I operate in" but a tool used from within another mode. Worth
considering a dedicated **Tune** action that handles the mode change, keys the
amp, and restores the prior mode — rather than making the operator drive mode
manually and remember to switch back.

### Filter

| Control | Does what | Frequency | Path |
|---|---|---|---|
| NAR / WID | IF filter width select | SESSION | FT-991A |
| Width slider | RX filter bandwidth | SESSION | FT-991A |

### Digital audio

| Control | Does what | Frequency | Path |
|---|---|---|---|
| DT GAIN | `[CC]` | `[CC]` | `[CC]` |

### SSB audio — **voice only**

| Control | Does what | Frequency | Path |
|---|---|---|---|
| MIC | Mic gain | HOT on SSB / inert on digital | FT-991A |
| COMP | Speech compression | HOT on SSB / inert on digital | FT-991A |
| TX BW — WIDE / RAG-CHEW / DX | TX bandwidth profile | SESSION on SSB / inert on digital | FT-991A |

### Noise reduction — **voice only**

| Control | Does what | Frequency | Path |
|---|---|---|---|
| NB | Noise blanker | HOT on SSB / inert on digital | FT-991A |
| DNF | Digital notch filter | HOT on SSB / inert on digital | FT-991A |
| DNR | Digital noise reduction | HOT on SSB / inert on digital | FT-991A |
| DNR level | DNR strength | HOT on SSB / inert on digital | FT-991A |

### AGC and audio — **voice only**

| Control | Does what | Frequency | Path |
|---|---|---|---|
| AGC — OFF / FAST / SLOW | AGC time constant | HOT on SSB / inert on digital | FT-991A |
| VOL | Audio level — **see below** | HOT | `[CC]` |

**VOL is not a bug.** 400% is approximately correct. It is one leg of a
three-way relationship:

```
VOL (this console)  <->  WSJT-X power slider  <->  FT-991A ALC
```

The operator adjusts it to land ALC in the right range. It therefore belongs
**next to the ALC meter**, not stranded at the bottom of the left column away
from the thing it interacts with. Consider showing ALC alongside it, or
annotating the ALC target band directly.

### Preamp and attenuator — **FT-991A receiver only**

| Control | Does what | Frequency | Path |
|---|---|---|---|
| Preamp — IPO / A1 / A2 | RX preamp stage | SESSION when using rig RX / inert otherwise | FT-991A |
| ATT | RX attenuator (currently a value with no control) | SESSION when using rig RX / inert otherwise | FT-991A |

Only relevant when receiving on the FT-991A rather than the SDR. When the SDR
is the receive path, this block should dim.

---

## 5. Center — spectrum and tuning

| Control | Does what | Frequency | Path |
|---|---|---|---|
| VFO readout | Tuned frequency; click-to-edit | HOT | FT-991A |
| Waterfall / spectrum | **Primary attention during digital operating** | HOT | SDR |
| Band pills / mode pills | `[CC]` — status or clickable? | `[CC]` | `[CC]` |
| S-meter | RX signal strength | READ | `[CC]` |
| S9 Cal | S-meter calibration offset | RARE | `[CC]` |
| ALC | TX ALC level — **interacts with VOL and WSJT-X power** | HOT | FT-991A |
| RADIO PO | Rig power out | READ (HOT) | FT-991A |
| SWR (rig) | Rig-side SWR — **watched for drift** | READ (HOT) | FT-991A |
| Tune | `[CC]` — does this run an amp/ATU tune cycle? | `[CC]` | `[CC]` |
| Whole Band / Center | Spectrum zoom / centering | SESSION | SDR |
| Audio: Live | `[CC]` | `[CC]` | `[CC]` |
| SPAN / AUTO / FLOOR / GAIN / AVG | Waterfall display params | SESSION | SDR |
| Palette | Waterfall colormap | RARE | SDR |
| EQ — Bass / Mid / Treble | `[CC]` — RX audio EQ? which path? | `[CC]` | `[CC]` |

---

## 6. Right column — amplifier

**The amp telemetry stack is HOT during all operating.** The operator watches
it continuously — not for a number, but for **drift**. Power should be where
it was set; SWR (rig and amp) should agree with each other and stay put.

This is a monitoring task, and it argues for showing trend, not just
instantaneous value. A drifting SWR is the signal; the current reading alone
is not.

| Control | Does what | Frequency | Path |
|---|---|---|---|
| Fwd P | Forward power | READ — **HOT, watched for drift** | ACOM 1200S |
| Rev P | Reflected power | READ — **HOT, watched for drift** | ACOM 1200S |
| SWR (amp) | Amp SWR — cross-checked against rig SWR | READ — **HOT, watched for drift** | ACOM 1200S |
| Drive P | Drive power into amp | READ — HOT | ACOM 1200S |
| PAM1 T | PA module temperature | READ — HOT | ACOM 1200S |
| HV / I | HV rail and current | READ — RARE, troubleshooting only | ACOM 1200S |
| Fault status | Active faults — **incl. ATU Unassigned Error** | READ — HOT at startup | ACOM 1200S |
| Standby | `[CC]` — indicator or control? | `[CC]` | ACOM 1200S |
| AMP OFF / AMP ON | Amp operating mode | SESSION | ACOM 1200S |
| Exciter drive | Power the FT-991A pushes into the amp | HOT | FT-991A |

**HV / I are demoted.** They are troubleshooting instruments, not operating
instruments. They currently sit in the same visual stack as the drift-watched
parameters. They should be collapsible or visually subordinate — present, but
not competing.

**Fault status deserves promotion at startup.** ATU Unassigned Error requires
a trip to the ACOM front panel. The console should name the fault and say
plainly that it must be cleared on the amp itself.

---

## 7. Right column — antenna

Port names (`A1F`, `A2F`, `A3R`, `A4R`) are **hard-coded in the ACOM 1200S
setup** and can only be changed at the amp's front panel. The console cannot
rename them and should not try.

It *can*, however, annotate them locally — a config-side alias map that displays
the operator's name alongside the amp's port name. This does not fight the amp;
it just stops the column being opaque.

| Port | Antenna | Notes |
|---|---|---|
| A1F | SS25 vertical | **Experimental.** Frequently reconfigured — counterpoise variations, multi-band options. This is the antenna under test most of the time. |
| A2F | *(nothing connected)* | Explains why it renders disabled. |
| A3R | 40m EFHW | |
| A4R | **1500 W dummy load** | Not an antenna. See below. |

| Control | Does what | Frequency | Path |
|---|---|---|---|
| A1F / A2F / A3R / A4R | Antenna select — mostly informational | READ, occasionally SESSION | ACOM 06AT switch |
| Next Ant | Cycle to next antenna | RARE | ACOM 06AT |

### The dummy load needs to be unmistakable

`A4R` is a 1500 W dummy load, and nothing on screen says so.

Transmitting into it is perfectly safe — which is exactly the problem. The
console will show a beautiful SWR and healthy forward power while the operator
calls CQ into a resistor. There is no failure to notice.

**Selecting A4R must be visually distinct from selecting an antenna.** Not a
fault — it is a legitimate and frequently correct state — but categorically
different from being on the air. A persistent `DUMMY LOAD` badge near the VFO
readout, or a distinct treatment on the readout itself, would make the state
impossible to lose track of.

This is the clearest safety-adjacent finding in the document, and it costs
almost nothing to implement.

### A1F is the experiment bench

The SS25 gets reconfigured constantly, and A1F-vs-A3R comparison is a routine
task — both by eye on the spectrum and via the A/B test. The antenna group is
therefore not purely informational: it is the entry point to the station's
experimental workflow. That argues for keeping the A/B test *near* it, not
buried below it in a cramped block.

### Antenna A/B test

| Control | Does what | Frequency | Path |
|---|---|---|---|
| Manual switching | Bypasses AcomBridge; free-text antenna labels; pauses each step until the operator physically switches and confirms | RARE | none — operator |
| Antenna checkboxes | Antennas to include in the run | RARE | ACOM 06AT |
| Rounds | Number of A/B cycles | RARE | — |
| Dwell s | Seconds per antenna | RARE | — |
| MHz range | Band segment to sweep | RARE | — |
| Start / Stop | Runs the test | RARE | — |

**RARE but not unimportant.** This is an experimental instrument, used
deliberately and not infrequently — it is the origin of the current
`antenna_ab_test.py` work. It is a *task*, not a control panel: it deserves to
be a distinct surface rather than a cramped block competing with operating
controls. Strong candidate for its own view.

---

## 8. Design consequences

Ranked by impact. Note that none of these are styling fixes — they came out of
describing the actual operating workflow, and no amount of restyling would have
surfaced them.

1. **Inhibit / Allow must sit with FAULT STATUS.** They are one control loop
   currently split across the screen. This is the highest-value fix in the list
   and it is a workflow defect, not a cosmetic one.

2. **The dummy load must be unmistakable.** A4R is a 1500 W resistor. The
   console currently presents it exactly like an antenna, and shows perfect SWR
   while the operator transmits into it.

3. **Nothing hides, nothing moves.** All controls stay visible and in fixed
   positions across all three modes. Hierarchy comes from size, weight, and
   grouping. This is a constraint on every other decision here.

4. **The amp stack is a drift monitor, not a readout.** Fwd P, Rev P, both SWRs,
   Drive P, PAM1 T are watched continuously for *change*, not value. Show trend.
   Demote HV / I — they are troubleshooting instruments, not operating ones.

5. **VOL belongs next to ALC.** They form one control loop with the WSJT-X power
   slider. They are currently on opposite sides of the screen.

6. **CW controls are missing entirely.** Keyer speed, break-in, sidetone, CW
   filter. Either they belong here or CW is driven from the rig's front panel —
   but the current state (a CW mode button and nothing else) is incoherent.

7. **Tune should be one action.** Switch to CW, key the amp, restore prior mode.
   The console can own that sequence instead of making the operator drive it.

8. **Startup needs a state summary.** "Is everything in order?" is currently
   answered by manually sweeping three columns. It should be one glance —
   including a plain statement when a fault (e.g. ATU Unassigned Error) must be
   cleared on the amp's own front panel.

9. **Annotate the antenna ports.** Port names are fixed by the amp, but a local
   alias map makes the column readable: `A1F — SS25 vertical`, `A3R — 40m EFHW`,
   `A4R — DUMMY LOAD`.

10. **The A/B test wants room.** It is a real experimental instrument, used
    deliberately and not infrequently. It should sit near the antenna group and
    have space to breathe, not be crushed into the bottom of a column.
