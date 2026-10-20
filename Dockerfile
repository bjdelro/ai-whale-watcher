# AI Whale Watcher - Whale Copy Trading
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ ./src/
COPY run_whale_copy_trader.py .

# Create directories for logs and data
RUN mkdir -p logs market_logs paper_trades

# Run the whale copy trader
# TODO(security): run as a non-root USER. Not changed yet because the Fly volume
# mounted at /app/market_logs already holds root-owned state files; switching
# users needs a one-off chown of that volume (or an entrypoint that fixes
# ownership) so the live bot can still write its position state.
# nosemgrep: dockerfile.security.missing-user.missing-user
CMD ["python", "run_whale_copy_trader.py"]
