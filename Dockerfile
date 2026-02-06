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
CMD ["python", "run_whale_copy_trader.py"]
