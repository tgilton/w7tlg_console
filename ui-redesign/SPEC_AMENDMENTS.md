SPEC AMENDMENTS (these override UI_REDESIGN_HANDOFF.md where they conflict).
Source: read-only audit of dashboard/console.html and the backend.

A. Sizing (Terry's decision)
- Keep the console's current overall size and layout behavior. Do not adopt
  the mockup's fixed 388/940 px widths. Keep drag-resizable columns with
  persisted widths (columnWidth:), column collapse (columnCollapsed:),
  section collapse (sectionCollapsed:), and the scrollbar-gutter
  reservation (console.html:72). The mockup is proportions and styling
  only. Text must not get smaller than today's; if something does not fit,
  report it instead of shrinking it.

B. Real controls (use the real ranges; the spec's numbers were guesses)
1. RF GAIN: keep the existing 7-state slider (HF_LNA_GR_DB, runs backwards,
   default "6 dB atten"). Restyle only.
2. Filter width: min/max are mode-dependent (syncFilterRange); FM disables
   it and shows "N/A". Compute any fill from the live min/max.
3. Spectrum SPAN, FLOOR, GAIN: keep existing semantics and readouts (SPAN
   is a log slider; GAIN is the display dynamic range).
4. The unlabeled NR slider (1-15) is labeled "NR STRENGTH". No other change.
5. EQ +-12 dB, DT GAIN 0-100: unchanged. Exciter drive: keep the runtime
   .max rewrite from s.drive_limit_w exactly as is.

C. Behavior changes wanted
1. AF GAIN default becomes 100%. Change console.html (RX1 ~:904, RX2
   ~:1111, value and readout) AND sdr/audio_demod.py:200 manual_gain
   4.0 -> 1.0 for both receivers. The state-push sync (~:4190) writes the
   slider from rig.sdr_rx_volume, so with the audio taper (gain% = 1000 *
   x^2, x = slider position 0-1) that sync needs the inverse mapping.
   Range stays 0-1000%. Check tests for anything assuming 4.0.
2. AVG default becomes 30% on both receivers (markup 80 -> 30 and any JS
   initial value, RX1 and RX2).
3. WF PALETTE already exists (default, linrad, grayscale, hot, gqrx). Keep
   those stop-for-stop, keep dbToColor and wfContrast 1.5 (no LUT rewrite).
   Add Turbo, Viridis, Inferno, Magma, Plasma, Amber phosphor and Green
   phosphor as new PALETTES entries in the same [pos,r,g,b] format (stops
   from spec section 2.3), in both PALETTES and PALETTES2, keeping the two
   lists identical. Add a gradient preview strip and localStorage
   persistence per receiver (new).
4. TRACE selector: the default must be today's exact look (#67D0F0 line at
   1.5 x dpr, fill rgba(103,208,240,0.25)), not the mockup's "Ice blue".
   Add Cyan, Green, Amber, Magenta, White. There are four drawing sites
   (drawSpectrum and drawSpectrum2, line and fill). Persist per receiver.
5. Peak-hold marker as in the spec (6 s hold, 3 dB/s decay, fixed values,
   no settings UI).
6. Frequency axis: today it has 2 labels. A 7-tick axis is optional;
   only do it in stage 5 if it is clean.

D. Decisions in effect (Terry may change them later)
1. Over-S9 label keeps today's 10 dB rounding (smeterLabel and
   smeterLabel2). Needle stays continuous.
2. Add an S9 CAL field to RX2 (today s9CalDb2 is hardcoded -75) and persist
   S9 CAL for both receivers in localStorage (new).
3. FT-991A RX group stays exactly as today: dimmed via .rx-inert, not
   disabled. Merge the duplicate chip into one group heading.
4. Backlit meter theme is NOT built. Paper face only.
5. Tray placement: propose an approach at stage 3 and report. Constraint:
   opening or closing the tray must not change spectrum/waterfall
   canvas sizes.

E. Must keep
1. Antenna tiles are status indicators (div.btn.ant-indicator, no onclick).
   Keep them non-interactive, non-focusable, with no hover or press
   affordance. NEXT ANT is the only actuator. Section 3 of the spec
   ("every interactive element is a button") does not apply to them.
2. #dummy-timer-wrap (under NEXT ANT, shown when s.dummy_load_active) is a
   safety guard. Keep it in the left-column ANTENNA block.
3. Tests that assert on console.html source text must keep passing:
   tests/test_amp_drive_limit.py:264-268 (const driveLimit = s.drive_limit_w
   ?? 100;, slider.max = driveLimit;, id="rf-power-slider") and
   tests/test_tx_guarded_writes.py:269-270. Keep those lines verbatim when
   restyling. If a test must change, stop and ask me.
4. DESIGN.md semantic decisions (PTT-TX amber not red; collapsed 3-tier amp
   severity) win over mockup colors where they conflict. Update DESIGN.md
   in stage 7.
5. Keep the rx-inert / pointer-events:none toggling on the antenna block and
   the A/B test panel when amp_in_path === false (2m/70cm).
6. Keep the Fault Status disclosure and its persisted state.
7. Panadapter and Panadapter2 duplication is deliberate; keep it. Stay in
   the single console.html unless splitting is unavoidable; ask first if a
   static mount or route would be needed.

F. Decisions log (Terry, 2026-09-20, after reviewing stage 2 and the
   stage 3 plan)
F1. Button height: recipe G. --v2-btn-h 44px, band-grid buttons 36px,
    title margin 12px, grid gap 6px, card padding 16px, card gap 16px.
    Revisit --v2-btn-h after stage 5 (one variable).
F2. Band buttons at 36px: accepted. Nothing may be smaller than today's.
F3. Card gap stays 16px (on the DESIGN.md scale). No off-scale values.
F4. The A/B TEST tab stays clickable when the amp is bypassed (2m/70cm);
    its panel stays inert via the existing #box-abtest.rx-inert logic and
    the explanatory tooltip must stay reachable.
F5. The duplicated "FT-991A RX" chip is merged into one group heading in
    stage 3b, keeping the divider between the NB/DNF and PREAMP/ATT groups.
F6. Tray: manual open only, no auto-open. First load (no saved state):
    collapsed. The green dot is the cue.
F7. SSB Audio takes the left column's card chrome when it moves (3b).
F8. The Measure Noise panel carries a small "RX1" label (it samples the
    RX1 feed).
F9. The one-column session at narrow widths is accepted. RAG-CHEW
    wrapping onto two lines in the TX BW row is accepted. No fix needed.
F10. Stage 4 is split. 4a = frequency card, S-meter (needle + peak hold),
     RX chip, S9 CAL on both receivers. 4b (later) = gains card, AF GAIN
     default and taper, mode card.
F11. Stage 4 complete. AF GAIN default 100% confirmed live.
F12. TRACE default = today's exact look (#67D0F0 line, rgba(103,208,240,
     0.25) fill), not the mockup's "Ice blue".
F13. The scope card (spectrum + waterfall) keeps its inner padding so the
     canvases keep today's size: the spectrum and waterfall must look as
     they do now, only the card chrome changes to the v2 hairline.
F14. Mouse wheel on the four spectrum sliders (span, floor, gain, avg)
     changes them only while Shift is held.
