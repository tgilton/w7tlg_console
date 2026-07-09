#!/bin/bash
# Graceful shutdown: releases the SDR + serial ports via the console's
# lifespan shutdown handler (kill, not kill -9, unless it won't die).

pids=$(lsof -ti tcp:8000)
if [ -n "$pids" ]; then
    echo "Stopping console (pid $pids)..."
    kill $pids
    for i in $(seq 1 10); do
        lsof -ti tcp:8000 > /dev/null 2>&1 || { echo "  stopped."; break; }
        sleep 1
    done
    still=$(lsof -ti tcp:8000)
    if [ -n "$still" ]; then
        echo "  still up after 10s, forcing..."
        kill -9 $still 2>/dev/null
    fi
else
    echo "Console not running."
fi

if pgrep -x rigctld > /dev/null; then
    echo "Stopping rigctld..."
    pkill -x rigctld
fi

echo "Done. Amp/radio power is separate — switch those at the units."
