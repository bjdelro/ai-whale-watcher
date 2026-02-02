#!/bin/bash
# Start paper trading in background with logging
# Run with: ./start_paper_trading.sh

cd "$(dirname "$0")"

# Load environment
source venv/bin/activate

# Create logs directory
mkdir -p logs

# Log file with timestamp
LOG_FILE="logs/paper_trading_$(date +%Y%m%d_%H%M%S).log"

echo "Starting paper trading (polling mode)..."
echo "Log file: $LOG_FILE"
echo "To monitor: tail -f $LOG_FILE"
echo "To stop: ./stop_paper_trading.sh"

# Run POLLING version (more reliable for long-running)
nohup python3 run_paper_trading_poll.py \
    --max-trade 1.0 \
    --max-total 100.0 \
    --min-score 70 \
    --max-daily-loss 20.0 \
    > "$LOG_FILE" 2>&1 &

# Save PID
echo $! > paper_trading.pid
echo "PID: $(cat paper_trading.pid)"
echo ""
echo "Paper trading started! Check logs with:"
echo "  tail -f $LOG_FILE"
