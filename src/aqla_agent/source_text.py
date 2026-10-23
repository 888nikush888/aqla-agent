"""Shared original-text tokens; no account, provider or exchange effects."""
import re
from decimal import Decimal

SOURCE_NUMBERS = re.compile(r"(?<![\w.])(?:[xх×]\s*)?\d+(?:[.,]\d+)*(?:[\u00a0\u202f]\d{3}(?:[.,]\d+)?)?(?:[xх×])?(?!\w|[.,]\d)", re.I)


def number_is_ordinal(text, start, end):
    if not re.fullmatch(r"\d{1,2}", text[start:end]) or text[end:end + 1] not in (":", ")"):
        return False
    before = text[text.rfind("\n", 0, start) + 1:start].strip()
    return not before or bool(re.search(r"(?<!\w)(?:tp|target|take[-\s]?profit|тейк|цел[ьи]|ziel)\s*$", before, re.I))


def complete_numeric_span(original, start, end, tokens):
    before, after = original[:start], original[end:]
    return not (before[-1:].isdigit() or after[:1].isdigit() or re.search(r"\d[.,]$", before) or re.match(r"[.,]\d", after)
                or any(low <= start and end <= high and (low, high) != (start, end) for low, high in tokens))


def numeric_evidence_values(original, quote):
    """Every original match must support one complete non-ordinal value."""
    occurrences = list(re.finditer(re.escape(quote), original))
    if not occurrences:
        return None
    tokens = [match.span() for match in SOURCE_NUMBERS.finditer(original)]
    compatible = None
    for occurrence in occurrences:
        values = []
        cropped = False
        for number in SOURCE_NUMBERS.finditer(quote):
            start, end = occurrence.start() + number.start(), occurrence.start() + number.end()
            if not complete_numeric_span(original, start, end, tokens):
                cropped = True
                break
            if number_is_ordinal(original, start, end):
                continue
            alternatives = set(quoted_numbers(number.group()))
            if not alternatives:
                return None
            values.append(alternatives)
        if cropped:
            continue  # A substring inside a larger decimal is not an occurrence of this value.
        if len(values) != 1:
            return None
        compatible = values[0] if compatible is None else compatible & values[0]
    return compatible or None


def quoted_numbers(quote):
    # General decimal notation only; no channel-specific syntax or field-role regex.
    normalized = quote.replace("\u00a0", " ").replace("\u202f", " ")
    # A sentence-ending period is punctuation; decimal/thousands dots stay untouched.
    normalized = re.sub(r"(?<=\d)\.(?=$|[\s\"'»”);,!?\]])", " ", normalized)
    normalized = re.sub(r"(?<=\d) (?=\d{3}(?:\b|[.,]))", "", normalized)
    for token in re.findall(r"(?<![\w.])\d+(?:[.,]\d+)*(?![\w.])", normalized):
        if "." in token and "," in token:
            token = token.replace(",", "") if token.rfind(".") > token.rfind(",") else token.replace(".", "").replace(",", ".")
        elif "," in token:
            parts = token.split(",")
            if parts[0] != "0" and all(len(part) == 3 for part in parts[1:]):
                yield Decimal("".join(parts))
            token = token.replace(",", ".")
        elif "." in token and token.split(".")[0] != "0" and all(len(part) == 3 for part in token.split(".")[1:]):
            yield Decimal(token.replace(".", ""))
        try:
            yield Decimal(token)
        except Exception:
            continue


def quoted_leverage_numbers(quote):
    # General multiplier notation, not channel patterns. Keep price evidence parsing unchanged.
    numeric = r"\d+(?:[.,]\d+)*"
    quote = re.sub(rf"(?<![\w.])[xх×]\s*({numeric})(?!\w)", r" \1 ", quote, flags=re.I)
    quote = re.sub(rf"(?<![\w.])({numeric})\s*(?:[xх×]|fach)(?!\w)", r" \1 ", quote, flags=re.I)
    return quoted_numbers(quote)


def pattern(expression):
    return re.compile(expression, re.I)


WORD = r"(?<!\w)"


END = r"(?!\w)"


MARKET = pattern(WORD + r"(?:market|markt|рынок|маркет(?:у|ом)?|по\s+рынку)" + END)


LIMIT = pattern(WORD + r"(?:limit|лимит(?:н(?:ый|ая|ые|ым|ую|ого|ой|ому|ом|ыми|ых)|к(?:а|и|ой|у|ами|ах)|ом)?)" + END)


SENTENCES = pattern(r"\n+|[;!?]+|\.(?!\d)")
