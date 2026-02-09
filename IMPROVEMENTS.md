# Whale Copy Trader: Improvement Plan

After reviewing the full codebase and considering that running for a few days has
resulted in roughly break-even performance, here are the core problems and proposed
fixes, ordered by expected impact.

---

## Problem 1: We're copying the wrong whales

**Current approach**: Fetch the top 50 wallets by *monthly PNL* from the Polymarket
leaderboard. Anyone in the top 50 gets copied.

**Why this doesn't work well**:
- Monthly PNL is dominated by a few big bets. A wallet could be #3 on the
  leaderboard because they put $500k on one event that resolved in their favor.
  That tells us nothing about whether their *next* trade has edge.
- Many top PNL wallets are high-variance gamblers, not skilled traders. They show up
  on the leaderboard due to survivorship bias — for every one of them, there are 50
  wallets that made the same bet and lost.
- The leaderboard doesn't distinguish between *realized skill* and *realized luck*.

**Proposed fix — Whale Selection Overhaul**:

1. **Switch from PNL-ranked to ROI + consistency-ranked whale selection**.
   Instead of sorting by raw monthly profit, fetch wallets and rank by:
   - `roi = monthly_pnl / monthly_volume` (capital efficiency)
   - `consistency = number_of_profitable_weeks / total_weeks_active`
   - `trade_count` (filter out one-hit wonders with < 30 trades/month)
   - Composite: `rank_score = 0.4 * roi_percentile + 0.3 * consistency_percentile + 0.3 * volume_percentile`

2. **Require minimum trade frequency**. A whale with 3 trades this month and
   $900k PNL is useless to copy — we need wallets that trade *regularly* so we
   actually get signals. Minimum: 30 trades/month (roughly 1/day).

3. **Track per-whale copy P&L**. This is the biggest gap. We have no feedback loop
   telling us which whales are actually profitable *to copy*. A whale can be
   profitable themselves but unprofitable to copy (due to latency, different sizing,
   etc.). Add a `copy_pnl_by_whale` tracker and after 1-2 weeks, drop whales whose
   copies are net negative. This is the single most impactful change.

4. **Cap the active whale list at 15-20, not 50**. More whales = more noise.
   Each additional whale dilutes signal quality. Start with 10, only expand to 20
   based on the per-whale P&L data.

---

## Problem 2: No distinction between market types

**Current approach**: Copy any trade on any market, with only basic volume and
hours-to-close filters.

**Why this doesn't work well**:
- A trader who is elite at political markets may be average at sports or crypto.
- High-profile markets (presidential elections, major sports) are *extremely*
  efficient — prices reflect consensus quickly. Whale trades in these markets
  likely have no edge because the information is already priced in.
- Obscure/niche markets are where information asymmetry (and thus edge) is most
  likely to exist.

**Proposed fix — Market-Aware Filtering**:

1. **Categorize markets** by type (politics, sports, crypto, finance, pop culture,
   etc.) using the `category` field from Gamma API. Track whale performance *per
   category* — a whale might be ELITE in crypto markets but BAD in sports.

2. **Prefer less efficient markets**. Add a scoring bonus for markets with:
   - Lower total volume (less attention = more mispricing)
   - Fewer unique traders
   - Higher price volatility (prices still moving = not yet settled)
   Conversely, penalize highly liquid, well-followed markets where whale trades
   are unlikely to carry private information.

3. **Avoid binary-outcome markets near extremes**. If a market is at 92% YES,
   copying a whale buying YES has terrible risk/reward (risk 92 cents to make 8).
   The current `EXTREME_PRICE_THRESHOLD` of 5% is too loose. Set it to 15% — skip
   any market priced above 85% or below 15%.

---

## Problem 3: No meaningful exit strategy

**Current approach**: Wait for the whale to sell, or for the market to resolve.

**Why this doesn't work well**:
- We detect whale exits with the same latency as entries — by the time we see
  the whale sold, price has already moved against us.
- Some whales hold for weeks. Our paper capital is locked up in stale positions
  instead of being deployed on fresh signals.
- There's no stop-loss or take-profit mechanism.

**Proposed fix — Active Position Management**:

1. **Time-based exit**. If a position hasn't moved more than 2% in either direction
   after 48 hours, close it. The whale's information is likely stale.

2. **Take-profit at 15-20%**. If we bought at 0.50 and price hits 0.65-0.70,
   take the profit rather than waiting for resolution. Prediction markets can
   revert.

3. **Stop-loss at -15%**. Cut losers early. If we bought at 0.50 and it drops
   to 0.35, exit. Don't wait for whale exit or resolution.

4. **Trailing stop after profit**. Once a position is up 10%, set a trailing stop
   at 5% below the high-water mark. Locks in gains while allowing runners.

5. **Scale out of positions proportionally to the whale**. If the whale reduces
   by 50%, we reduce by 50% too, rather than waiting for full exit.

---

## Problem 4: Position sizing is flat and too small

**Current approach**: Every copy trade is $1, regardless of conviction.

**Why this doesn't work well**:
- A 95-score STRONG_COPY signal from an ELITE wallet gets the same $1 as a
  51-score marginal COPY from a GOOD wallet. This wastes edge.
- With $1 trades, even a 60% win rate barely covers the bid-ask spread.

**Proposed fix — Conviction-Weighted Sizing**:

1. **Scale position size with copy score**:
   - Score 50-60: $1 (base)
   - Score 60-70: $2
   - Score 70-80: $3
   - Score 80-90: $5
   - Score 90+: $8-10
   This is a Kelly-style approach — bet more when edge is higher.

2. **Scale with whale tier**:
   - ELITE whale with STRONG_COPY: 2x multiplier
   - GOOD whale with COPY: 1x multiplier
   This stacks with the score-based sizing.

3. **Reduce sizing when on a losing streak**. If the last 5 copies are net
   negative, halve position sizes until 3 winners in a row.

---

## Problem 5: Latency cost is unaccounted for

**Current approach**: We poll whale trades at 15-second intervals. By the time we
detect + evaluate + copy, the market has likely moved.

**Why this matters**:
- In prediction markets, large whale trades move the price immediately. If a whale
  buys $50k of YES at 0.55, by the time we see it the price may already be 0.58-0.60.
- We're systematically buying at *worse* prices than the whale. This is a hidden cost
  that erodes all edge.

**Proposed fix — Latency-Aware Execution**:

1. **Measure and track copy latency**. Log the time between whale trade and our
   copy, and the price difference. This gives us hard data on how much latency
   costs.

2. **Use limit orders, not market orders**. Place a limit order at the whale's
   price or slightly above (e.g., whale price + 1-2%). If the market has moved
   more than 3% past the whale's entry, skip the trade — the edge is gone.

3. **Stale signal filter**. If we detect a whale trade that happened more than
   60 seconds ago (due to polling delay, processing, etc.), discard it. The
   `first_fill_time` on the CanonicalTrade should be compared against current time.

4. **Price-slippage gate**. Before copying, fetch the current orderbook price.
   If `current_price - whale_entry_price > 3%`, skip. The alpha has been
   arbitraged away.

---

## Problem 6: The scoring system underweights the most predictive signal

**Current approach**: Wallet quality is 40% of the score, which is correct
directionally, but the wallet quality signal itself is weak because it's based
on our own limited markout data (we've only been running for days).

**Proposed fix — Bootstrap wallet quality from external data**:

1. **Use the Polymarket leaderboard's historical data to pre-seed wallet stats**.
   The leaderboard API returns PNL, volume, and trade count. We can compute ROI
   and trade frequency before we ever observe a single trade ourselves.

2. **Weight recent performance more heavily**. Our markout tracker treats all
   trades equally. Use exponential decay — trades from the last 24 hours get 3x
   weight vs. trades from 7 days ago. Whales can go cold.

3. **Add a "conviction" signal**. When a whale makes a trade that's large
   relative to *their own history* (not just absolute size), that's a much
   stronger signal than a routine trade. Currently `size_vs_wallet_avg` is scored
   but capped at only 5 points out of 100. Increase this to 10-15 points — it's
   one of the most predictive features in copy trading.

---

## Problem 7: No correlation/conflict awareness across positions

**Current approach**: Each copy trade is evaluated independently. We could end up
with 10 positions that are all correlated (e.g., all betting on the same political
outcome across slightly different markets).

**Proposed fix**:

1. **Track category exposure**. Limit exposure per category (politics, sports, etc.)
   to e.g., 30% of total capital. Don't let the portfolio become a concentrated
   bet on one theme.

2. **Detect correlated markets**. Markets like "Will X win the election?" and
   "Will X's party win?" are highly correlated. If we're already long on one,
   reduce sizing on the other.

3. **Cross-whale conflict detection exists but is unused for sizing**. The cluster
   detection logic detects when multiple whales trade the same market, but this
   signal isn't used to *boost* sizing. If 3 independent ELITE whales all buy YES
   on the same market within 5 minutes, that's a much stronger signal than one
   whale buying.

---

## Implementation Priority

| Priority | Change | Expected Impact | Effort |
|----------|--------|-----------------|--------|
| **P0** | Track per-whale copy P&L and drop bad whales | High | Medium |
| **P0** | Price-slippage gate (skip if price moved >3%) | High | Low |
| **P0** | Stop-loss and take-profit on positions | High | Medium |
| **P1** | Conviction-weighted position sizing | Medium-High | Low |
| **P1** | Increase EXTREME_PRICE_THRESHOLD to 15% | Medium | Trivial |
| **P1** | Stale signal filter (>60s = skip) | Medium | Low |
| **P1** | Reduce active whales to 15-20 max | Medium | Trivial |
| **P2** | Market category tracking + per-category whale performance | Medium | Medium |
| **P2** | Time-based exit (48h stale position cleanup) | Medium | Low |
| **P2** | Switch whale selection from PNL to ROI+consistency | Medium | Medium |
| **P2** | Boost size_vs_wallet_avg scoring weight | Low-Medium | Trivial |
| **P3** | Category exposure limits | Low-Medium | Medium |
| **P3** | Trailing stop after profit | Low-Medium | Medium |
| **P3** | Pre-seed wallet stats from leaderboard API | Low | Medium |

---

## Quick Wins (can implement in minutes)

These are config/constant changes that should help immediately:

1. `EXTREME_PRICE_THRESHOLD`: 0.05 → 0.15 (skip markets at >85% or <15%)
2. `ACTIVE_WHALES_MAX`: 50 → 20 (less noise)
3. `ACTIVE_WHALES_INITIAL`: 10 → 8
4. `MIN_WHALE_TRADE_SIZE`: $1 → $50 (stop copying dust trades from whales)
5. `min_copy_score` in risk limits: 70 → 65 (the score system is well-calibrated;
   70 is too restrictive given we have limited data for wallet quality)
6. `size_vs_wallet_avg` scoring: increase cap from 5 → 12 points
7. Add price-delta check before execution (if current price > whale price + 3%, skip)

---

## Summary

The core issue is that **the system treats all whales as equally worth copying and
all trades as equally worth taking**. In reality:

- Most "whales" on the leaderboard are there due to variance, not skill
- Even skilled whales aren't worth copying if we can't get similar prices
- Flat position sizing wastes edge on high-conviction signals
- Lack of exit management means capital is locked in stale positions
- No feedback loop exists to learn which whales are actually profitable to copy

The single highest-ROI change is **tracking per-whale copy P&L and using it to
prune the whale list**. This creates a feedback loop that makes the system
self-improving over time.
