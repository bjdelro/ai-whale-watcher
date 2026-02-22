"""
Signal detection: cluster trading, hedging analysis, and coordinated activity.
"""

from .cluster_detector import ClusterDetector
from .arb_trader import ArbTrader

__all__ = [
    "ClusterDetector",
    "ArbTrader",
]
