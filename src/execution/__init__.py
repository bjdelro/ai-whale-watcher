"""
Execution engine for copy trading.
Supports paper trading (dry run) and live execution with risk controls.
"""

# Lazy imports to avoid pulling in heavy dependencies (sqlalchemy, database)
# when only LiveTrader is needed (e.g., run_whale_copy_trader.py)
__all__ = ["PaperTrader", "RiskManager"]


def __getattr__(name):
    if name == "PaperTrader":
        from .paper_trader import PaperTrader
        return PaperTrader
    if name == "RiskManager":
        from .risk_manager import RiskManager
        return RiskManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
