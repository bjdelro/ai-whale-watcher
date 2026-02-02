#!/bin/bash
# Stop paper trading gracefully

cd "$(dirname "$0")"

if [ -f paper_trading.pid ]; then
    PID=$(cat paper_trading.pid)
    echo "Stopping paper trading (PID: $PID)..."

    # Send SIGINT for graceful shutdown (shows report)
    kill -INT $PID 2>/dev/null

    # Wait a bit for graceful shutdown
    sleep 5

    # Check if still running
    if kill -0 $PID 2>/dev/null; then
        echo "Force killing..."
        kill -9 $PID 2>/dev/null
    fi

    rm paper_trading.pid
    echo "Stopped."
else
    echo "No paper_trading.pid file found. Not running?"
fi
