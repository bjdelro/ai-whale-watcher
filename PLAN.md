# Whale Copy Trader: Remaining Improvements Plan

## What's Already Done (merged in PR #6)

The following are already implemented and working:

- Per-whale copy P&L tracking + auto-pruning (prune after 5 bad copies)
- Price-slippage gate (skip if current price > whale entry + 3%)
- Stop-loss (-15%), take-profit (+20%), stale position cleanup (48h)
- Conviction-weighted position sizing (rank + track record + trade size)
- Config tightening: extreme threshold 15%, max 20 whales, $50 min trade, 120s freshness
- Boosted `size_vs_wallet_avg` scoring (5pt -> 12pt max)
- Per-whale P&L leaderboard in periodic reports
- Sell-all-positions script for clean portfolio reset

---

## What's Left: Two Pillars

### Pillar A: Market Diversification & Category Intelligence

**Problem:** The system still treats all markets equally. Sports markets dominate whale activity but are the *least* likely to contain insider edge due to professional odds-setting infrastructure. The system should actively favor politics, crypto, and niche markets where information asymmetry is more plausible.

#### A1. Market Category Extraction

**What:** When evaluating a trade, fetch and cache the market's category from the Gamma API.

**Implementation:**
- In `_evaluate_trade`, look up the market's `condition_id` via the Gamma API `/markets` endpoint.
- Extract the `category` field (e.g., "sports", "politics", "crypto", "pop_culture").
- Cache market -> category mappings in a dict (markets don't change category).
- Store the category on each `CopiedPosition` for aggregation.

#### A2. Per-Category P&L Tracking

**What:** Track copy P&L broken down by market category, just as we already do per-whale.

**Implementation:**
- Add `_category_copy_pnl: Dict[str, dict]` with same structure as `_whale_copy_pnl`.
- Update on every position close.
- Display in periodic reports alongside per-whale leaderboard.
- Persist to state file.

#### A3. Category Exposure Limits

**What:** Cap the percentage of total exposure in any single category, with sports capped lowest.

**Implementation:**
```python
MAX_CATEGORY_EXPOSURE_PCT = {
    "sports": 0.15,       # Max 15% in sports
    "politics": 0.35,     # Higher info asymmetry potential
    "crypto": 0.30,       # Insider knowledge common
    "pop_culture": 0.15,
    "default": 0.25,
}
```

- Before copying, compute `category_exposure / total_exposure`.
- If over limit, skip with log: "Category exposure limit reached for sports (15%)".

#### A4. Market Efficiency Score Adjustment

**What:** Replace the flat `market_context` score (0-10 pts) in `copy_scorer.py` with an efficiency-aware adjustment.

**Scoring:**
- Highly efficient (high volume, sports, many traders): -5 to -8 points
- Neutral: 0 points
- Inefficient (low volume, niche, few traders, new market): +3 to +8 points

This biases the copy score toward less efficient markets where insider edge is more plausible.

---

### Pillar B: Smarter Learning & Selection

#### B1. Decay-Weighted Performance

**What:** Recent performance matters more than old performance.

**Implementation:**
- Weight trades by recency: last 24h = 3x, 48h = 2x, 7d = 1x, older = 0.5x.
- Apply to whale pruning/promotion decisions and conviction sizing.
- A whale with 5 consecutive recent losses should be deprioritized even if their lifetime P&L is positive.

#### B2. Whale Selection Overhaul: ROI + Consistency

**What:** Replace `orderBy=PNL` with a composite ranking.

**Ranking formula:**
```python
roi = monthly_pnl / monthly_volume
consistency = profitable_weeks / total_weeks
frequency = min(1.0, trade_count / 30)

composite = (0.4 * roi_percentile + 0.3 * consistency_percentile + 0.3 * frequency_score)
```

- Fetch top 100 from leaderboard, compute composite, take top 20.
- Minimum: 30+ trades/month, positive ROI, >0.4 consistency.
- Filters out one-hit wonders and lucky gamblers.

#### B3. Trailing Stop

**What:** After a position is up 10%, set a trailing stop at 5% below the high-water mark.

- Track high-water mark per position during reconciliation.
- If price drops >5% from HWM after initially being up 10%, close position.
- More sophisticated than flat take-profit; captures upside while limiting downside.

#### B4. Enhanced Periodic Reports

**What:** Extend the existing periodic report with category breakdown and whale tier summary.

```
=== PERFORMANCE REPORT ===
Overall: 45 copies, 22W/23L, P&L: -$3.20
By Category:
  politics: 12 copies, 8W/4L, +$4.20
  sports:   25 copies, 10W/15L, -$8.50  [CAPPED at 15%]
  crypto:    5 copies, 3W/2L, +$0.80
By Whale Tier:
  STAR (2): 12 copies, +$6.30
  NEUTRAL (8): 28 copies, -$1.20
  PRUNED (3): 5 copies, -$8.30
```

---

## Implementation Order

### Phase 1: Category Intelligence (Pillar A)
1. A1: Market category extraction + caching
2. A2: Per-category P&L tracking + reporting
3. A3: Category exposure limits (sports cap at 15%)
4. A4: Market efficiency score adjustment in copy_scorer

### Phase 2: Smarter Learning (Pillar B)
5. B1: Decay-weighted performance
6. B2: Whale selection overhaul (ROI + consistency)
7. B3: Trailing stop
8. B4: Enhanced periodic reports with category + tier breakdown

---

## Expected Outcomes

After implementing both pillars:

- **Sports trades drop from ~60-70% to ~15%** of the portfolio
- **Category P&L tracking reveals** which market types are actually profitable to copy
- **Decay weighting prevents** stale whale ratings from persisting
- **ROI-based selection** filters out lucky gamblers before they enter the pool
- **Trailing stops** capture more upside than flat take-profit
- **System becomes data-driven** at every level: which whales, which categories, which market conditions
