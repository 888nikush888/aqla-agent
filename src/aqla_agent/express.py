"""Whole-message entry templates; unknown text always belongs to Hermes."""
from __future__ import annotations

import re
from contextvars import ContextVar
from decimal import Decimal

from aqla_hyperliquid_mcp.models import AqlaError, Evidence, Interpretation, SourceEvent

from .decisions import candidates

VERSION = "express-fields-v2"
QUALIFIED_MASTER = "eec4d5ef625ee5f592441f17db84fbcedcb3827d2c38438a21bc10375437d10f"
CLAIM = ContextVar("aqla_express_claim", default=None)
ACTIVE = {"prepared", "interpreted", "applying", "waiting_preview", "recovering"}
NUMBER = r"[0-9]+(?:[.,][0-9]+)?"
FLAGS = re.I
HEADINGS = frozenset({"#FUTURES", "FUTURES", "FUTURES SIGNAL", "FUTURES SIGNALS", "PERPETUAL FUTURES"})
HEADER = re.compile(r"(?:➡\ufe0f?\s*)?(?:(?P<side>LONG|SHORT)\s+(?P<symbol>[A-Z0-9][A-Z0-9._/:-]{0,99})|"
                    r"(?P<symbol_first>[A-Z0-9][A-Z0-9._/:-]{0,99})\s+(?P<side_last>LONG|SHORT))", FLAGS | re.ASCII)
ENTRY = re.compile(r"(?:❇\ufe0f?\s*)?(?:Entry|Вход|Einstieg)\s*:\s*(?P<body>.+)", FLAGS)
RANGE = re.compile(rf"(?P<first>{NUMBER})\s*[-–]\s*(?P<second>{NUMBER})")
ORDER = re.compile(rf"(?P<form>market|рынок|по рынку|markt|limit|лимит)\s*(?P<first>{NUMBER})?", FLAGS)
TARGET = re.compile(rf"(?:☑\ufe0f?\s*)?(?:Target\s+(?P<index>[0-9]+)|TP\s*(?P<tp_index>[0-9]+)|Ziel\s+(?P<ziel_index>[0-9]+))\s*:\s*(?P<value>{NUMBER})", FLAGS)
STOP = re.compile(rf"(?:⛔\ufe0f?\s*)?(?:Stoploss|Stop-loss|Stop loss|SL|Стоп|Stop)\s*:\s*(?P<value>{NUMBER})", FLAGS)
LEVERAGE = re.compile(rf"(?:💫\ufe0f?\s*)?(?:Leverage|Hebel|Плечо)\s*:\s*(?P<value>{NUMBER})\s*[xх×]", FLAGS)


def active_claim(snapshot, event_id):
    row = snapshot.get("events", {}).get(event_id, {})
    identity = row.get("express_job_id")
    job = snapshot.get("source_jobs", {}).get(identity, {})
    return identity if job.get("execution_path") == "express" and job.get("status") in ACTIVE else None


def require_source_access(snapshot, event_id):
    identity = active_claim(snapshot, event_id)
    if identity and (CLAIM.get() or (None, None))[1] != identity:
        raise AqlaError("Source revision is owned by express; use its existing outcome")


def compile_template(source: SourceEvent):
    """A draft is never admission. Historical replay may inspect unchanged text."""
    if (source.media != "text" or any((source.edited_at is not None, source.reply_to_id,
            source.thread_id, source.album_id, source.forward_from))):
        return None
    lines, offset = [], 0
    for raw in source.text.splitlines(keepends=True):
        text = raw.strip()
        if text:
            lines.append((offset + len(raw) - len(raw.lstrip()), text))
        offset += len(raw)
    if not lines:
        return None
    first = 0
    # Only complete leading category labels are metadata; keep original offsets.
    while first < len(lines) and lines[first][1].upper() in HEADINGS:
        first += 1
    if first == len(lines):
        return None
    header = HEADER.fullmatch(lines[first][1])
    if header is None:
        return None
    ref = source.source_id
    packet = {"messages": [{"text": source.text, "source_id": ref, "event_id": source.event_id}]}
    try:
        spans = candidates(packet)
    except (AqlaError, ValueError):
        return None
    proofs, values, targets = [], {}, []

    def proof(field, quote):
        proofs.append(Evidence(field=field, source_id=ref, quote=quote))

    def number(field, match, name, origin):
        start, end = match.span(name)
        found = {span.value for span in spans if span.kind == "number" and span.notation == "value"
                 and span.start == origin + start and (span.end == origin + end or field == "leverage"
                     and origin + end <= span.end <= origin + match.end())}
        if len(found) != 1:
            raise ValueError("Numeric original is not uniquely interpreted")
        value = Decimal(found.pop())
        if value <= 0:
            raise ValueError("Nonpositive source value")
        proof(field, match[0] if field in ("leverage", "stop_loss") or field.startswith("take_profits.")
              else source.text[origin + start:origin + end])
        return value

    symbol = header["symbol"] or header["symbol_first"]
    side = header["side"] or header["side_last"]
    proof("symbol", symbol)
    proof("side", side)
    try:
        for origin, line in lines[first + 1:]:
            if match := ENTRY.fullmatch(line):
                if "entry" in values:
                    return None
                body = match["body"]
                body_origin = origin + match.start("body")
                if prices := RANGE.fullmatch(body):
                    ordered = sorted((Decimal(prices[name].replace(",", ".")), name) for name in ("first", "second"))
                    values.update(entry="range", entry_prices=[number(f"entry_prices.{i}", prices, name, body_origin)
                                                               for i, (_, name) in enumerate(ordered)])
                elif order := ORDER.fullmatch(body):
                    market = order["form"].lower() in ("market", "рынок", "по рынку", "markt")
                    if not market and order["first"] is None:
                        return None
                    values.update(entry="market" if market else "limit", entry_prices=(
                        [number("entry_prices.0", order, "first", body_origin)] if order["first"] else []))
                else:
                    return None
                proof("entry", line)
            elif match := TARGET.fullmatch(line):
                index = int(match["index"] or match["tp_index"] or match["ziel_index"])
                if index != len(targets) + 1 or index > 20:
                    return None
                targets.append(number(f"take_profits.{len(targets)}", match, "value", origin))
            elif match := STOP.fullmatch(line):
                if "stop_loss" in values:
                    return None
                values["stop_loss"] = number("stop_loss", match, "value", origin)
            elif match := LEVERAGE.fullmatch(line):
                if "leverage" in values:
                    return None
                values["leverage"] = number("leverage", match, "value", origin)
            else:
                return None
        if "entry" not in values or "stop_loss" not in values or not targets:
            return None
        return Interpretation(kind="new_signal", source_refs=[ref], symbol=symbol, side=side.lower(),
                              take_profits=targets, evidence=proofs, **values)
    except (AqlaError, ValueError):
        return None
