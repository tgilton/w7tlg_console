# TX_GATING_AUDIT.md

Read-only audit of every piece of code in this repo that reacts to,
gates on, or mutes because of transmit state. **No code was changed.**

- Branch `audit-tx-gating`, HEAD `d824434`.
- Baseline before the audit: **98 passed in 1.23s** (`./venv/bin/python -m pytest -q`).
- Date: 2026-09-19.

Hardware context supplied by the operator and used throughout:

> The FT-991A PTT line goes to **both** the SDRSwitch.com switch and the
> SDS-4000S simultaneously. The SDS-4000S then keys the amp via AUX after
> ~30 ms, so PTT-to-amp-on is ~75–100 ms. **The stock (no console
> intervention) transmit scheme has never been tested with this hardware.**

That last sentence is the single most important fact in this document. Every
element below was added between 2026-06-21 and 2026-06-28 against an
RSPdx-R2 and a differently-wired switch, against symptoms observed *with the
console already in the loop*. None of them has been re-validated since the
RSPduo swap (2026-09-11) or against the current SDS-4000S wiring.

---

## 1. Inventory — every TX-reactive element

Fourteen elements, server + browser. Nothing else in the repo reads,
writes, or branches on transmit state.

| # | Element | File:line | Fires on | Class |
|---|---|---|---|---|
| 1 | `_fast_ptt_monitor()` — 5 ms rigctld PTT watchdog, own TCP socket | `dashboard/server.py:473-557` | rigctld `t` poll | (b) + (a)† |
| 2 | `tx_mute` broadcast on `/ws` | `dashboard/server.py:520,542` | #1 rising edge | (b) |
| 3 | `_POST_TX_HOLD_S = 0.15` hold-and-keep-polling after PTT drops | `dashboard/server.py:468-470,527-550` | #1 falling edge | (b) |
| 4 | `SdrClient.gate_tx()` — fans out to A, B, RX0, BlackHole | `sdr/sdr_client.py:344-358` | called by #1 and #9 | (b) |
| 5 | `AudioDemodulator.gate_tx()` — IQ queue flush + AGC reset to 1.0 | `sdr/audio_demod.py:410-425` | #4 | (b) |
| 6 | `AudioDemodulator._run` TX drop + overlap/EQ/NR state reset on falling edge | `sdr/audio_demod.py:648-670` | `tx_active` | (b) |
| 7 | `AudioDemodulator._publish` early-return on `tx_active` | `sdr/audio_demod.py:834-839` | `tx_active` | (b) |
| 8 | `DigitalAudioOutput.flush()` — drains the BlackHole queue | `sdr/virtual_audio_output.py:68-74` | #4 | (b) |
| 9 | `on_station_state` slow-path PTT edge (100 ms bridge poll) | `dashboard/server.py:381-396` | main rig poll | (b) safety net |
| 10 | `on_audio_frame` / `_b` / `_0` broadcast gates | `dashboard/server.py:440-458` | `tx_active` | (b) last line of defence |
| 11 | `_compute_frame` / `_compute_frame_b` average-power reset + phase-tap suppression | `sdr/sdr_client.py:1005-1017`, `1108-1120` | `tx_active` | (b) display hygiene |
| 12 | `Combiner` (RX0) TX drop + DSP state reset | `sdr/combiner.py:344-350,485-515` | `tx_active` | (b) |
| 13 | Browser `txSilenceStart()` / `txSilenceEnd()` — gain→0, worklet flush, **audio WS close**, 500 ms unmute hold | `console.html:1669-1691` (RX1), `3362-3379` (RX2), `panadapter.html:440-470` | `tx_mute` or `rig.ptt` | (b) |
| 14 | `parse_ptt_reply()` strict `[0-3]` validator + hold-previous-on-None | `rig/rigctld_client.py:316-336,730-751` | main poll | (c) |

† #1 is filed under (b) deliberately — see §6.1. It is *described* in its
own commit as protecting the SDR front end, but it cannot: it is software
reacting ~2.5 ms after a PTT the hardware switch has already acted on.

Rig-side TX behaviour that is not a "gate" but belongs in the same picture:

| Element | File:line | Effect |
|---|---|---|
| Frequency poll frozen during TX | `rig/rigctld_client.py:755-778` | prevents a split VFO-B read from retuning the SDR and band-selecting the amp mid-TX |
| Mode/split poll frozen during TX | `rig/rigctld_client.py:780-795` | prevents the FT-991A's spurious VFO-B mode from flipping the digital profile |
| Meter poll switches to the TX set | `rig/rigctld_client.py:~781` | `l ALC` is polled **only** while PTT is true — this is the proof-of-phantom marker used in finding I |

---

## 2. Q1 — What triggers "Fast PTT: TX gate opened, tx_mute sent"?

**Exactly one thing: the rising edge of `_fast_ptt_monitor`'s own rigctld
`t` poll** (`dashboard/server.py:504-521`). That log string exists nowhere
else in the tree.

Ruled out by direct reading, not inference:

- **WSJT-X UDP** — `wsjtx/` carries a `transmitting` flag through
  `on_wsjtx_status` (`server.py:404-435`) which is *broadcast only*. No
  subscriber. It touches no gate.
- **Amp telemetry** — `t.flag_keyin` lands in `station.amp_ptt_active`
  (`acom_bridge.py:675`) and in the trend CSV. It drives the UI badge and
  nothing else. The amp is never an input to the gate.
- **UI click** — no frontend command reaches `gate_tx`. The only gate-
  adjacent commands are `set_audio_enabled` and `set_audio_target`.
- **Main rig poll** — reaches `gate_tx` via `on_station_state`
  (`server.py:384-387`) but logs nothing. Its own visible signature is
  `"TX start detected"` from `acom_bridge.py:521`.

The monitor keeps a **dedicated** TCP connection to rigctld and writes
only `t\n`, every 5 ms, forever.

### 2.1 The startup gate with no matching amp TX

The signature — a `Fast PTT: TX gate opened` with no `TX start detected`
and no amp key-in — means the **fast monitor saw `"1"` while the main poll
did not.** After `74a5b60` the main poll validates strictly, so it will
reject a straggler and hold PTT false; the fast monitor has no such guard
and will gate. The two paths disagreeing in exactly this direction is the
expected fingerprint of a fast-monitor-only false positive.

Ranked candidates, most to least likely:

**1. Serial-layer desync inside rigctld — the gap finding I left open.**
Finding I's conclusion was that the watchdog "cannot meaningfully desync"
because every reply on its socket is a `t` reply. That is true **at the
TCP layer and only there.** Finding J then measured ~4.0 s reply latency,
quantised at 4.00–4.04 s, and concluded the cause is a *fixed
timeout-and-retry below us* — i.e. hamlib's serial backend retrying failed
reads. A retried/mis-framed read inside rigctld produces a wrong PTT value
that arrives on the watchdog's socket *correctly framed as a `t` reply*.
The watchdog's immunity does not extend there. **A startup-only gate with
no RF is positive evidence that this happens.**

Startup is also when it is most likely: `bridge.start()` and the 100 ms rig
poll come up first (`server.py:660`), then `await sdr.start()` blocks on
SDRplay device open, and only then does `asyncio.create_task(
_fast_ptt_monitor())` fire (`server.py:689`) and add ~200 requests/second
to an already-contended serial link. Peak contention, cold hamlib state.

**2. A real, brief PTT assert that the amp legitimately did not see.**
"No amp TX" is *not* proof of no RF. `flag_keyin` only goes true when the
SDS-4000S has keyed the amp via AUX — so a PTT shorter than the ~75–100 ms
PTT-to-amp-on delay, or any PTT while the amp is in STANDBY, on a
`DIRECT_TO_RIG_BANDS` band (`acom_bridge.py:105` — 2m/70cm), or diverged
into HV-off (open bug `project_acom_amp_mode_divergence`), produces exactly
"gate fired, amp silent." The trend log records only `flag_keyin`, so it
cannot distinguish these.

**3. Hamlib's own state at first read.** rigctld is usually already running
when the console starts (`tools/start_w7tlg.sh:19`), so a fresh port open
is not normally in play — this is the weakest of the three.

**Decisive test, cheap:** run a raw `nc 127.0.0.1 4532` loop sending `t`
while the console starts, logging every reply with a timestamp. If a `"1"`
appears there with the key up, it is candidate 1 and the fix belongs in
rigctld/hamlib timeouts, not in this repo. If the raw socket stays `"0"`
while the console logs a gate, the desync is inside the watchdog's own
socket handling and #1 needs `parse_ptt_reply`'s treatment.

### 2.2 A real defect found while answering Q1

`_fast_ptt_monitor` parses PTT as `ptt = decoded == '1'`
(`server.py:501`). `parse_ptt_reply` accepts `0-3`, because hamlib returns
**2 = `RIG_PTT_ON_MIC`** and **3 = `RIG_PTT_ON_DATA`**. The two readers of
the same signal use different grammars:

- If this rig ever reports `2` for mic PTT, **the fast gate silently never
  fires for voice TX** — the exact case commit `c5f2ee2` was written for —
  and the 100 ms slow path handles it, which is the pre-`c5f2ee2` behaviour.
- The gate demonstrably does fire today, so hamlib is returning `1` for at
  least the observed path. Whether that holds for mic PTT vs CAT PTT is
  **unverified** and is a one-line hardware test (§8).

---

## 3. Q2 — Why both audio WebSockets drop and reconnect on every TX

**It is intentional, it is load-bearing, and it is the most expensive thing
in this whole subsystem.**

`txSilenceStart()` closes the audio WebSocket outright
(`console.html:1675`, `3367`). Both fire because the `/console` page runs
two independent panadapter modules, each owning its own `/ws` and each
handling `tx_mute` separately: RX1 at `console.html:1613`, RX2 at
`console.html:2559`. So one `tx_mute` broadcast closes `/ws/audio` **and**
`/ws/audio_b`, and the server logs two `Audio WebSocket disconnected`
lines, then two `connected` lines ~500 ms after PTT drops.

**Why a close and not just a mute.** Three commits, in order:

1. `5e5c868` (2026-06-27) — original design: gain→0 + ring flush + frame
   gate, all on one flag.
2. `1ab49c3` (2026-06-28) — that left ~100 ms of already-delivered audio
   in the worklet ring playing into the mic. The WS close was introduced
   as the "nuclear" option because **closing the socket discards the
   frames still sitting in the browser's receive queue**, which no
   JS-level flush can reach.
3. `5659a05` (2026-06-28) — the flush signal was moved off `/ws/audio` (it
   always lost the race, sitting behind the binary backlog) onto `/ws`.

The problem it solves is the mic feedback loop: `3b1a673`'s commit message
records a **"2-second building crash"** — SDR audio → speakers → FT-991A
mic → TX RF → SDR → louder. That is a genuine, observed, destructive
failure, not a theoretical one.

**What it costs.** Per transmission, per receiver: a TCP+WS teardown, a
500 ms unmute hold, a fresh handshake, and a cold worklet ring. What does
**not** re-initialize is the expensive part — `ensureAudioWorklet()`
caches `audioCtx`/`audioWorkletNode`/`muteGain`
(`console.html:2281-2304`), and the server's `AudioConnectionManager`
(`server.py:151-179`) is stateless. So "the audio chain re-initializes" is
accurate at the socket and ring-buffer level, not at the AudioContext
level. The audible cost is a dead spot of `TX duration + 500 ms +
handshake` on **both** receivers.

**The part that is not justified.** RX2 is muted by RX1's PTT. RX2 is a
physically separate tuner on a receive-only antenna, frequently on a
different band (`FUNCTIONS.md §8`; memory `project_rx2_independent_receiver_intent`).
Its speaker output can feed the mic just like RX1's, so muting it during TX
is correct for the feedback loop — but **closing its socket and flushing
its DSP state** (`combiner.py:509`, `audio_demod.py:658` on the B instance)
is inherited from RX1's antenna-disconnect reasoning, which may not apply
to RX2 at all. Whether it does is Q6, and the code does not answer it.

---

## 4. Q3 — Does the console originate or delay PTT, or gate TX audio?

**PTT: no. Confirmed exhaustively.**

`RigctldClient.set_ptt()` exists (`rig/rigctld_client.py:410-411`) and is
called from **nowhere** in production code — only from the test fake. The
only other reference is `amplifier/tx_power_calibration.py:9`, a docstring
that records this as a deliberate standing choice: *"keeping a human at the
key for every RF-producing burst … was an explicit choice over automating
PTT."* Grep for `set_ptt` returns three hits, all of them documentation or
fakes. The console is a pure observer of PTT.

**TX audio: no.** `DigitalAudioOutput` (`sdr/virtual_audio_output.py`) is
**RX-only** — it writes the SDR's demodulated receive audio *into* BlackHole
for WSJT-X to decode. WSJT-X's transmit audio goes straight to the
FT-991A's own USB CODEC and never touches this process
(`virtual_audio_output.py:12-14`, `ARCHITECTURE.md` RF diagram). There is
no TX audio path in this repo at all — no buffer, no gate, no delay.

**Therefore nothing in this repo can change RF onset time relative to
PTT.** The console has no wire into the PTT line, the key line, or the TX
audio path.

Things that change RF *while* transmitting, or the state around it — the
honest list, since they are what "flag anything that could" should surface:

1. **`set_rf_power` from amp-mode sync, with no PTT guard.**
   `acom_bridge.py:645-656`: three consecutive OPR-class telemetry frames
   flip `_mode` to `AMP_ON` and then, unconditionally, `await
   self.rig.set_rf_power(40)` (`MODE_DRIVE_LIMITS`, `:95-98`). Telemetry
   arrives at ~100 ms. **There is no `rig.state.ptt` check on this path**,
   unlike the band-select path right above it. A CAT `L RFPOWER` write can
   therefore land mid-transmission and change output power inside a QSO.
   This is the one genuine "changes RF during TX" finding in the audit.
2. **`inhibit_tx()` → `cmd_tx_prohibit()`** (`acom_bridge.py:486-492`),
   fired from HV-rail collapse and the dummy-load curve trip during live
   TX. Correct and intended — this is the safety interlock doing its job.
3. **`cmd_select_band`** — the only console→relay path that could hot-switch
   the amp. Guarded twice: `if not rig.ptt` at `acom_bridge.py:516`, and by
   the frequency poll being frozen during TX (`rigctld_client.py:755`).
   Both guards depend on PTT being read **correctly**, which is why a
   *false-negative* PTT is the dangerous direction — see §5.
4. **`cmd_next_antenna`** — refused while `rig.state.ptt or
   self._tx_was_active` (`acom_bridge.py:443`).

---

## 5. Q4 — Does any path drive hardware from inferred TX state?

**Yes — four, and one of them is unguarded.**

| Path | Drives | Inferred from | Fail direction |
|---|---|---|---|
| Drive-limit clamp `set_rf_power(40)` | **rig, via CAT** | amp telemetry mode byte, 3-frame filter | **Unsafe** — no PTT guard; can write during TX |
| `switch_antenna` refusal | amp relay (blocks) | `rig.state.ptt` | Safe — blocks on doubt |
| `cmd_select_band` | **amp band relays** | `not rig.ptt` | **Unsafe on false-negative** |
| Dummy-load watchdog → `inhibit_tx` | amp, `cmd_tx_prohibit` | `rig.ptt` starts it, but the **trip** needs measured `t.fwd_power_w` | Safe — a phantom PTT reads 0 W and never trips |

The asymmetry that matters: **every guard in this system is written against
a false-positive PTT, and the dangerous failure is a false negative.**

- A phantom TX (PTT reads true, no RF) costs audio and display. It is
  visible, it was chased down as finding I, and it was fixed. It is also
  self-limiting for the dummy-load trip, because the trip reads real watts.
- A *missed* TX (PTT reads false, RF is live) lets `cmd_select_band` fire
  into the amp's band relays with RF on the line. `parse_ptt_reply` returns
  `None` on a bad reply and **holds the previous state**
  (`rigctld_client.py:326-333`) — which is the right call when the previous
  state was TX, and the wrong one when it was RX and a real TX just started.
  Chain it with WSJT-X split (`f` returning a VFO-B on another band, the
  exact case `rigctld_client.py:756-760` warns about) and you have a band
  change command sent into live RF.

This matches memory `project_phantom_ptt_and_rig_wedge`: *amp band-select
is the only desync→relay path*. This audit confirms that and narrows it —
the path is only open when PTT reads **false** while RF is live, which is
the one case none of the current guards address.

Also worth recording: `sdr/sdr_client.py:348-354`'s docstring still claims
Channel B's `tx_active` "isn't wired to PTT yet … is a no-op in practice."
That is **stale** — `server.py:388-396` and `:547-549` wire all three
(A, B, RX0) in both directions. The comment describes 2026-09-11 behaviour.

---

## 6. Q5 — Classification, with the history for each

### 6.1 (a) SDR front-end protection — **nothing in software qualifies**

This is the headline of the audit.

The physical SDRSwitch.com relay is what protects the RSPduo front end, and
it is driven directly off the same PTT line as the SDS-4000S. Software
running on a 5 ms poll cannot protect a front end from a transient the
relay has already handled; by the time `_fast_ptt_monitor` has read `"1"`
over TCP from a daemon talking CAT over a 38400-baud serial link, the relay
has long since thrown. **Not one line of code in this repo is front-end
protection.** Every comment that says "SDR Switch disconnects the antenna
during TX" (`audio_demod.py:649`, `sdr_client.py:1015`, `:202`) is
explaining why the *samples* are garbage — it is describing hygiene, and it
is correctly worded. The commit message for `c5f2ee2` frames the gate as
stopping "TX RF leaking into the SDR", which reads as protection but was
actually about the audio blast. That framing should not be trusted.

### 6.2 (b) Audio hygiene — elements 1-13, all of them

| Element | Added | Why, per its commit |
|---|---|---|
| Filter/AGC reset on TX edge | `9962e98` 2026-06-21 | "PTT now freezes the demodulator and resets its filter/AGC state instead of feeding it disconnected-antenna noise" |
| Spectrum average reset on TX | `282ab8f` 2026-06-21 | persistent averaging was "bleeding TX-period garbage into the first several RX frames after every transmission" |
| Browser `txSilenceStart/End` | `5e5c868` 2026-06-27 | mic picks up speaker audio and transmits it as SSB |
| Fast PTT monitor + `gate_tx` + hold | `c5f2ee2` 2026-06-27 | mic PTT gives no software notice; 100 ms poll → ~50 ms lag → "the high-AGC-gain output blasts through to the browser" |
| `_publish` gate + `DigitalAudioOutput.flush` | `3b1a673` 2026-06-27 | **the 2-second building feedback crash** — BlackHole's 64-frame (~6 s) queue kept draining to the speakers after the IQ gate closed |
| WS close as the mute | `1ab49c3`, `5659a05` 2026-06-28 | ring-flush alone left ~100 ms of audio already in the browser; `/ws/audio` text always lost the race to `/ws` |
| Poll-through-hold, 20 ms → 5 ms | `8ae27be` 2026-06-28 | every other TX missed `tx_mute` — the falling-edge `sleep` blacked out polling for the whole 150 ms hold |
| Unmute-timer cancel before early return | `298a9a9` 2026-06-28 | a stale `ptt=False` scheduled an unmute that fired **mid-TX** and reopened audio |
| Channel B / RX0 gating | `1c99f07`, `3c82c7f` 2026-09-11 | mechanical extension of the same pattern to the new pipelines |

Every one of these is a real, reproduced bug fix. Six of the nine are fixes
to *earlier attempts at the same fix* — this subsystem was built by
iteration against live symptoms over two days, which is precisely the
pattern memory `feedback_dont_stack_live_symptom_fixes` warns about.

### 6.3 (c) Amp / TX safety — element 14 only

`parse_ptt_reply` (`74a5b60`, 2026-09-19). Not audio: its purpose is to
stop a desynced straggler from faking a transmission, which in turn keeps
the frequency sanity check alive (it is the only desync detector in the
poll loop, and it only runs while PTT is false). See finding I.

Genuine amp/TX safety lives outside this subsystem entirely — the amp's own
fault bits, the HV cross-check, the dummy-load curve, and `cmd_tx_prohibit`.

### 6.4 (d) Dead, stale or duplicated

1. **`dashboard/panadapter.html:389-470`** — a full second copy of the
   `tx_mute` handler and `txSilenceStart/End`, still served at
   `/panadapter` (`server.py:1501`). `console.html:1520-1530` states it was
   copied verbatim and deliberately not refactored. Not dead (the page
   loads), but it is an unversioned fork that will drift.
2. **`sdr/sdr_client.py:348-354`** — stale docstring, see §5.
3. **`Combiner._publish` (`combiner.py:579-581`) has no `tx_active`
   guard**, unlike `AudioDemodulator._publish:834-838`. RX0 is covered only
   by the consumer-loop drop (`:505`) and `on_audio_frame_0`
   (`server.py:454-458`). Harmless today because RX0 has no second
   subscriber the way Channel A has BlackHole — but it is the exact hole
   `3b1a673` was written to close, left open in the copy.
4. **`_fast_ptt_monitor`'s `last_ptt = last_ptt` no-op**
   (`server.py:499`) — a comment wearing an assignment.
5. **`ARCHITECTURE.md:56-135`** — the RF diagram still shows an RSPdx-R2
   and a 3-port SDR Switch, with no RSPduo, no second tuner and no
   SDS-4000S. It is eight days out of date and actively misleading for
   exactly the question in §7.

---

## 7. Q6 — Which RSPduo tuner sits behind which switch?

**The code does not settle it. Listing as open questions.**

What the code *does* assert, and it is only the antenna half:

> "Antenna routing is now fixed by physical wiring per tuner — **Tuner 1 /
> Ant A is TX-capable (the only antenna the amp can key), Tuner 2 / Ant B
> is receive-only**"
> — `sdr/sdr_client.py:19-22`

Corroborated by `DESIGN.md:303-309` ("antenna 1 is fixed as the only
TX-capable chain — wiring, not software") and `FUNCTIONS.md:357-359`. Since
`1c99f07`, `_antenna_for_freq`-style software antenna switching is gone
entirely; `sdr_client` only ever addresses `Tuner_A`/`Tuner_B` for tuning,
gain and notches. The RSPduo's `AMPORT` selection
(`sdrplay_capi.py:81-83`) is never written, so **Tuner 1 is on its normal
SMA input, not the Hi-Z port.**

What is nowhere in the repo — not in code, comments, `ARCHITECTURE.md`,
`DESIGN.md`, `FUNCTIONS.md`, `README.md` or `STARTUP.md`:

- **Q6-a. Does the SDRSwitch.com switch protect Tuner 1/Ant A, Tuner 2/Ant B,
  or both?** Every "SDR Switch disconnects the antenna during TX" comment
  predates the RSPduo and describes a single-receiver world.
- **Q6-b. Is RX2/Ant B behind any PTT-driven protection at all?** If it is
  not, then during TX it sees the full near-field from the TX antenna with
  no relay in front of it, and the software gate does nothing about that —
  it only discards samples *after* the ADC. This is the one place where the
  §6.1 "nothing is front-end protection" conclusion could actually matter.
- **Q6-c. Where does the SDS-4000S sit in the RF chain** relative to the
  ACOM 06AT tuner and the 1200S? The ~30 ms AUX delay is the only thing
  known about it.
- **Q6-d. Which ACOM antenna port(s) feed which SDR path?** `ANTENNAS`
  (`acom_bridge.py:130-156`) lists A1F SS-25/DXF, A2F unconnected, A3R 40m
  EFHW, A4R dummy load — with the Greyline (memory
  `project_greyline_antenna_pending`) still not reflected. Nothing maps any
  ACOM port to an RSPduo tuner.
- **Q6-e. Is the 150 ms `_POST_TX_HOLD_S` still the right number?** It was
  chosen in `c5f2ee2` for "relay settle + pipeline drain" on the old
  switch. With PTT-to-amp-on now ~75–100 ms, the *release* path through the
  SDS-4000S has never been measured.

**Recommendation: fix `ARCHITECTURE.md` from the answers, in the same pass
that answers them.** A wrong RF diagram is how §6.1's misreading got
written in the first place.

---

## 8. Recommendation

### 8.1 Can be removed now — nothing, with one exception

The honest answer is that **almost none of this should be removed on the
strength of code reading alone.** Every element traces to a reproduced
failure, and one of them (`3b1a673`) was an audio feedback loop that built
for two seconds into a transmitted signal. The operator's note that the
stock transmit scheme "has never been tested with this hardware" cuts both
ways: it means the gating may be unnecessary, and it equally means nobody
knows what happens without it.

The one safe deletion:

- **`server.py:499` `last_ptt = last_ptt`** — a literal no-op.

Safe cleanups that change no behaviour (do them together, in one commit):

- Fix the stale docstring at `sdr_client.py:348-354`.
- Add the missing `tx_active` guard to `Combiner._publish` — this *adds*
  a gate rather than removing one, but it closes the `3b1a673` hole in the
  RX0 copy and costs nothing.
- Update `ARCHITECTURE.md`'s RF diagram once §7 is answered.

### 8.2 Needs a hardware test before any removal

Four tests, in dependency order. **Do not stack them** — one live run per
test, full report before the next change
(`feedback_dont_stack_live_symptom_fixes`).

**T-1. Answer §7 by inspection of the physical station.** Ten minutes with
the coax. Everything below depends on knowing whether RX2 is behind a
switch. No code involved.

**T-2. The stock-scheme test the operator named.** With the console running
but the fast monitor disabled (`_FAST_PTT_POLL_MS` unreachable / task not
created) and the browser mute left as-is, make one SSB voice transmission
at low power on the dummy load, **headphones only**, and listen for the
blast that `c5f2ee2` was written to stop. This is the single test that
decides whether elements #1-#3 can go. It is also the cheapest thing in
this document and it has never been run.

- If there is no blast: the fast monitor, the `tx_mute` broadcast and the
  150 ms hold can be deleted, and with them ~200 requests/second off the
  rigctld serial link — which is **directly upstream of findings G, I and
  J**. That is the largest single win available anywhere in this codebase
  right now.
- If there is a blast: keep #1-#3, and the measurement tells you the real
  RF-to-audio latency budget for the first time.

**T-3. PTT grammar.** While keying by **mic** (not CAT), read `t` from a
raw socket and record whether hamlib returns `1`, `2` or `3`. One line of
`nc`. Decides whether §2.2 is a live defect or a latent one, and it must be
answered before anyone "simplifies" `parse_ptt_reply` to match the
watchdog or vice versa.

**T-4. Raw-socket capture across console startup**, per §2.1, to settle
whether the startup gate is a rigctld-side desync or a real brief PTT.

### 8.3 Should be fixed regardless of any test

**The unguarded `set_rf_power` at `acom_bridge.py:653-656`.** Add the same
`not self.rig.state.ptt` check the band-select path one block up already
has. A CAT power write landing inside a live transmission is not something
any of the four tests above will make acceptable. Small, isolated, and it
matches an existing pattern in the same file.

### 8.4 What this audit does not claim

- It does not claim the gating is unnecessary. It claims the gating is
  **audio hygiene, not protection**, and that the distinction has never
  been tested on this hardware.
- It does not diagnose the startup gate. It ranks three candidates and
  gives the test that separates them.
- It does not answer Q6. The repository cannot; §7 is a question list for
  the operator.
