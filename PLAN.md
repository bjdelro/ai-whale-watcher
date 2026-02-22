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
- **Important:** Skipped trades (due to category cap) should NOT count as "fresh trades" for the auto-scaling logic. This ensures the system recognizes it's starving for actionable trades and scales up whale count.

#### A5. Category-Aware Whale Discovery

**Problem:** Category caps alone would just reduce trade volume, since the leaderboard is sorted by PNL and skews heavily toward sports bettors. Capping sports at 15% without finding non-sports whales means idle capital.

**Solution:** When selecting whales from the leaderboard, diversify by category:

- After fetching the top 100 leaderboard wallets, sample their recent trades to estimate each wallet's primary category mix.
- Rank wallets by a composite that factors in category diversity:
  ```python
  # Prefer whales who trade non-sports markets
  non_sports_ratio = 1.0 - (sports_trades / total_trades)
  diversity_bonus = non_sports_ratio * 0.3  # up to 30% rank boost
  ```
- Ensure at least 30% of active whales have significant non-sports activity.
- As the auto-scaling logic adds more whales (low activity triggers), it pulls from this diversified pool rather than just the next PNL-ranked (likely sports) wallet.

This way, hitting the sports cap naturally leads to discovering whales in politics, crypto, and niche markets — keeping trade volume healthy.

#### A6. Market Efficiency Score Adjustment

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

### Pillar C: LLM-Powered Intelligence (periodic, not in the hot path)

These run on a timer (every 12-24h or on-demand), NOT in the per-trade execution path. They use an LLM to do things rules can't.

#### C1. Rich Market Tagging

**Problem:** The Gamma API `category` field is coarse. "Will Trump announce crypto executive order by March?" is "politics" but has strong crypto/insider overlap. Rules can't parse this nuance from a title string.

**What:** When a new market is first seen, call an LLM (Haiku for cost) with the market title + description to produce structured tags:

```python
# Input: market title + description
# Output:
{
    "category": "politics",         # primary
    "subcategories": ["crypto", "regulatory"],
    "insider_likelihood": "high",   # none/low/medium/high
    "reasoning": "Executive orders are known to insiders before announcement",
    "efficiency_estimate": "low",   # how well-priced is this market likely to be
}
```

- Cache results permanently (market titles don't change).
- Feed `insider_likelihood` and `efficiency_estimate` into the copy score as bonus/penalty.
- Batch new markets and tag them in one call to reduce API costs.

#### C2. Correlated Position Detection

**Problem:** Rules can't tell that "Will Biden drop out?" and "Will Kamala be the nominee?" are correlated. Holding both is doubling down on the same thesis.

**What:** Every 6h, feed the LLM all open position market titles and ask it to identify clusters of correlated bets.

```
Open positions:
1. "Will Trump win 2024?" - YES @ 0.55
2. "Will Republicans win popular vote?" - YES @ 0.40
3. "Bitcoin above $100k by June?" - YES @ 0.30
4. "Will Fed cut rates in March?" - YES @ 0.65

Response: Positions 1 and 2 are highly correlated (both depend on Trump/GOP performance).
Recommend: reduce combined exposure or close the weaker conviction position.
```

- If correlated cluster exposure exceeds a threshold, flag it in Slack and/or auto-reduce the lowest-conviction position.

#### C3. Periodic Strategy Review

**What:** Every 24h, feed the LLM the full performance report and ask for actionable adjustments.

**Input:** Per-whale P&L, per-category P&L, win rates, recent trade log, current open positions, current config values.

**Example output:**
```
Recommendations:
1. Whale "CryptoKing" has 85% win rate on crypto markets but 15% on sports —
   consider only copying their crypto trades (per-whale category filter).
2. 4 of your 6 open positions resolve within 48h of each other —
   you're exposed to a single news cycle. Spread time horizons.
3. Your effective stop-loss at -15% hasn't triggered in 3 days but
   3 positions are at -12%. Consider tightening to -10% temporarily.
```

- Present recommendations in Slack. Optionally auto-apply "safe" suggestions (like per-whale category filters) with a confirmation step.
- This is the "advisor" model — the LLM sees patterns across dimensions that individual rules miss.

**Cost estimate:** Haiku at ~$0.25/M input tokens. A daily strategy review with full context is ~2-5k tokens input. Monthly cost: <$1. Market tagging is even cheaper (batch of 10 markets ~500 tokens). This is negligible relative to trading capital.

---

## Implementation Order

### Phase 1: Category Intelligence (Pillar A)
1. A1: Market category extraction + caching
2. A2: Per-category P&L tracking + reporting
3. A3: Category exposure limits (sports cap at 15%) + fix auto-scaling to ignore capped skips
4. A5: Category-aware whale discovery (diversify whale pool away from sports)
5. A6: Market efficiency score adjustment in copy_scorer

### Phase 2: LLM Intelligence (Pillar C)
6. C1: Rich market tagging via LLM (insider likelihood, subcategories)
7. C2: Correlated position detection
8. C3: Periodic strategy review with actionable recommendations

### Phase 3: Smarter Learning (Pillar B)
9. B1: Decay-weighted performance
10. B2: Whale selection overhaul (ROI + consistency)
11. B3: Trailing stop
12. B4: Enhanced periodic reports with category + tier breakdown

---

## Expected Outcomes

After implementing both pillars:

- **Sports trades drop from ~60-70% to ~15%** of the portfolio
- **Trade volume stays healthy** because category-aware whale discovery fills the pool with non-sports whales
- **Category P&L tracking reveals** which market types are actually profitable to copy
- **Decay weighting prevents** stale whale ratings from persisting
- **ROI-based selection** filters out lucky gamblers before they enter the pool
- **Trailing stops** capture more upside than flat take-profit
- **LLM market tagging** surfaces insider-likely markets that coarse API categories miss
- **Correlated position detection** prevents doubling down on the same thesis
- **Daily strategy review** catches cross-dimensional patterns that individual rules miss
- **System becomes data-driven** at every level: which whales, which categories, which market conditions
