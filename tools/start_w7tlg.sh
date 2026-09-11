#!/bin/bash
# Combined startup: rigctld (if not already up) + console, then opens the
# unified /console shell. Run ~/start_console.sh yourself if you ever need
# to bounce just the console. Monitor/panadapter/dashboard pages still work
# standalone — load them manually (e.g. http://localhost:8000/monitor) if
# you want a page outside the unified shell.

CONSOLE_DIR="$HOME/w7tlg-console"

RIGCTLD=/Applications/WRLDesktop.app/Contents/Resources/assets/hamlib-binaries/darwin/rigctld
RIG_DEVICE=/dev/cu.usbserial-01A3286E0

# Don't just check for any process named "rigctld" — other apps (e.g. Winlink
# helpers) bundle their own hamlib and may already be squatting on :4532
# bound to the wrong serial device. Verify it's actually *our* rigctld
# talking to the FT-991A before trusting it. (This copy had drifted out of
# sync with the deployed ~/start_w7tlg.sh, which already had this fix —
# resynced 2026-08-30 along with the CAT baud bump below.)
if pgrep -f "$RIGCTLD.*-r $RIG_DEVICE" > /dev/null; then
    echo "rigctld already running for FT-991A."
else
    stale=$(lsof -ti :4532)
    if [ -n "$stale" ]; then
        echo "Port 4532 is held by a different rigctld (PID $stale) — killing it."
        kill -9 $stale 2>/dev/null
        sleep 1
    fi
    echo "Starting rigctld..."
    $RIGCTLD -m 1035 -r "$RIG_DEVICE" -s 38400 -t 4532 &
    sleep 1
    if pgrep -f "$RIGCTLD.*-r $RIG_DEVICE" > /dev/null; then
        echo "rigctld started successfully on port 4532"
    else
        echo "WARNING: rigctld failed to start — continuing anyway"
    fi
fi

existing=$(lsof -ti :8000)
if [ -n "$existing" ]; then
    echo "Stopping existing console (PID $existing)..."
    kill -9 $existing 2>/dev/null
    sleep 1
fi

echo "Starting sleep prevention (caffeinate)..."
caffeinate -s -i &
CAFFEINATE_PID=$!

echo "Starting W7TLG Console..."
cd "$CONSOLE_DIR"
source venv/bin/activate

python main.py &
CONSOLE_PID=$!

echo "Waiting for console on :8000..."
for i in $(seq 1 30); do
    if lsof -i :8000 > /dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

if lsof -i :8000 > /dev/null 2>&1; then
    echo "Console started. Opening console..."
    open -a "Google Chrome" http://localhost:8000/console
else
    echo "Console failed to start within 15s."
fi

wait $CONSOLE_PID
kill $CAFFEINATE_PID 2>/dev/null
echo "Console stopped cleanly."
