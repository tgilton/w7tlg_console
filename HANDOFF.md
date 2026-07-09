# W7TLG Console — Session Handoff & Mac Studio Bring-Up

> Written 2026-07-09 from the MacBook Pro M4 session, to be picked up by a fresh
> Claude session (and operator) on the **Mac Studio**. The console became
> untenable to start on the MacBook; we are moving back to the Studio (the
> known-good desktop). This file is self-contained: everything needed to bring
> the console up on the Studio is here.
>
> **Fresh Claude session: read this file first, then help execute the
> "Mac Studio bring-up" checklist below.**

## What was wrong (root causes, confirmed by evidence this session)

1. **The SDR wedge is in the SDRplay *daemon*, not the device.** The service
   `com.sdrplay.service` (LaunchDaemon at
   `/Library/LaunchDaemons/com.sdrplay.service.plist`, `KeepAlive=true`) gets
   stuck in an endless `libusb ... bulk transfer failed ... pipe is stalled
   (code = 0xe000404f)` loop (see `/Library/Logs/sdrplayservice_err.log`) and
   accumulates orphaned device handles from every crashed console (visible via
   `ioreg -l | grep sdrplay_apiService`).
   - **Replugging the USB does NOT fix it** — the daemon survives replugs, so the
     wedged layer is never reset. We replugged 3+ times to no effect.
   - **The fix is to restart the daemon:**
     `sudo launchctl kickstart -k system/com.sdrplay.service`
   - The RSPdx-R2 is selected by **hardware type, not serial number**, so it is
     portable across machines (no serial hardcoded anywhere).

2. **The SDR sits on the startup critical path with no timeout.** In
   `dashboard/server.py`, the `lifespan` startup does `await sdr.start()`
   (~line 477) **before** uvicorn binds port 8000. `sdr.start()` →
   `_init_with_retries` → `run_in_executor(self._open_and_init)`
   (`sdr/sdr_client.py:187`) calls native `sdrplay_api_Init` with **no timeout**.
   On a wedged daemon that native call never returns → lifespan never reaches
   `yield` → port 8000 never opens → browser shows **"localhost refused to
   connect."** The process is alive but never listening.

3. **No supervisor; in-process SDR recovery causes the "died then restarted on
   its own" behavior.** Nothing respawns the console (only *rigctld* has a launchd
   job). The apparent self-restart is the in-process SDR stall-watchdog/recovery
   (`_recover`, commit `bc2d2bc`) tearing down and re-initing the SDR in place —
   which drops the WebSocket/UI and, per a prior crash
   (`~/Library/Logs/DiagnosticReports/Python-2026-07-08-203341.ips`), can
   **segfault the entire process**. Blast radius is process-wide: SDR shares the
   process AND the default thread pool with the amp's serial reads.

4. **Running two consoles at once makes it worse** — they fight over the single
   ACOM serial port (`multiple access on port?` reconnect storm) and the single
   RSPdx. Only ever run ONE instance.

5. **Migration surface is small and known** (checklist below). The amp/rig/feed
   code is robust (reconnect loops, timeouts, graceful degradation); fragility is
   concentrated in the native SDR layer.

## Mac Studio bring-up checklist (run/verify in order)

1. **Get the code.** `git clone https://github.com/tgilton/w7tlg_console.git
   ~/w7tlg_console` — or if already cloned: `cd ~/w7tlg_console && git pull`.
   Current `main` (incl. commit `bc2d2bc`) was pushed from the MacBook so the
   Studio gets the exact running code.

2. **Re-detect and set `ACOM_PORT`** — *highest-risk item*; the FTDI device path
   differs per machine and per USB port. Run `ls /dev/cu.usbserial-*` and identify
   the FTDI adapter (ACOM 1200S) vs. the SiLabs/CP210x (FT-991A CAT). Then edit
   `dashboard/server.py:51`:
   ```python
   ACOM_PORT = "/dev/cu.usbserial-XXXXXXXX"   # <- the Studio's FTDI path
   ```
   (On the MacBook it was `/dev/cu.usbserial-A92518IM`; an even older machine used
   `A9V19CH7`. It will be different again on the Studio.)

3. **Fix rigctld serial paths for the Studio** in both places (currently pinned to
   the MacBook's `/dev/cu.usbserial-01A3286E0` / `/dev/tty.SLAB_USBtoUART`):
   - `~/start_rigctld.sh` (`RIG_PORT`)
   - `~/Library/LaunchAgents/com.hamlib.rigctld.plist` (`-r` path, and the `-p`
     PTT path). Reload with `launchctl unload/load` if you use the launchd job.
   Verify: `rigctl -m 1035 -r <port> -s 9600 f` returns the dial frequency.

4. **Recreate the venv** — do NOT copy `.venv` (it is bound to the MacBook's
   Homebrew paths; Python 3.12 required):
   ```
   brew install python@3.12
   cd ~/w7tlg_console
   python3.12 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

5. **Install DeepFilterNet (optional noise reduction; compiles from Rust).**
   Ensure `~/.cargo/bin` is on PATH (`brew install rust` or rustup), then
   `tools/install_deepfilternet.sh`. The console degrades gracefully if this is
   absent — safe to skip initially.

6. **Create `.env`** (gitignored) from `.env.example`:
   `ANTHROPIC_API_KEY=...` (required for AI advisor), optionally `HAMQTH_*` /
   `QRZ_*` for callsign lookup.

7. **Verify external services exist on the Studio:**
   - SDRplay API 3.15.1 installed; `com.sdrplay.service` running;
     `/usr/local/lib/libsdrplay_api.dylib` present.
   - BlackHole 2ch virtual audio device (`brew install blackhole-2ch`, then
     reboot). Needed for digital-mode audio; degrades gracefully if missing.
   - WSJT-X UDP multicast set to `224.0.0.1:2237` on `lo0` (matches
     `wsjtx/udp_listener.py`).
   - Dropbox tree synced for RUMLogNG log
     `~/Library/CloudStorage/Dropbox/Research/Amateur_Radio/Logs/TG Log 001.rlog`
     (award tracker; falls back to WSJT-X ADIF if absent).

8. **Recreate the `$HOME` launch scripts** if not present (they are NOT in the
   repo). Verbatim contents are in the appendix below — remember to update the
   serial device paths for the Studio.

## First-start procedure (Studio)

1. Start rigctld (`~/start_rigctld.sh`), then the console (`~/start_w7tlg.sh`,
   which also opens Chrome, or `python main.py` in the foreground).
2. Watch the log: `tail -f /tmp/w7tlg_console.log`. Success sequence:
   `Starting W7TLG Console...` → `SdrClient started` → `W7TLG Console running`
   (port 8000 binds right after the last line).
3. **If the SDR hangs/wedges: restart the daemon, do NOT replug** —
   `sudo launchctl kickstart -k system/com.sdrplay.service`, then start ONE
   console. Ignore any in-app "needs a replug" prompt — it does not apply to this
   failure mode.
4. Run only ONE console instance.

## Robustness roadmap (after the Studio is up — strategy TBD with operator)

Recommended (evidence-backed): **isolate the SDR in its own subprocess + harden**,
keeping the well-structured amp/rig/feed code. Open decisions: (Q1) isolate-and-
harden vs. rewrite-SDR-only vs. full-rewrite; (Q2) sequencing. Recommendation:
isolate + harden, do it after the Studio is stable.

- Move `SdrClient` + `sdr/sdrplay_capi.py` into a separate process (socket/pipe
  boundary, reuse the existing `available`/`unavailable` status protocol) so a
  native crash/wedge becomes "SDR feed down", never a whole-console outage.
- Wrap native init in `asyncio.wait_for` timeout; move SDR startup OFF the
  critical path (bind the port first, bring the SDR up in the background).
- Give SDR native calls a dedicated (non-default) thread pool so a wedge can't
  starve the amp's serial reads.
- Make the `bc2d2bc` recovery crash-safe: on stall, mark unavailable + show a
  badge; do NOT call back into the vendor lib to re-init in-process.
- Supervise the console via launchd (like rigctld) so a crash auto-recovers.
- Move `ACOM_PORT` / rigctld paths to `.env` or auto-detect so machine moves need
  no code edits.
- Add tests (currently ZERO) for startup, graceful degradation, serial reconnect,
  and recovery.

---

## Appendix — `$HOME` launch scripts (verbatim; update serial paths for Studio)

### `~/start_rigctld.sh`
```zsh
#!/bin/zsh
# Start Hamlib rigctld for the FT-991A.
RIG_PORT="/dev/cu.usbserial-01A3286E0"   # <- UPDATE for the Studio
RIG_BAUD="9600"
RIG_MODEL="1035"   # Yaesu FT-991A
if pgrep -x rigctld >/dev/null; then
  echo "rigctld already running (pid $(pgrep -x rigctld))."; exit 0
fi
echo "Starting rigctld: model $RIG_MODEL on $RIG_PORT @ $RIG_BAUD ..."
exec rigctld -m "$RIG_MODEL" -r "$RIG_PORT" -s "$RIG_BAUD" -t 4532
```

### `~/start_console.sh`
```zsh
#!/bin/zsh
set -e
REPO="$HOME/w7tlg_console"
cd "$REPO"
source .venv/bin/activate
echo "Starting W7TLG console -> http://localhost:8000/console  (Ctrl+C to stop)"
exec python main.py "$@"
```

### `~/start_w7tlg.sh` (orchestrator: rigctld + console + Chrome; idempotent)
```zsh
#!/bin/zsh
REPO="$HOME/w7tlg_console"
LOG_LATEST="/tmp/w7tlg_console.log"
# 1. rigctld
if pgrep -x rigctld >/dev/null; then
  echo "rigctld already running (pid $(pgrep -x rigctld))."
else
  echo "Starting rigctld..."
  nohup "$HOME/start_rigctld.sh" >/tmp/rigctld.log 2>&1 & disown
  sleep 2
fi
# 2. console
if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "console already running on :8000."
else
  LOG="/tmp/w7tlg_console_$(date +%Y%m%d_%H%M%S).log"
  echo "Starting console (log: $LOG)..."
  cd "$REPO"; source .venv/bin/activate
  nohup python main.py >"$LOG" 2>&1 & disown
  ln -sf "$LOG" "$LOG_LATEST"
  ls -1t /tmp/w7tlg_console_*.log 2>/dev/null | tail -n +16 | xargs rm -f 2>/dev/null
  for i in {1..15}; do
    lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1 && break; sleep 1
  done
fi
# 3. browser
open -a "Google Chrome" "http://localhost:8000/console"
echo "W7TLG console -> http://localhost:8000/console"
```

### `~/stop_w7tlg.sh` (graceful — releases SDR + serial via lifespan shutdown)
```zsh
#!/bin/zsh
pids=(${(f)"$(lsof -ti tcp:8000 2>/dev/null)"})
if (( ${#pids} )); then
  echo "Stopping console (pid ${pids})..."
  kill ${pids}
  for i in {1..10}; do
    lsof -ti tcp:8000 >/dev/null 2>&1 || { echo "  stopped."; break; }; sleep 1
  done
  lsof -ti tcp:8000 >/dev/null 2>&1 && kill -9 ${(f)"$(lsof -ti tcp:8000)"} 2>/dev/null
else
  echo "Console not running."
fi
pgrep -x rigctld >/dev/null && { echo "Stopping rigctld..."; pkill -x rigctld; }
echo "Done. Amp/radio power is separate — switch those at the units."
```

### `~/Library/LaunchAgents/com.hamlib.rigctld.plist` (optional supervised rigctld)
Currently pinned to the MacBook's `-r /dev/tty.usbserial-01A3286E0` and
`-p /dev/tty.SLAB_USBtoUART`. Update both paths for the Studio before loading with
`launchctl load ~/Library/LaunchAgents/com.hamlib.rigctld.plist`. (It was NOT
loaded on the MacBook; rigctld was started by `start_w7tlg.sh` instead.)
