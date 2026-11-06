"""Tests for src.timeutils.parse_trade_timestamp (regression for #13)."""

from datetime import datetime, timedelta, timezone

import pytest

from src.timeutils import parse_trade_timestamp

EXPECTED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
EPOCH = int(EXPECTED.timestamp())


@pytest.mark.parametrize(
    "value",
    [
        EPOCH,
        float(EPOCH),
        str(EPOCH),
        EPOCH * 1000,
        str(EPOCH * 1000),
        "2026-01-02T03:04:05Z",
        "2026-01-02T03:04:05+00:00",
        "2026-01-02T03:04:05.000Z",
        "2026-01-01T22:04:05-05:00",
        "2026-01-02T03:04:05",  # naive -> assumed UTC
        "2026-01-02 03:04:05",
    ],
)
def test_parses_to_aware_utc(value):
    result = parse_trade_timestamp(value)
    assert result.tzinfo is not None
    assert result.utcoffset() == timedelta(0)
    assert result == EXPECTED


def test_result_can_be_subtracted_from_aware_now():
    """The original bug: aware now() minus a naive datetime raised TypeError."""
    age = datetime.now(timezone.utc) - parse_trade_timestamp("2026-01-02T03:04:05Z")
    assert isinstance(age, timedelta)


def test_unparseable_raises():
    with pytest.raises(ValueError):
        parse_trade_timestamp("not a timestamp")
