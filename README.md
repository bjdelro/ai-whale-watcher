# 🐋 AI Whale Watcher

A real-time system that monitors and copies profitable whale traders on **Polymarket** prediction markets.

## What It Does

1. **Monitors** the top profitable wallets on Polymarket in real-time
2. **Detects** when whales make trades (buys/sells)
3. **Copies** their trades automatically (paper or live)
4. **Exits** positions when whales sell or markets resolve

## Trading Modes

| Mode | Description | Risk |
|------|-------------|------|
| **Paper** (default) | Simulates trades, tracks P&L | None |
| **Live Dry Run** | Logs what it would trade, no execution | None |
| **Live Real** | Executes real trades with real money | Real $ |

## Platform Support

| Platform | Monitoring | Paper Trading | Live Trading |
|----------|------------|---------------|--------------|
| Polymarket | ✅ | ✅ | ✅ |
| Kalshi | ⚠️ Partial | ❌ | ❌ |

> **Note:** Live trading currently only supports Polymarket via the `py-clob-client` SDK.

---

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Run Paper Trading (No Risk)

```bash
python run_whale_copy_trader.py
```

This will:
- Monitor whale wallets
- Simulate copying their trades
- Track paper P&L
- Log everything to console

---

## Live Trading

### Prerequisites Checklist

Before enabling live trading, complete this checklist:

- [ ] **Polygon Wallet** - Create/have a wallet on Polygon network
- [ ] **Private Key** - Export your wallet's private key
- [ ] **USDC Funded** - Deposit USDC on Polygon (start with $50-100 for testing)
- [ ] **MATIC for Gas** - Small amount of MATIC for transaction fees (~$5)
- [ ] **Environment Variable** - Set `PRIVATE_KEY` in your environment
- [ ] **Paper Trading Validated** - Run paper trading for 24-48h to verify strategy
- [ ] **Risk Limits Set** - Decide max per trade and max total exposure

### Environment Setup

```bash
# Set your private key (required for live trading)
export PRIVATE_KEY=0x_your_private_key_here

# Or add to .env file
echo "PRIVATE_KEY=0x_your_private_key_here" >> .env
```

> ⚠️ **SECURITY**: Never commit your private key to git. Keep it in environment variables or `.env` (which is gitignored).

### Live Trading Commands

```bash
# Paper trading only (default, safe)
python run_whale_copy_trader.py

# Live DRY RUN - logs trades but doesn't execute (safe)
python run_whale_copy_trader.py --live

# Live REAL trading - executes actual trades (uses real money!)
python run_whale_copy_trader.py --live --live-real

# With custom limits
python run_whale_copy_trader.py --live --live-real \
  --live-max-trade 10 \
  --live-max-exposure 100
```

### CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--max-trade` | $1.00 | Max per paper trade |
| `--max-total` | $10,000 | Max paper exposure |
| `--live` | off | Enable live trading (dry run) |
| `--live-real` | off | Execute real orders (requires `--live`) |
| `--live-max-trade` | $5.00 | Max per live trade |
| `--live-max-exposure` | $50.00 | Max live exposure |

---

## How It Works

### 1. Whale Monitoring

The system tracks the top 20+ most profitable Polymarket wallets:
- Polls their recent trades every 15 seconds
- Detects new BUY and SELL activity
- Filters out trades older than 5 minutes (prevents replaying history)

### 2. Trade Evaluation

When a whale trades, the system evaluates:
- **Trade size** - Minimum $100 whale trade to copy
- **Price** - Skips extreme prices (>95% or <5%) as market is already decided
- **Hedging** - Detects if whale is hedging vs taking directional bets
- **Exposure** - Checks if we have room for more positions

### 3. Position Management

**Entry:** When a whale buys, we buy the same outcome at the same price.

**Exit:** Positions close when:
- Whale sells their position (we copy the sell)
- Market resolves (price hits 0% or 100%)

### 4. P&L Tracking

Both paper and live modes track:
- Open positions and unrealized P&L
- Closed positions and realized P&L
- Win rate (winners vs losers)
- Projected returns (hourly/daily/weekly)

---

## Deployment (Render)

The app is configured for Render deployment:

### 1. Connect Repository

Link your GitHub repo to Render as a **Background Worker**.

### 2. Set Environment Variables

In Render dashboard, add:
```
PRIVATE_KEY=0x_your_key_here
PYTHONUNBUFFERED=1
```

### 3. Deploy

Render will auto-deploy on push to `main`.

### Render Configuration

See `render.yaml` for the deployment config:
- Docker-based worker service
- $7/month starter plan
- Ohio region
- Persistent disk for logs

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                   Whale Copy Trader                         │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   ┌─────────────────┐     ┌─────────────────┐              │
│   │  Whale Monitor  │     │ Unusual Scanner │              │
│   │  (Top 20 wallets)│     │ (Large trades)  │              │
│   └────────┬────────┘     └────────┬────────┘              │
│            │                       │                        │
│            └───────────┬───────────┘                        │
│                        │                                    │
│              ┌─────────▼─────────┐                         │
│              │  Trade Evaluator  │                         │
│              │  - Size filter    │                         │
│              │  - Price filter   │                         │
│              │  - Hedge detect   │                         │
│              └─────────┬─────────┘                         │
│                        │                                    │
│         ┌──────────────┴──────────────┐                    │
│         │                             │                    │
│   ┌─────▼─────┐               ┌───────▼───────┐           │
│   │  Paper    │               │  Live Trader  │           │
│   │  Trader   │               │  (Optional)   │           │
│   └─────┬─────┘               └───────┬───────┘           │
│         │                             │                    │
│   ┌─────▼─────┐               ┌───────▼───────┐           │
│   │ Paper P&L │               │ Polymarket    │           │
│   │ Tracking  │               │ CLOB API      │           │
│   └───────────┘               └───────────────┘           │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## Safety Features

### Paper Trading
- No real money at risk
- Full P&L simulation
- Position tracking

### Live Trading
- **Dry run mode** by default (even with `--live`)
- **5-second warning** before real trading starts
- **Max order limits** prevent large single trades
- **Max exposure limits** cap total position size
- **Timestamp filtering** prevents replaying old trades

---

## Monitoring

### Logs

The system logs to console with timestamps:
```
14:30:15 | INFO | 🐋 WHALE TRADE: sovereign2013 BUY $1,137 of Volynets @ 21.4%
14:30:15 | INFO |    📝 PAPER BUY: $1.00 of Volynets @ 21.4% (4.67 shares)
14:30:15 | INFO | Poll #4: Whales: 0 trades | Positions: 2 open, 0 closed | P&L: $+0.00
```

### Periodic Reports

Every 5 minutes, a full report is logged:
- Portfolio stats
- Open positions
- P&L summary
- Projected returns

---

## Troubleshooting

### "No trades being copied"
- Check that whale wallets are active (some days are slow)
- Verify timestamp filter isn't too aggressive
- Look for "Skipping" messages in logs

### "Live trading not working"
- Verify `PRIVATE_KEY` is set correctly
- Ensure wallet has USDC on Polygon
- Check you're using `--live --live-real` flags
- Look for error messages in logs

### "Old trades being replayed"
- This was fixed - trades older than 5 minutes are filtered
- Ensure you're on the latest `main` branch

---

## Disclaimer

This software is for educational and informational purposes only.

- **Prediction markets involve significant risk** - you can lose your entire investment
- **Past whale performance does not guarantee future returns**
- **The system may have bugs** - always monitor your positions
- **Start small** - test with minimal amounts before scaling up

Use at your own risk. The authors are not responsible for any financial losses.

---

## License

MIT
