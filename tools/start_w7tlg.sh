#!/bin/bash
# Combined startup: rigctld (if not already up) + console, then opens the
# unified /console shell. Run ~/start_console.sh yourself if you ever need
# to bounce just the console. Monitor/panadapter/dashboard pages still work
# standalone — load them manually (e.g. http://localhost:8000/monitor) if
# you want a page outside the unified shell.

CONSOLE_DIR="$HOME/w7tlg-console"

if pgrep -x rigctld > /dev/null; then
    echo "rigctld already running."
else
    echo "Starting rigctld..."
    RIGCTLD=/Applications/WRLDesktop.app/Contents/Resources/assets/hamlib-binaries/darwin/rigctld
    $RIGCTLD -m 1035 -r /dev/cu.usbserial-01A3286E0 -s 9600 -t 4532 &
    sleep 1
    if pgrep -x rigctld > /dev/null; then
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
