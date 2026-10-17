"""Source percentages: new own-margin ROE and preserved legacy source semantics."""
import re
from decimal import Decimal

from aqla_hyperliquid_mcp.models import AqlaError

SOURCE_TP_PREFIX = r"(?:\btps?(?=\b|\d)|take.?profits?|targets?|ziele?|тейк|цел[ьи])(?:\s*\d+\s*[:=])?\s*[:=;\-]?\s*"
SOURCE_TP_PERCENT = re.compile(SOURCE_TP_PREFIX + r"(?P<value>\d+(?:[.,]\d+)?)\s*%", re.I)
PERCENT_VALUE = r"\d+(?:[.,]\d+)?\s*%"
INTERVAL_LINK = r"(?:[-–—]|\bto\b|\bbis\b|\bдо\b)"
PERCENT_INTERVAL_BODY = r"(?P<lower>\d+(?:[.,]\d+)?)\s*%?\s*" + INTERVAL_LINK + r"\s*(?P<upper>\d+(?:[.,]\d+)?)\s*%"
PERCENT_INTERVAL = re.compile(r"(?<![\w.,])" + PERCENT_INTERVAL_BODY, re.I)
SOURCE_TP_PERCENT_INTERVAL = re.compile(SOURCE_TP_PREFIX + PERCENT_INTERVAL_BODY, re.I)


def percentage_intervals(text):
    """Whole two-bound intervals, never a pair cropped from a longer TP list."""
    for match in PERCENT_INTERVAL.finditer(text):
        if (re.search(r"\d+(?:[.,]\d+)?\s*%?\s*" + INTERVAL_LINK + r"\s*$", text[:match.start()], re.I)
                or re.match(r"\s*" + INTERVAL_LINK + r"\s*\d", text[match.end():], re.I)):
            continue
        yield match


def binding_percentage_target_interval(text):
    intervals = {(m.span("lower"), m.span("upper")) for m in percentage_intervals(text)}
    if any((m.span("lower"), m.span("upper")) in intervals for m in SOURCE_TP_PERCENT_INTERVAL.finditer(text)):
        return True
    from .semantic_contract import SENTENCES
    for sentence in SENTENCES.split(text):
        for match in percentage_intervals(sentence):
            scope = sentence[max(0, match.start() - 60):match.end() + 60]
            if (re.search(r"\b(?:only|max(?:imum)?|at most|no more|nur|maximal|höchstens|nicht mehr|только|не больше|не более)\b", scope, re.I)
                    and not re.search(r"\b(?:risk\w*|rm|stop\w*|allocation\w*|margin\w*|equity|deposit\w*|capital|size|risiko\w*|kapital\w*|марж\w*|деп\w*|риск\w*|стоп\w*)\b", scope, re.I)):
                return True
    return False


def explicit_percentage_targets(text):
    """Recognize only unqualified percent-only target lines, never allocations."""
    from aqla_hyperliquid_mcp.rules import quoted_numbers
    values = []
    for line in text.splitlines():
        match = SOURCE_TP_PERCENT.search(line)
        if not match or re.search(r"\w", line[:match.start()]):
            continue
        if line[:match.start("value")].rstrip().endswith(("-", "−")):
            continue
        body = line[match.start("value"):].strip()
        # Mixed price/portion explanations remain with semantic review.
        if not re.fullmatch(PERCENT_VALUE + r"(?:\s*[/,;|]\s*" + PERCENT_VALUE + r")*[.!]?", body):
            continue
        for number in re.finditer(PERCENT_VALUE, body):
            alternatives = set(quoted_numbers(number.group()))
            if len(alternatives) != 1:
                return []
            values.append(alternatives.pop())
    return values if values == sorted(set(values)) and all(value > 0 for value in values) else []


def reference_entry(signal, policy):
    if signal.entry not in ("limit", "range") or not signal.entry_prices:
        return None
    low, high = signal.entry_prices[0], signal.entry_prices[-1]
    if policy.entry_range_choice == "midpoint":
        return (low + high) / 2
    long = signal.side == "long"
    return (high if long else low) if policy.entry_range_choice == "near" else (low if long else high)


def prices(reference, side, market, unit, values, leverage=None):
    from aqla_hyperliquid_mcp.rules import price_grid
    reference = Decimal(str(reference))
    if unit not in ("course_percent", "roi_percent", "roe_percent"):
        raise AqlaError("Unknown source percentage interpretation")
    if unit in ("roi_percent", "roe_percent") and (leverage is None or Decimal(str(leverage)) < 1):
        if unit == "roe_percent":
            raise AqlaError("ROE targets need the actual effective own initial margin leverage")
        raise AqlaError("ROI targets need the evidenced original signal leverage; profile/Backup leverage cannot replace it")
    percentages = [Decimal(str(value)) / (Decimal(str(leverage)) if unit in ("roi_percent", "roe_percent") else 1) for value in values]
    if percentages != sorted(set(percentages)) or any(value <= 0 or not value.is_finite() for value in percentages):
        raise AqlaError("Source percentage targets must be finite, positive, unique and ascending")
    long = side == "long"
    raw = [reference * (1 + (1 if long else -1) * percentage / 100) for percentage in percentages]
    targets = [price_grid(value, market, up=long) for value in raw if value > 0]
    if (len(targets) != len(values) or targets != sorted(set(targets), reverse=not long)
            or any(value <= 0 or (value <= reference if long else value >= reference) for value in targets)):
        raise AqlaError("Source percentage prices collapse or have invalid direction on the exchange tick grid")
    return targets
