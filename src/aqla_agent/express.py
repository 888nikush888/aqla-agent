"""Whole-message entry templates; unknown text always belongs to Hermes."""
from __future__ import annotations

import re
from contextvars import ContextVar
from decimal import Decimal
from typing import Any

from aqla_hyperliquid_mcp.models import AqlaError, Evidence, Interpretation, SourceEvent

from .source_candidates import candidates

VERSION = "express-fields-v2"
QUALIFIED_MASTER = "1ef5231d0bcb8f05aa2e8379bd7276010804cb0cfc37e03168175a3cfc000235"
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


def template_lines(text):
    lines, offset = [], 0
    for raw in text.splitlines(keepends=True):
        text = raw.strip()
        if text:
            lines.append((offset + len(raw) - len(raw.lstrip()), text))
        offset += len(raw)
    return lines


def template_header(lines):
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
    return first, header


class TemplateDraft:
    """One whole-message draft using the existing original-span candidates."""

    def __init__(self, source, header, spans):
        self.source, self.ref, self.spans = source, source.source_id, spans
        self.proofs: list[Evidence] = []
        self.values: dict[str, Any] = {}
        self.targets: list[Decimal] = []
        self.symbol = header["symbol"] or header["symbol_first"]
        self.side = header["side"] or header["side_last"]
        self.proof("symbol", self.symbol)
        self.proof("side", self.side)

    def proof(self, field, quote):
        self.proofs.append(Evidence(field=field, source_id=self.ref, quote=quote))

    def number(self, field, match, name, origin):
        start, end = match.span(name)
        found = {span.value for span in self.spans if span.kind == 'number' and span.notation == 'value' and (span.start == origin + start) and (span.end == origin + end or (field == 'leverage' and origin + end <= span.end <= origin + match.end()))}
        if len(found) != 1:
            raise ValueError('Numeric original is not uniquely interpreted')
        value = Decimal(found.pop())
        if value <= 0:
            raise ValueError('Nonpositive source value')
        self.proof(field, match[0] if field in ('leverage', 'stop_loss') or field.startswith('take_profits.') else self.source.text[origin + start:origin + end])
        return value

    def entry(self, match, origin):
        if "entry" in self.values:
            return False
        body = match["body"]
        body_origin = origin + match.start("body")
        if prices := RANGE.fullmatch(body):
            ordered = sorted((Decimal(prices[name].replace(",", ".")), name) for name in ("first", "second"))
            self.values.update(entry="range", entry_prices=[self.number(f"entry_prices.{i}", prices, name, body_origin)
                                                           for i, (_, name) in enumerate(ordered)])
        elif order := ORDER.fullmatch(body):
            market = order["form"].lower() in ("market", "рынок", "по рынку", "markt")
            if not market and order["first"] is None:
                return False
            self.values.update(entry="market" if market else "limit", entry_prices=(
                [self.number("entry_prices.0", order, "first", body_origin)] if order["first"] else []))
        else:
            return False
        self.proof("entry", match[0])
        return True

    def target(self, match, origin):
        index = int(match["index"] or match["tp_index"] or match["ziel_index"])
        if index != len(self.targets) + 1 or index > 20:
            return False
        self.targets.append(self.number(f"take_profits.{len(self.targets)}", match, "value", origin))
        return True

    def unique_number(self, field, match, origin):
        if field in self.values:
            return False
        self.values[field] = self.number(field, match, "value", origin)
        return True

    def line(self, origin, line):
        if match := ENTRY.fullmatch(line):
            return self.entry(match, origin)
        if match := TARGET.fullmatch(line):
            return self.target(match, origin)
        if match := STOP.fullmatch(line):
            return self.unique_number("stop_loss", match, origin)
        if match := LEVERAGE.fullmatch(line):
            return self.unique_number("leverage", match, origin)
        return False

    def interpretation(self):
        if "entry" not in self.values or "stop_loss" not in self.values or not self.targets:
            return None
        return Interpretation(kind="new_signal", source_refs=[self.ref], symbol=self.symbol,
                              side="long" if self.side.upper() == "LONG" else "short",
                              take_profits=self.targets, evidence=self.proofs, **self.values)


def compile_template(source: SourceEvent):
    """A draft is never admission. Historical replay may inspect unchanged text."""
    if (source.media != "text" or any((source.edited_at is not None, source.reply_to_id,
            source.thread_id, source.album_id, source.forward_from))):
        return None
    lines = template_lines(source.text)
    selected = template_header(lines)
    if selected is None:
        return None
    first, header = selected
    packet = {"messages": [{"text": source.text, "source_id": source.source_id, "event_id": source.event_id}]}
    try:
        draft = TemplateDraft(source, header, candidates(packet))
        for origin, line in lines[first + 1:]:
            if not draft.line(origin, line):
                return None
        return draft.interpretation()
    except ValueError:
        return None
