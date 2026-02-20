# Whale Copy Trader: Learning, Diversification & Insider Focus Plan

## Current State (What's Wrong)

After a week of running, the system is **net negative**. Root causes:

1. **No learning loop** -- We copy whales, but never measure which ones are profitable to copy. No feedback means no improvement.
2. **Sports-dominated market mix** -- Polymarket's highest-volume markets are sports (NBA, NFL, etc.), so our whale list skews heavily toward sports bettors. Sports markets are highly efficient with public odds as a baseline; they're the *least* likely to contain insider edge.
3. **Whale selection is backwards** -- We pick wallets by raw monthly PNL (leaderboard sort). This selects for lucky gamblers, not skilled traders. A wallet that bet $500k on one NFL game and won tells us nothing about their next trade.
4. **Flat sizing, no exits** -- Every trade is $1, win or lose. No stop-loss, no take-profit, no time-based exit. Capital sits in stale positions.

---

## Design Philosophy: Find Insiders, Not Gamblers

The original thesis: find wallets that appear to have private information, and copy them. This is most plausible in:

- **Political/regulatory markets** -- Staffers, lobbyists, journalists with advance knowledge
- **Crypto/DeFi markets** -- Insiders with token listing, partnership, or hack knowledge
- **Corporate/finance markets** -- Earnings, M&A, regulatory decisions
- **Obscure/niche markets** -- Low attention = low efficiency = highest information asymmetry

This is *least* plausible in:

- **Major sports** -- Odds are set by professional bookmakers using the same data everyone has. Information edges are rare and quickly priced in. Line movement is driven by public betting volume, not insider knowledge.

The system should **deprioritize sports markets** and **actively seek out less efficient markets** where information advantages are more likely to exist.

---

## Plan: Three Pillars

### Pillar 1: Trader Learning System (the feedback loop)

This is the highest-impact change. Without it, the system is flying blind.

#### 1A. Per-Whale Copy P&L Tracker

**What:** Track realized and unrealized P&L for every position, grouped by the whale that triggered it.

**Implementation:**

- Add a `WhalePerformance` data structure to `WhaleCopyTrader`:
  ```
  whale_address -> {
      total_copies: int,
      wins: int,
      losses: int,
      total_pnl: float,
      avg_return_pct: float,
      best_trade_pnl: float,
      worst_trade_pnl: float,
      last_copy_time: datetime,
      market_categories: {category: {copies, pnl}},
      recent_returns: deque(maxlen=20),  # rolling window
  }
  ```
- Update on every position close (whale_sold, resolved, stop-loss, take-profit, time-exit).
- Persist to the state file alongside positions.

**Decision logic after accumulating data:**

| Condition | Action |
|-----------|--------|
| Whale has 10+ copies, win rate < 35% | **Ban** -- stop copying entirely |
| Whale has 10+ copies, win rate 35-45% | **Demote** -- reduce position size by 50% |
| Whale has 10+ copies, win rate 45-55% | **Neutral** -- standard sizing |
| Whale has 10+ copies, win rate 55-65% | **Promote** -- increase size by 50% |
| Whale has 10+ copies, win rate > 65% | **Star** -- 2x position size, priority alerts |

This creates a self-improving system: bad whales are pruned, good whales get more capital.

#### 1B. Market-Category Performance Tracking

**What:** Track copy performance per market category, not just per whale.

- When fetching market metadata from Gamma API, extract and store the `category` field.
- Aggregate P&L by category: `{politics: {copies: 40, pnl: +$12}, sports: {copies: 80, pnl: -$25}, ...}`.
- After 50+ trades in a category, if the category is net negative with win rate < 40%, add a penalty to the copy score for trades in that category.

#### 1C. Decay-Weighted Performance

**What:** Recent performance matters more than old performance.

- Use exponential decay: trades from the last 24h get 3x weight, 48h get 2x, 7d get 1x, older get 0.5x.
- A whale that was great 2 weeks ago but has had 5 consecutive losses this week should be demoted, not still treated as ELITE.
- Apply decay to both whale tier classification and per-whale copy P&L.

#### 1D. Periodic Performance Report

**What:** Every 6 hours, log a structured performance report:

```
=== PERFORMANCE REPORT ===
Overall: 45 copies, 22 wins, 23 losses, P&L: -$3.20
By Whale:
  Multicolored-Self: 8 copies, 6W/2L, +$2.40 [STAR]
  BreezeScout:       5 copies, 1W/4L, -$3.10 [DEMOTED]
  anoin123:          3 copies, -- (too few to evaluate)
By Category:
  politics: 12 copies, 8W/4L, +$4.20
  sports:   25 copies, 10W/15L, -$8.50  [PENALIZED]
  crypto:    5 copies, 3W/2L, +$0.80
  other:     3 copies, 1W/2L, -$0.70
```

---

### Pillar 2: Market Diversification (away from sports)

#### 2A. Market Category Filter

**What:** Limit the proportion of capital allocated to any single market category, and specifically cap sports.

**Implementation:**

- Fetch market category from Gamma API when evaluating a trade (the `category` field on market metadata).
- Define category allocation limits:
  ```
  MAX_CATEGORY_EXPOSURE_PCT = {
      "sports": 0.15,       # Max 15% of total exposure in sports
      "politics": 0.35,     # Allow more -- higher info asymmetry potential
      "crypto": 0.30,       # Allow more -- insider knowledge is common
      "pop_culture": 0.15,
      "default": 0.25,      # Any unlisted category
  }
  ```
- Before copying, check: `category_exposure / total_exposure < MAX_CATEGORY_EXPOSURE_PCT[category]`.
- If over the limit, skip with a log message: "Category exposure limit reached for sports (15%)".

#### 2B. Market Efficiency Scoring

**What:** Add a "market efficiency" estimate to the copy score. Less efficient markets get a bonus; highly efficient markets get a penalty.

**Efficiency signals (higher = more efficient = less edge):**
- High 24h volume (>$500k) -- lots of attention, prices well-informed
- Many unique traders -- diverse opinions, hard to have private info
- Sports category -- professional odds-setting infrastructure
- Binary market near 50/50 with high volume -- well-arbitraged

**Inefficiency signals (higher = less efficient = more potential edge):**
- Low volume ($10k-$100k range) -- fewer eyes
- Few unique traders (<50)
- Non-sports category
- Multi-outcome market -- harder to price correctly
- Recent market creation (<7 days) -- prices still finding equilibrium

**Scoring adjustment** (added to the existing copy_scorer.py):
```
market_efficiency_adjustment:
  - Highly efficient: -8 to -5 points
  - Moderately efficient: -3 to 0 points
  - Neutral: 0 points
  - Moderately inefficient: +3 to +5 points
  - Highly inefficient: +5 to +8 points
```

This replaces the current flat `market_context` score (0-10 points) with something that actively rewards trades in less efficient markets.

#### 2C. Active Market Discovery

**What:** Instead of only seeing markets that whales happen to trade, proactively scan for interesting markets where insider trading is more likely.

- Periodically (every 30 min) query Gamma API for active markets.
- Filter for non-sports markets with moderate volume ($10k-$200k) that are not near resolution.
- Cross-reference: if a whale we track makes a trade in one of these "interesting" markets, boost the copy score by +10.
- This biases the system toward exactly the kind of markets where insiders would operate: politically sensitive, niche, or newly created.

---

### Pillar 3: Smarter Whale Selection & Execution

#### 3A. Selection Overhaul: ROI + Consistency, Not Raw PNL

**What:** Replace `orderBy=PNL` with a composite ranking.

**New ranking formula:**
```python
roi = monthly_pnl / monthly_volume  # Capital efficiency
consistency = profitable_weeks / total_weeks  # Not just one big win
frequency = min(1.0, trade_count / 30)  # Penalize infrequent traders

composite = (0.4 * roi_percentile +
             0.3 * consistency_percentile +
             0.3 * frequency_score)
```

- Fetch top 100 from leaderboard, compute composite, take top 20.
- Minimum requirements: 30+ trades/month, positive ROI, >0.4 consistency.
- This filters out one-hit wonders and lucky gamblers.

#### 3B. Reduce Active Whale Count

**What:** `ACTIVE_WHALES_MAX`: 50 -> 20, `ACTIVE_WHALES_INITIAL`: 10 -> 8.

More whales = more noise. Each additional whale dilutes signal quality. With the learning system in place (Pillar 1), we'll further prune based on actual copy P&L data.

#### 3C. Position Sizing by Conviction

**What:** Scale trade size with copy score AND whale track record.

```
Base size by score:
  50-60: $1
  60-70: $2
  70-80: $3
  80-90: $5
  90+:   $8

Whale multiplier (from learning system):
  STAR:     1.5x
  PROMOTE:  1.25x
  NEUTRAL:  1.0x
  DEMOTED:  0.5x
  BANNED:   0x (skip)

Category multiplier:
  Net positive category: 1.0x
  Net negative category: 0.5x
  Insufficient data:     0.75x
```

Final size = `base_size * whale_multiplier * category_multiplier`

#### 3D. Active Exit Management

Add stop-loss, take-profit, and time-based exits. Currently the only exits are whale-sold and market-resolved.

```
Exit rules (checked every polling cycle):
1. Stop-loss:   exit if position is down 15%
2. Take-profit: exit if position is up 20%
3. Trailing stop: once up 10%, set trailing stop at 5% below high water mark
4. Time exit:   exit if position hasn't moved >2% in 48 hours (stale)
5. Whale exit:  exit when copied whale sells (existing logic)
6. Resolution:  exit on market resolution (existing logic)
```

#### 3E. Price Slippage Gate

**What:** Before copying a trade, compare the whale's entry price to the current market price. If the market has already moved >3% in the whale's direction, skip -- the alpha is priced in.

```python
current_price = fetch_orderbook_mid(token_id)
whale_price = trade["price"]
slippage = abs(current_price - whale_price)

if slippage > 0.03:
    logger.info(f"Skipping: price moved {slippage:.1%} since whale entry")
    return
```

#### 3F. Stale Signal Filter

**What:** If a trade is more than 60 seconds old by the time we process it, skip it. The 15-second polling interval means most trades are 15-30s old. Trades older than 60s have likely already been arbitraged.

---

## Implementation Order

Organized by expected impact and dependencies:

### Phase 1: Quick Wins (config changes, no new logic)

1. Cap sports exposure at 15% of total portfolio
2. Tighten `EXTREME_PRICE_THRESHOLD` from 0.05 to 0.15
3. Reduce `ACTIVE_WHALES_MAX` from 50 to 20
4. Add price slippage gate (3% threshold)
5. Add stale signal filter (60s threshold)
6. Increase `MIN_WHALE_TRADE_SIZE` from $1 to $50

### Phase 2: Learning System (core feedback loop)

7. Implement per-whale copy P&L tracker
8. Implement whale banning/demotion based on copy P&L
9. Add market category extraction from Gamma API
10. Implement per-category P&L tracking
11. Add periodic performance report logging
12. Implement decay-weighted performance

### Phase 3: Smarter Execution

13. Implement stop-loss / take-profit / trailing stop exits
14. Implement time-based stale position exit (48h)
15. Implement conviction-weighted position sizing
16. Add whale track record multiplier to sizing

### Phase 4: Market Intelligence

17. Add market efficiency scoring to copy_scorer
18. Implement category exposure limits
19. Add active market discovery (proactive scanning)
20. Implement whale selection overhaul (ROI + consistency ranking)

---

## Expected Outcomes

After implementing all phases:

- **Sports trades drop from ~60-70% to ~15%** of the portfolio
- **Bad whales are identified and pruned within 1-2 weeks** of data collection
- **Good whales get more capital**, compounding wins
- **Losses are capped** by stop-loss at -15% instead of riding to resolution
- **Stale capital is freed** by time-based exits, increasing turnover
- **Higher-edge markets** (politics, crypto, niche) get more allocation
- **System improves over time** as the learning loop accumulates data

The key insight: the current system is a static copy machine. This plan turns it into an **adaptive system that learns which whales, markets, and conditions produce profitable copies**, and allocates capital accordingly.
