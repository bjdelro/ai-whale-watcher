# Whale Copy Trader: Improvements Plan

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

## Implementation Order & Status

### Phase 0: Modularization (Pillar E) — COMPLETE ✅
All 7 modules extracted from the monolith (**2850 → 1019 lines**, -64%):
- **E1:** `src/whales/whale_manager.py` — leaderboard, scaling, per-whale P&L, pruning, conviction sizing
- **E2:** `src/positions/position_manager.py` — position lifecycle, persistence, reconciliation, live orders (~1100 lines)
- **E3:** `src/market_data/client.py` — price fetching, market details, resolution detection
- **E4:** `src/evaluation/trade_evaluator.py` — trade evaluation, sell detection, conflict checks (~310 lines)
- **E6:** `src/signals/cluster_detector.py` — hedge analysis, cluster trading detection
- **E7:** `src/reporting/reporter.py` — Slack alerts, periodic/final reports (~370 lines)
- **E8:** `src/signals/arb_trader.py` — unusual activity detection, arbitrage scanning (~510 lines)

The orchestrator now contains only init/start/run/stop/CLI and thin delegation wrappers.

### Phase 1: WebSocket Migration + API Budget (Pillar D) — COMPLETE ✅
1. **D1:** RTDS WebSocket feed for real-time whale trade detection (`src/ingestion/rtds_feed.py`). Shadow mode runs WS alongside REST. Client-side proxyWallet filtering. `6d4bea8`
2. **D2:** Tiered cache TTLs (5min default, 60s resolution), cohort-based staggered resolution checks (hash % 4), batch price extraction via `extract_token_prices()`. `4eaf541`

### Phase 2: Category Intelligence (Pillar A) — COMPLETE ✅
3. **A1:** Market category extraction via Gamma API `/markets` endpoint. `CATEGORY_MAP` normalizes raw categories to canonical buckets. Permanent cache per condition_id. `category` field on CopiedPosition. `a4b3dfc`
4. **A2:** Per-category copy P&L tracking (`_category_copy_pnl`), per-whale category mix (`_whale_category_mix`). `record_category_pnl()` called alongside all 5 `record_copy_pnl()` sites. Category breakdown in periodic reports. `a4b3dfc`
5. **A3:** Category exposure limits (`MAX_CATEGORY_EXPOSURE_PCT`: sports 15%, politics 35%, crypto 30%). Category-capped trades return `"category_capped"` sentinel; orchestrator pops fresh_trade_timestamps so auto-scaling stays accurate. `a4b3dfc`
6. **A5:** Category-aware whale deprioritization. `rebuild_active_set()` moves sports-heavy whales (>80%) out when sports cap is hit. Freed slots filled with non-sports whales. `a4b3dfc`
7. **A6:** Category efficiency scores in `copy_scorer.py` (sports -6, crypto +5, politics +4). `CATEGORY_CONVICTION` multiplier in trade_evaluator and position sizing. `a4b3dfc`

### Phase 3: LLM Intelligence (Pillar C) — COMPLETE ✅
8. **C1:** `src/intelligence/market_tagger.py` — Rich market tagging via Haiku. Structured output (subcategories, insider_likelihood, efficiency_estimate). Permanent cache, batch processing. Score adjustment fed into position sizing (±15%). `61e5942`
9. **C2:** `src/intelligence/correlation_detector.py` — Correlated position detection every 6h. Flags clusters >30% exposure in Slack. `61e5942`
10. **C3:** `src/intelligence/strategy_reviewer.py` — Strategy review every 24h. Builds comprehensive prompt with per-whale, per-category P&L. 3-5 prioritized recommendations posted to Slack. `61e5942`

All LLM features use raw HTTP to Anthropic API (no SDK dependency). Graceful no-op if no `ANTHROPIC_API_KEY`.

### Phase 4: Smarter Learning (Pillar B) — COMPLETE ✅
11. **B1:** Decay-weighted performance. Timestamped history on copy P&L records. Weights: 24h=3x, 48h=2x, 7d=1x, older=0.5x. Used for pruning, conviction sizing, and auto-un-pruning when recent performance recovers. `6c999f1`
12. **B2:** Composite whale ranking. `_compute_composite_rank()`: 0.5 ROI + 0.3 volume + 0.2 PNL percentile. Fetches top 100, filters <$5k volume. Replaces pure PNL ordering. `6c999f1`
13. **B3:** Trailing stop. `high_water_mark` field on CopiedPosition. After 10% gain, closes if price drops 5% below HWM. `6c999f1`
14. **B4:** Enhanced reports. Category `[CAPPED]` indicator. Whale tier breakdown (STAR/NEUTRAL/PRUNED) with aggregate P&L per tier. `6c999f1`

### Phase 5: On-Chain (Pillar D — optional)
15. D3: On-chain event monitoring (replaces discovery polling entirely) — **NOT STARTED**

---

## Architecture Notes

### Key Files
| File | Lines | Purpose |
|------|-------|---------|
| `run_whale_copy_trader.py` | ~1300 | Orchestrator: init, run loop, CLI, dataclasses |
| `src/positions/position_manager.py` | ~1150 | Position lifecycle, persistence, reconciliation |
| `src/whales/whale_manager.py` | ~560 | Whale discovery, scaling, P&L, pruning |
| `src/evaluation/trade_evaluator.py` | ~380 | Trade evaluation, sell detection |
| `src/reporting/reporter.py` | ~420 | Slack alerts, periodic reports |
| `src/market_data/client.py` | ~350 | Market data, price fetching, category cache |
| `src/signals/arb_trader.py` | ~510 | Unusual activity, arbitrage scanning |
| `src/signals/cluster_detector.py` | ~200 | Hedge analysis, cluster detection |
| `src/intelligence/` | ~580 | LLM client, market tagger, correlation, strategy |
| `src/ingestion/rtds_feed.py` | ~300 | RTDS WebSocket feed |

### Data Flow
```
Leaderboard API → WhaleManager → active whale set
                                      ↓
RTDS WebSocket ──→ whale_trade_queue ──→ TradeEvaluator
REST fallback ───→                        ↓ (filters, caps, hedge analysis)
                                    PositionManager.copy_trade()
                                        ↓
                                    CopiedPosition (open)
                                        ↓
                          check_market_resolutions() / exit checks
                                        ↓
                          CopiedPosition (closed) → record P&L
```

---

## Risks, Gaps & Open Questions

### Resolved
- ✅ D1: RTDS `activity` topic includes `proxyWallet` in trade events
- ✅ A1: Gamma API `/markets` endpoint has `category` directly (not just tags on events)
- ✅ B2: Leaderboard API returns `volume` alongside `pnl` — ROI computable

### Architectural Risks (still apply)
- **Cold start:** Most features need weeks of data. System runs in "learning mode" initially.
- **Small samples:** Min sample sizes enforced before acting on aggregates (e.g., `WHALE_PRUNE_MIN_COPIES = 5`).
- **B3 trailing stop vs prediction markets:** Price can jump to 0.95 instantly on news. Trailing stop may exit before resolution. Consider time-gating (only for positions >24h old).

### Open Questions
- [ ] What's the WebSocket subscription limit per connection? Can we subscribe to 50+ markets?
- [ ] Do proxy wallet addresses from the leaderboard match RTDS trade events consistently?
- [ ] What's the real message throughput on popular markets?
