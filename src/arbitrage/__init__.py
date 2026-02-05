"""
Arbitrage detection module for Polymarket

Strategies:
1. Intra-Market Arbitrage: Buy all outcomes in multi-outcome markets when sum < $1
2. Cross-Platform Arbitrage: Exploit price differences between Polymarket and Kalshi
3. High-Probability Bonds: Buy near-certain outcomes for small but safe returns
"""

from .intra_market import IntraMarketArbitrage, ArbitrageOpportunity

__all__ = ["IntraMarketArbitrage", "ArbitrageOpportunity"]
