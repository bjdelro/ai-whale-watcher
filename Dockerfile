# AI Whale Watcher - Paper Trading
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ ./src/
COPY run_paper_trading_poll.py .

# Create directories for logs and data
RUN mkdir -p logs market_logs paper_trades

# Run the paper trader
CMD ["python", "run_paper_trading_poll.py"]
