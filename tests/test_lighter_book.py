import copy
from decimal import Decimal

import pytest

from aqla_agent.entry_watch import parse_book
from aqla_agent.lighter_book import LighterBook
from aqla_hyperliquid_mcp.models import AqlaError, Market, now_ms


def snapshot():
    market = Market(symbol="ETH", asset_id=0, product="perp", mid=100, size_decimals=2, max_leverage=20)
    frame = {
        "channel": "order_book:0",
        "type": "subscribed/order_book",
        "timestamp": now_ms(),
        "order_book": {
            "nonce": 10,
            "bids": [{"price": "99", "size": "2"}, {"price": "98", "size": "3"}],
            "asks": [{"price": "101", "size": "2"}, {"price": "103", "size": "3"}],
        },
    }
    return market, frame


def delta(frame, bids, asks, *, begin=10, nonce=11):
    return {
        **frame,
        "type": "update/order_book",
        "timestamp": now_ms(),
        "order_book": {"begin_nonce": begin, "nonce": nonce, "bids": bids, "asks": asks},
    }


def test_one_sided_and_deep_updates_preserve_the_real_best_prices():
    market, frame = snapshot()
    book = LighterBook(market)
    parse_book("lighter", market, frame, lighter_book=book)
    value = parse_book(
        "lighter", market, delta(frame, [], [{"price": "103", "size": "8"}]), lighter_book=book
    )
    assert value.bid == 99 and value.ask == 101 and value.asks[-1] == (Decimal(103), Decimal(8))
    value = book.apply(delta(frame, [{"price": "98", "size": "4"}], [], begin=11, nonce=12))
    assert value.bid == 99 and value.ask == 101
    value = book.apply(delta(frame, [{"price": "99", "size": "0"}], [], begin=12, nonce=13))
    assert value.bid == 98 and value.ask == 101


@pytest.mark.parametrize(
    "change", ["gap", "replay", "missing_nonce", "negative_size", "nan", "crossed", "stale", "no_snapshot"]
)
def test_bad_continuity_or_levels_invalidate_the_book_until_a_new_snapshot(change):
    market, frame = snapshot()
    book = LighterBook(market)
    if change != "no_snapshot":
        book.apply(frame)
    event = delta(frame, [{"price": "98", "size": "4"}], [])
    if change == "gap":
        event["order_book"]["begin_nonce"] = 9
    if change == "replay":
        event["order_book"].update(begin_nonce=9, nonce=10)
    if change == "missing_nonce":
        event["order_book"].pop("nonce")
    if change == "negative_size":
        event["order_book"]["bids"][0]["size"] = "-1"
    if change == "nan":
        event["order_book"]["bids"][0]["price"] = "NaN"
    if change == "crossed":
        event["order_book"]["bids"][0]["price"] = "102"
    if change == "stale":
        event["timestamp"] -= 2001
    with pytest.raises(AqlaError):
        book.apply(event)
    assert book.value is None and book.nonce is None
    assert book.apply(copy.deepcopy(frame)).bid == 99


def test_a_delta_cannot_be_used_as_a_standalone_book():
    market, frame = snapshot()
    with pytest.raises(AqlaError):
        parse_book("lighter", market, delta(frame, [], [{"price": "101", "size": "1"}]))
