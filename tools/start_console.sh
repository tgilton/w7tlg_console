#!/bin/bash

CONSOLE_DIR="$HOME/w7tlg-console"

if ! pgrep -x rigctld > /dev/null; then
    echo "WARNING: rigctld is not running. Start it first with: ~/start_rigctld.sh"
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

# Every run also lands in a timestamped file under logs/ — the terminal
# output is unchanged, tee just copies it. Findings G/I/J were all diagnosed
# from log captures the operator happened to have; this stops that being
# a matter of remembering to redirect. logs/ is gitignored.
mkdir -p "$CONSOLE_DIR/logs"
LOG_FILE="$CONSOLE_DIR/logs/console-$(date +%Y%m%d-%H%M%S).log"
echo "Logging to $LOG_FILE"

python main.py 2>&1 | tee "$LOG_FILE" &
CONSOLE_PID=$!

echo "Waiting for console on :8000..."
for i in $(seq 1 30); do
    if lsof -i :8000 > /dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

if lsof -i :8000 > /dev/null 2>&1; then
    echo "Console started. Opening in Chrome..."
    open -a "Google Chrome" http://localhost:8000/console
else
    echo "Console failed to start within 15s."
fi

wait $CONSOLE_PID
kill $CAFFEINATE_PID 2>/dev/null
echo "Console stopped cleanly."
