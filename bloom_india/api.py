"""
bloom_india/api.py
==================
Top-level shortcut. Import from here for the cleanest usage.

    from bloom_india.api import get_fundamentals, get_ohlcv, get_universe

    df     = get_fundamentals("2024-01-25")
    prices = get_ohlcv("HDFCBANK", "2023-01-01", "2024-12-31")
    nifty  = get_universe("NIFTY 50", date="2024-01-01")
"""

from bloom_india.data.api import (           # noqa: F401
    get_fundamentals,
    get_fundamentals_history,
    get_ohlcv,
    get_price,
    get_panel,
    get_universe,
    NIFTY50,
    refresh_fundamentals_cache,
    refresh_price_cache,
)
