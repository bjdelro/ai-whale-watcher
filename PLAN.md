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

#### A5. Category-Aware Whale Selection + Deprioritization

**Problem:** Category caps alone would just reduce trade volume, since the leaderboard is sorted by PNL and skews heavily toward sports bettors. Capping sports at 15% without finding non-sports whales means idle capital. Worse, sports-only whales waste polling slots and rate limit budget — we fetch their trades every cycle only to skip them all.

**Solution — two parts:**

**Part 1: Stop polling sports-only whales when category cap is hit.**

Once the sports category cap is reached, whales whose recent trades are >80% sports are **deprioritized** — moved out of the active polling list entirely. This frees up rate limit budget for whales who might produce actionable trades.

```python
# Per-whale category tracking (built from A1 + A2 data)
whale_category_mix = {
    "0xabc...": {"sports": 0.90, "politics": 0.05, "crypto": 0.05},
    "0xdef...": {"sports": 0.20, "politics": 0.50, "crypto": 0.30},
}

# When sports cap is hit, deprioritize sports-heavy whales
if category_at_cap("sports"):
    for whale in active_whales:
        if whale_category_mix[whale.address].get("sports", 0) > 0.80:
            deprioritize(whale)  # remove from active polling, add to low-freq backup
```

- Deprioritized whales still get polled at a reduced frequency (every 5th cycle) in case they start trading non-sports markets.
- If the sports cap opens back up (positions close, exposure drops), deprioritized whales are re-promoted automatically.

**Part 2: Fill freed slots with non-sports whales.**

When selecting whales from the leaderboard, diversify by category:

- After fetching the top 100 leaderboard wallets, sample their recent trades to estimate each wallet's primary category mix.
- Rank wallets by a composite that factors in category diversity:
  ```python
  # Prefer whales who trade non-sports markets
  non_sports_ratio = 1.0 - (sports_trades / total_trades)
  diversity_bonus = non_sports_ratio * 0.3  # up to 30% rank boost
  ```
- Ensure at least 30% of active whales have significant non-sports activity.
- As the auto-scaling logic adds more whales (low activity triggers), it pulls from this diversified pool rather than just the next PNL-ranked (likely sports) wallet.

**Net effect:** Hitting the sports cap triggers a chain reaction — sports whales get deprioritized, freed slots get filled with non-sports whales, rate limit budget goes to whales producing actionable trades, and trade volume stays healthy in categories with more edge.

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

### Pillar D: API Budget Reduction + Faster Execution

**Problem:** The system burns ~80 REST API calls/min, mostly on per-whale polling — and the Polymarket data API caps at 60-100 req/min. We're already borderline. Every improvement we add (category lookups, price checks, more whales) makes this worse. We need to fundamentally reduce API calls, not just rearrange them.

**Current API budget breakdown (per cycle @ 15s = 4 cycles/min):**

| Call | Per cycle | Per minute | % of budget |
|------|-----------|------------|-------------|
| `_fetch_whale_trades` (8-20 whales) | 8-20 | 32-80 | **~75%** |
| `_scan_for_unusual_activity` | 1 | 4 | ~5% |
| `_fetch_current_price` (per trade/position) | 1-5 | 4-20 | ~10% |
| `_fetch_market_data` (resolutions, SL/TP) | 2-5 | 8-20 | ~10% |
| **Total** | **~25** | **~80-100** | at/over limit |

The whale polling dominates. The solution is to **eliminate most REST polling by switching to WebSocket for trade detection**, then use the freed budget for smarter things (category lookups, market data).

#### D1. WebSocket-First Architecture (the big win)

**What:** Replace per-whale REST polling with a single persistent RTDS WebSocket connection. This takes whale trade detection from ~80 REST calls/min to **0 REST calls/min** for the primary use case.

**Key constraint:** Polymarket WebSocket doesn't support per-wallet subscriptions. The `activity` topic fires for ALL trades on a subscribed market. So we subscribe to markets our whales frequent and filter client-side.

**Implementation:**

1. **Bootstrap phase (REST, one-time):** On startup, do a single REST poll per whale to learn which markets they're active in. This gives us the initial market watchlist. Cost: 20 requests once.

2. **Steady state (WebSocket, ongoing):**
   ```python
   # Subscribe to activity on markets our whales frequent
   for market_slug in whale_market_watchlist:
       client.subscribe(topic="activity", type="trades", market_slug=market_slug)
   ```

3. **Client-side whale filter (zero API cost):**
   ```python
   async def on_ws_trade(trade_data):
       maker = trade_data.get("maker", "").lower()
       taker = trade_data.get("taker", "").lower()
       if maker in whale_addresses or taker in whale_addresses:
           whale_addr = maker if maker in whale_addresses else taker
           await evaluate_and_copy(whale_addr, trade_data)
   ```

4. **Low-frequency REST fallback (discovery only):**
   - Poll each whale once every **5 minutes** (not 15s) purely to discover new markets they've entered that we aren't subscribed to. 20 whales / 5 min = **4 req/min** (down from 80).
   - When a new market is discovered, subscribe via WebSocket immediately.
   - This also catches whale trades on markets we hadn't seen — but at 5-min latency, which is acceptable for discovery (the WebSocket handles the fast path).

**API budget after D1:**

| Call | Per minute | Change |
|------|------------|--------|
| Whale trade detection (WebSocket) | **0** | was 32-80 |
| Discovery polling (5-min REST fallback) | **4** | was 32-80 |
| Unusual activity scan | 4 | unchanged |
| Price checks + market data | 12-40 | unchanged |
| **Total** | **~20-48** | **was ~80-100** |

This frees up ~50 req/min of budget for category lookups (Pillar A), market data enrichment, and a lower discovery poll interval if needed.

**Trade-offs:**
- We can only detect whale trades in real-time on markets we know about. New markets are discovered at 5-min latency via fallback polling.
- More WS subscriptions = more messages to filter. With 20 whales across ~50 markets, volume is manageable — most messages get discarded by the address check (a fast O(1) set lookup).
- The existing `WebSocketFeed` class in `src/ingestion/websocket_feed.py` already has the connection/reconnection infrastructure — we adapt it for the RTDS `activity` topic.

#### D2. Smarter REST Where REST Remains

For the remaining REST calls that can't be replaced by WebSocket:

- **Batch price checks:** When checking SL/TP on N open positions, the CLOB market endpoint returns token prices alongside market data. Use `_fetch_market_data` (which we already call for resolution checks) and extract the price, instead of making a separate `_fetch_current_price` call. One call per position instead of two.
- **Cache market data aggressively:** Current cache TTL is 60s. For non-resolution data (category, volume, title), extend to 1 hour. Resolution status can stay at 60s.
- **Stagger position checks:** Don't check all open positions every cycle. Check 1/4 of positions per cycle (round-robin), so each position is checked every ~40s. Still fast enough for SL/TP, but spreads the API load.

#### D3. On-Chain Event Monitoring (optional, advanced)

**What:** Monitor Polygon blockchain events for the CTF Exchange contract to catch whale trades at the blockchain level.

- Subscribe to `Transfer` and `OrderFilled` events on the Polymarket CTF Exchange contract.
- Filter by whale addresses appearing in `from`/`to` fields.
- Latency: ~2-5s (block time dependent), but catches ALL trades including those the API might delay.
- Would also replace the discovery polling entirely — every on-chain trade has the wallet address, so we'd catch whales entering new markets in real-time.

This is the most complete solution but also the most complex. Consider it after D1 is proven.

---

## Implementation Order

### Phase 0: Modularization (Pillar E — done)
Extracted 3 self-contained modules from the monolith (~550 lines, 3398 -> 2850):
- **E3:** `src/market_data/client.py` — price fetching, market details, resolution detection
- **E1:** `src/whales/whale_manager.py` — leaderboard, scaling, per-whale P&L, pruning, conviction sizing
- **E6:** `src/signals/cluster_detector.py` — hedge analysis, cluster trading detection

Remaining extractions (E2 PositionManager, E4 TradeEvaluator, E5 TradePoller, E7 Reporter, E8 ArbTrader) deferred — they have deep coupling to orchestrator state and need a shared context object to extract cleanly. Will extract incrementally as each pillar touches them.

### Phase 1: WebSocket Migration + API Budget (Pillar D — unlocks everything else)
1. D1: WebSocket-first architecture (replace per-whale REST polling with RTDS activity subscription + 5-min discovery fallback)
2. D2: Batch price checks, extend cache TTLs, stagger position checks

### Phase 2: Category Intelligence (Pillar A — now feasible with freed API budget)
3. A1: Market category extraction + caching
4. A2: Per-category P&L tracking + reporting
5. A3: Category exposure limits (sports cap at 15%) + fix auto-scaling to ignore capped skips
6. A5: Category-aware whale selection + deprioritization
7. A6: Market efficiency score adjustment in copy_scorer

### Phase 3: LLM Intelligence (Pillar C)
8. C1: Rich market tagging via LLM (insider likelihood, subcategories)
9. C2: Correlated position detection
10. C3: Periodic strategy review with actionable recommendations

### Phase 4: Smarter Learning (Pillar B)
11. B1: Decay-weighted performance
12. B2: Whale selection overhaul (ROI + consistency)
13. B3: Trailing stop
14. B4: Enhanced periodic reports with category + tier breakdown

### Phase 5: On-Chain (Pillar D — optional)
15. D3: On-chain event monitoring (replaces discovery polling entirely)

---

## Expected Outcomes

After implementing all pillars:

- **Sports trades drop from ~60-70% to ~15%** of the portfolio
- **Trade volume stays healthy** because category-aware whale discovery fills the pool with non-sports whales
- **Category P&L tracking reveals** which market types are actually profitable to copy
- **Decay weighting prevents** stale whale ratings from persisting
- **ROI-based selection** filters out lucky gamblers before they enter the pool
- **Trailing stops** capture more upside than flat take-profit
- **LLM market tagging** surfaces insider-likely markets that coarse API categories miss
- **Correlated position detection** prevents doubling down on the same thesis
- **Daily strategy review** catches cross-dimensional patterns that individual rules miss
- **WebSocket migration** drops API usage from ~80-100 to ~20-48 req/min, freeing budget for category intelligence
- **WebSocket latency** of 1-2s (vs 15s polling) dramatically increases trades that pass the slippage gate
- **System becomes data-driven** at every level: which whales, which categories, which market conditions

---

## Risks, Gaps & Open Questions

### Showstoppers to Verify Before Building

**1. D1 (WebSocket) — does RTDS `activity` topic include `proxyWallet` in trade events?**

Research confirms the RTDS trade event DOES include a `proxyWallet` field. However, it does NOT include separate `maker`/`taker` fields — only the single `proxyWallet` of the user involved. This means:
- We can still match against our whale address set (the leaderboard returns `proxyWallet` too — line 229 of `run_whale_copy_trader.py`).
- But we might receive **two separate events** for the same trade (one from maker's perspective, one from taker's), or only one. This needs testing. If the WebSocket only emits one event per fill with only one side's `proxyWallet`, we might miss 50% of whale trades (the ones where our whale is the taker vs maker).
- **Must validate with live WebSocket before committing to D1.** If proxyWallet is unreliable, D1 falls apart and we're back to REST polling + optimizations.

**2. A1 (Category) — Gamma API has `tags`, not a simple `category` field.**

The plan assumes a clean `category` field from the Gamma API. Reality: the Gamma API uses a **tag system** on events (not markets directly). Markets inherit tags from their parent event. Categories exist but are accessed via `tag_id` filtering, and a market can have multiple tags. This means:
- Need to fetch the parent event to get categories, not the market directly. Extra API call, or use the `/events` endpoint.
- Tags might be missing or inconsistent for some markets.
- Need a tag -> category mapping layer.

### Architectural Risks

**3. Cold start problem — most features need weeks of data to work.**

- Whale P&L pruning needs 5+ closed copies per whale. At current trade rates, that's 1-2 weeks.
- Category P&L tracking needs 50+ trades per category for statistical significance.
- Decay weighting is meaningless without enough trades in each time window.
- **Mitigation:** Accept that the system runs in "learning mode" for the first 2-3 weeks. Log everything but don't enforce thresholds until minimums are met. The existing system already does this for whale pruning (waits for `WHALE_PRUNE_MIN_COPIES`).

**4. Small sample sizes — overfitting risk is real.**

With 20 whales, category caps, and slippage gates, each cell in the whale x category matrix might have 1-3 trades. Making decisions on that is noise, not signal. Examples:
- "Whale X is 1W/2L on crypto" — could easily be bad luck, not a real pattern.
- "Politics category is +$2.40 on 12 trades" — insufficient for confidence.
- **Mitigation:** Set minimum sample sizes before acting on any aggregate. Don't demote a whale from a category until 10+ trades. Don't penalize a category until 30+ trades. Be explicit about confidence levels in reports.

**5. Complexity creep — the plan adds ~15 features to a ~3000 line system.**

Each feature interacts with others in non-obvious ways:
- Category caps + whale deprioritization + auto-scaling + WebSocket subscriptions = complex state machine.
- Decay weighting + whale tiers + conviction sizing + category multipliers = complicated position size calculation.
- More code = more bugs = harder to debug when trades go wrong.
- **Mitigation:** Build incrementally. Each phase should be independently valuable and tested before starting the next. Phase 1 (WebSocket) should run in shadow mode alongside existing polling before replacing it.

### Feature-Specific Gaps

**6. B3 (Trailing stop) — prediction markets aren't stocks.**

Prediction market prices can jump from 0.60 to 0.95 instantly on news (e.g., election called, court ruling announced). A trailing stop at 5% below HWM would:
- Trigger on normal price fluctuation (noise) before the real resolution move.
- Miss the big payoff — you'd sell at 0.57 when the market was going to resolve at 1.00.
- **Consider:** Trailing stops might only make sense for positions held >24h where we're capturing drift, not event resolution. For positions in "about to resolve" markets, flat take-profit is better.

**7. A5 (Whale deprioritization) — sports whales might be our best non-sports whales too.**

A whale who's 85% sports / 15% crypto might be a great crypto trader precisely because they're sharp enough to profit in sports. Deprioritizing them means we miss their rare but high-signal crypto trades.
- **Consider:** Instead of deprioritizing entire whales, apply category filters at the trade level — still monitor sports-heavy whales via WebSocket, just only copy their non-sports trades. With WebSocket (zero marginal cost per subscription), this costs us nothing.

**8. C1 (LLM tagging) — hallucination risk in a financial context.**

The LLM might confidently tag a market as "high insider likelihood" when it's not. If this feeds into the copy score, we'd systematically overweight trades based on a wrong tag.
- **Mitigation:** LLM tags should only be a minor score adjustment (+/- 3-5 points), never a gate. Log every tag for manual review. Consider human-in-the-loop for the first month.

**9. B2 (ROI + consistency ranking) — leaderboard API might not expose the data we need.**

The plan assumes we can get `profitable_weeks`, `trade_count`, and `monthly_volume` from the leaderboard. The current API returns `pnl`, `volume`, `proxyWallet`, `userName`. We may need to compute consistency ourselves by polling wallet history — which costs more API calls.

### Questions to Resolve

- [ ] Live test: Does RTDS WebSocket `activity` topic fire for both sides of a trade, or only one?
- [ ] Does Gamma API `/events` endpoint reliably include category tags for all events?
- [ ] What's the WebSocket subscription limit per connection? Can we subscribe to 50+ markets?
- [ ] Do proxy wallet addresses from the leaderboard match proxy wallet addresses in RTDS trade events? (Or do users sometimes have multiple proxies?)
- [ ] What's the real message throughput on popular markets? Could 50 market subscriptions overwhelm us?
