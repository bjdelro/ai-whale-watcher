"""
Execution engine for copy trading.
Supports paper trading (dry run) and live execution with risk controls.
"""

from .paper_trader import PaperTrader
from .risk_manager import RiskManager

__all__ = ["PaperTrader", "RiskManager"]
