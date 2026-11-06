"""Timestamp helpers shared by trade evaluation paths."""

from datetime import datetime, timezone
from typing import Union

from dateutil.parser import parse as parse_date


def parse_trade_timestamp(value: Union[int, float, str]) -> datetime:
    """Parse a trade timestamp into an aware UTC datetime.

    Accepts unix seconds or milliseconds (int, float, or digit string) and
    ISO-8601 / dateutil-parseable strings. Strings without a timezone are
    assumed to be UTC. Raises ValueError/OverflowError on unparseable input.
    """
    if isinstance(value, (int, float)) or str(value).isdigit():
        ts = float(value)
        if ts > 1e12:
            ts = ts / 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    parsed = parse_date(str(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
