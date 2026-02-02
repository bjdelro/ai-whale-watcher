# AI Whale Watcher - Market Monitor
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ ./src/
COPY run_market_monitor.py .
COPY .env .

# Create directories for logs and data
RUN mkdir -p logs market_logs paper_trades

# Run the market monitor
CMD ["python", "run_market_monitor.py"]
