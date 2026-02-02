# 🐋 Whale Watcher

A real-time monitoring system that tracks large trades on **Polymarket** and **Kalshi** prediction markets, focusing on detecting potential insider activity.

## Features

- **Real-time monitoring** via WebSocket connections
- **Polling fallback** when WebSocket fails
- **New account detection** - flags trades from accounts < 7 days old
- **Large trade alerts** - configurable threshold (default $100k)
- **Last-minute detection** - flags trades near market close
- **Obscure market detection** - flags activity on low-volume markets
- **Telegram alerts** with severity levels and rich formatting
- **SQLite persistence** for trade history and analysis

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Environment

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

Edit `.env`:
```
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
POLYGONSCAN_API_KEY=your_api_key_here  # Optional but recommended
```

#### Getting Telegram Credentials

1. Create a bot with [@BotFather](https://t.me/botfather):
   - Send `/newbot`
   - Follow prompts to name your bot
   - Copy the token provided

2. Get your chat ID:
   - Send a message to your new bot
   - Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   - Find `"chat":{"id":YOUR_CHAT_ID}`

### 3. Test Configuration

```bash
python main.py --test-alert
```

You should receive a test message in Telegram.

### 4. Run the Watcher

```bash
python main.py
```

## Configuration

Edit `config.yaml` to customize behavior:

```yaml
detection:
  new_account_days: 7        # Accounts newer than this are flagged
  large_trade_usd: 100000    # Minimum trade size to alert
  last_minute_secs: 300      # Flag trades within 5 min of close
  obscure_volume_threshold: 50000  # Markets with less volume are "obscure"
  min_alert_score: 60        # Minimum score (0-100) to trigger alert
```

## Alert Scoring

Each trade is scored based on multiple signals:

| Signal | Points | Description |
|--------|--------|-------------|
| Large trade | 30-60 | Based on trade size vs threshold |
| New account | +20-50 | Multiplied for very new accounts |
| Last minute | +15-25 | Based on time until market close |
| Obscure market | +10-15 | Low volume market activity |
| Volume ratio | +5-10 | Trade size vs market daily volume |

Alerts are sent when total score >= 60 (configurable).

## Alert Severity Levels

- ⚪ **Low** (< 60) - Minor signal
- 🟡 **Medium** (60-75) - Moderate signal
- 🟠 **High** (75-90) - Strong signal
- 🔴 **Critical** (90+) - Multiple strong signals

## Telegram Commands

- `/start` - Welcome message
- `/status` - System status and stats
- `/alerts` - Recent alerts
- `/top` - Top whales in last 24h
- `/help` - Help message

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Whale Watcher                         │
├─────────────────────────────────────────────────────────┤
│                                                          │
│   Polymarket Client          Kalshi Client              │
│   ├─ WebSocket              ├─ WebSocket                │
│   └─ Polling                └─ Polling                  │
│           │                        │                     │
│           └────────┬───────────────┘                     │
│                    │                                     │
│            Detection Engine                              │
│            ├─ Wallet Age Check                          │
│            ├─ Trade Size Analysis                       │
│            ├─ Timing Analysis                           │
│            └─ Market Analysis                           │
│                    │                                     │
│             Alert Scoring                                │
│                    │                                     │
│           Alert Manager                                  │
│                    │                                     │
│            Telegram Bot ──────> SQLite DB               │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

## Project Structure

```
whale-watcher/
├── main.py                    # Entry point
├── config.yaml                # Configuration
├── requirements.txt           # Dependencies
├── .env.example              # Environment template
├── src/
│   ├── database/
│   │   ├── models.py         # SQLAlchemy models
│   │   └── db.py             # Database helpers
│   ├── platforms/
│   │   ├── base.py           # Platform interface
│   │   ├── polymarket.py     # Polymarket client
│   │   └── kalshi.py         # Kalshi client
│   ├── detection/
│   │   ├── detectors.py      # Main detection engine
│   │   ├── wallet_age.py     # PolygonScan integration
│   │   └── scoring.py        # Alert scoring
│   └── alerts/
│       ├── telegram_bot.py   # Telegram integration
│       └── alert_manager.py  # Alert routing
└── tests/
```

## Advanced Usage

### Run with custom config

```bash
python main.py --config /path/to/config.yaml
```

### Docker deployment

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["python", "main.py"]
```

```bash
docker build -t whale-watcher .
docker run -d --env-file .env whale-watcher
```

## Troubleshooting

### "Rate limited" errors
- Add a `POLYGONSCAN_API_KEY` to increase rate limits
- The system caches wallet ages for 24h to minimize API calls

### No alerts received
- Check that Telegram credentials are correct
- Run `python main.py --test-alert` to verify
- Check the `whale_watcher.db` file for recorded trades

### WebSocket disconnections
- The system automatically reconnects
- Falls back to polling if WebSocket fails repeatedly

## Disclaimer

This tool is for informational purposes only. Trading on prediction markets involves significant risk. Past whale activity does not guarantee future performance. Always do your own research before making any trades.

## License

MIT
