"""Conservative original-text constraints for the single-action public schema.

These checks are vetoes, never authorizations or substitutes for semantic model
review. They inspect only the selected original revision, not old reply plans.
"""
from __future__ import annotations

import re
from decimal import Decimal

from aqla_hyperliquid_mcp.models import require_first
from aqla_hyperliquid_mcp.rules import quoted_numbers


def pattern(expression):
    return re.compile(expression, re.I)


WORD = r"(?<!\w)"
END = r"(?!\w)"
MARKET = pattern(WORD + r"(?:market|markt|рынок|маркет(?:у|ом)?|по\s+рынку)" + END)
LIMIT = pattern(WORD + r"(?:limit|лимит(?:н(?:ый|ая|ые|ым|ую|ого|ой|ому|ом|ыми|ых)|к(?:а|и|ой|у|ами|ах)|ом)?)" + END)
CURRENT_PRICE_ENTRY = pattern(WORD + r"(?:buy(?:ing)?|sell(?:ing)?|покупа(?:ем|ю)|прода(?:ем|ю)|kauf(?:e|en)|verkauf(?:e|en))\s+"
    r"(?:(?:at|по|zum?)\s+)?(?:current\s+prices?|текущ\w*\s+цен\w*|aktuell\w*\s+(?:preis|kurs)\w*)" + END)
PENDING_ENTRY_ORDER = pattern(WORD + r"(?:(?:pending|deferred|resting)\s+(?:(?:buy|sell|limit)\s+)?orders?|"
    r"отложенн\w*\s+(?:ордер\w*|заявк\w*)|ausstehend\w*\s+(?:kauf|verkaufs?)?order\w*)" + END)
ENTRY = pattern(WORD + r"(?:entry|enter|buy|sell|einstieg|einsteigen|вход|входим|войти)" + END)
PORTION = pattern(WORD + r"(?:half|quarter|part|portion|hälfte|halbe\w*|viertel|teil|половин\w*|четверть|часть|частичн\w*|[1-3]\s*/\s*[24]|\d+\s*%)" + END)
PAST = pattern(WORD + r"(?:yesterday|previously|already|earlier|gestern|bereits|zuvor|вчера|ранее|уже)" + END)
NEGATED = pattern(WORD + r"(?:do\s+not|don't|never|not|no|nicht|kein\w*|не|нельзя)\s+(?:\w+\s+){0,2}$")
PAST_PREFIX = pattern(WORD + r"(?:did|had|have|has|was|were|habe|haben|hatte|hat|wurde|был\w*)\s+(?:\w+\s+){0,3}$")
PAST_VERB = (r"(?:перестав(?:ил[аио]?|лял[аио]?|лен[аоы]?)|обнов(?:ил[аио]?|лял[аио]?|лен[аоы]?)|"
             r"замен(?:ил[аио]?|ял[аио]?|ен[аоы]?)|отмен(?:ил[аио]?|ял[аио]?|ен[аоы]?)|"
             r"сократ(?:ил[аио]?|или)|закрывал[аио]?|закрыл[аио]?|перенес(?:ла|ли|ло)?|"
             r"перевел[аио]?|подтянул[аио]?|поставил[аио]?|убрал[аио]?|выш(?:ел|ла|ли|ло)|зафиксировал[аио]?|"
             r"moved|changed|updated|replaced|cancell?ed|reduced|closed|"
             r"setzte[n]?|verschob(?:en)?|zog(?:en)?|[äa]nderte[n]?|aenderte[n]?|stornierte[n]?|storniert|reduzierte[n]?|reduziert)")
PAST_MATCH = pattern(r"^" + PAST_VERB + END)
PAST_GOVERNOR = pattern(WORD + PAST_VERB + r"\s+(?:\w+\s+){0,3}$")
CLAUSES = pattern(r"\n+|[;!?]+|(?<!\d)\.(?!\d)|\b(?:and|und|и|затем)\b|\s*[,+]\s*")
# Integer prices can end a sentence; a decimal point still precedes a digit.
SENTENCES = pattern(r"\n+|[;!?]+|\.(?!\d)")
CONJUNCTIONS = pattern(r"\b(?:and|und|и|затем|but|aber|но)\b|\s*[,+]\s*")
CURRENT = pattern(WORD + r"(?:now|jetzt|сейчас|теперь)" + END)
HYPOTHETICAL = pattern(WORD + r"(?:example(?:\s+only)?|hypothetical\w*|for\s+example|"
    r"beispiel\w*|hypothetisch\w*|например|пример\w*|гипотет\w*|предположим)" + END)
PAST_WORD = pattern(WORD + PAST_VERB + END)
FUTURE = pattern(WORD + r"(?:tomorrow|tonight|later|next\s+\w+|in\s+\d+\s*(?:min\w*|hours?|days?)|"
                 r"morgen|später|spaeter|nach\s+\d+\s*\w+|завтра|позже|потом|через\s+\d+\s*\w+)" + END)
CONDITION = pattern(WORD + r"(?:if|when|once|after|until|falls|wenn|sobald|nachdem|nach\s+(?:tp\s*\d|ziel\w*|erreich\w*)|"
                    r"если|когда|после|при\s+(?:достижении|касании|пробое|tp\s*\d|тейк\w*|цел\w*))" + END)
CONDITIONAL_EXIT_PERMISSION = pattern(WORD + r"(?:"
    r"кто\s+(?:(?:еще|пока)\s+)?не\s+(?:фикс(?:ил|ировал)\w*|закры(?:л|вал)\w*)[^.!?\n]{0,30}"
    r"(?:можно|можете|может)\s+(?:сделать\s+это|это\s+сделать)\s+(?:прямо\s+)?сейчас|"
    r"(?:who(?:ever)?|anyone\s+who|if\s+you)\s+(?:(?:has|have|hasn't|haven't)\s+)?(?:not\s+)?(?:yet\s+)?"
    r"(?:taken\s+profits?|closed)(?:\s+yet)?[^.!?\n]{0,30}(?:can|may)\s+do\s+(?:it|so)\s+now|"
    r"wer\s+noch\s+nicht[^.!?\n]{0,30}(?:mitgenommen|geschlossen)\s+hat[^.!?\n]{0,20}"
    r"kann\s+(?:das\s+)?jetzt\s+(?:tun|machen))" + END)
STOP_CONDITION = pattern(WORD + r"(?:stop(?:[-\s]+loss)?|sl|стоп(?:[-\s]*лосс)?)\s*[:=-]?\s*"
                         r"(?:\w+\s+){0,4}(?:h\d+|\d+\s*[hm]|hourly|daily|candle\w*|свеч\w*|часов\w*|kerz\w*)" + END)
EXTRA_ENTRY = pattern(WORD + r"(?:bids?|averag(?:e|ing)(?!\s+entry\s+price\b)|additional\s+(?:limit|entries|entry)|"
                      r"second\s+(?:entry|order|leg|tranche)|втор\w*\s+(?:набор|вход|добор|ордер)|"
                      r"zweit\w*\s+(?:einstieg|order|tranche)|"
                      r"усреднен\w*|докуп\w*|nachkauf\w*|zusätzlich\w*\s+(?:limit|einstieg))" + END)
TP_LABEL = pattern(WORD + r"(?:tp\s*\d|target\s*\d|тейк\w*|цел\w*|ziel\w*)" + END)
NEW_ENTRY = pattern(WORD + r"(?:entry|einstieg|вход)\s*[:=@]|"
                    + WORD + r"(?:enter|buy|sell|einsteigen|входим|войти|целюсь\s+в\s+(?:лонг|шорт)|"
                    r"aim(?:ing)?\s+(?:for\s+)?(?:a\s+)?(?:long|short)|peil(?:e|en)\s+(?:einen?\s+)?(?:long|short))" + END)
SETUP_HEADING = pattern(r"(?:^|\n)\s*[#$]?[a-z0-9]+\s+(?:long|short|лонг|шорт)" + END)
DISCRETE_ENTRIES = pattern(WORD + r"(?:limit\s+(?:orders|entries|bids)|(?:orders|entries|bids)\s+at|лимитк(?:и|ами)|лимитны\w*\s+(?:заявк\w*|ордер\w*)|einstiegsorders|limitorders)" + END)
ENTRY_ZONE = pattern(WORD + r"(?:range|zone|диапазон|зона|bereich)" + END)
STRICT_ENTRY_PRICE = pattern(WORD + r"(?:strictly|only|exclusively)\s+(?:\w+\s+){0,4}(?:specified|stated|given)\s+price\b|"
    + WORD + r"(?:строго|только|исключительно)\s+(?:по\s+)?(?:указанной|обозначенной|заданной)\s+цене\b|"
    + WORD + r"(?:nur|ausschließlich|ausschliesslich)\s+(?:zum?\s+)?(?:angegebenen|genannten)\s+preis\b")
OPPORTUNITY = pattern(WORD + r"(?:opportunit\w*|chance\w*|gelegenheit\w*|m[öo]glich\w*|moeglich\w*|возможност\w*|шанс\w*)" + END)
OPENING_OPPORTUNITY = pattern(WORD + r"(?:enter\w*|entry|open\w*|buy\w*|sell\w*|long|short|"
    r"einstieg\w*|einsteigen|er[öo]ffn\w*|eroeffn\w*|открыти\w*|вход\w*|лонг\w*|шорт\w*)" + END)
FUTURE_OPPORTUNITY = pattern(WORD + r"(?:will|would|could|might|may|expected|awaited|"
    r"wird|w[üu]rde|würden|koennte|k[öo]nnte|erwartet\w*|"
    r"предоставит|даст|создаст|обеспечит|может|ожидаем\w*)" + END)
ENTRY_LABEL = pattern(WORD + r"(?:entry|einstieg|вход)\s*[:=@]")
CURRENT_ENTRY = pattern(r"(?:^|[:,]\s*)\s*(?:(?:now|jetzt|сейчас)\s+)?"
    r"(?:(?:i|we)\s+(?:am\s+|are\s+)?(?:enter(?:ing)?|buy(?:ing)?|sell(?:ing)?|open(?:ing)?)|"
    r"(?:enter|buy|sell|einsteigen|kaufe[n]?|verkaufe[n]?|входим|вхожу|войти|покупаем|покупаю|открываем|открываю))" + END)
FINANCIAL_FIELD = pattern(WORD + r"(?:entry|stop(?:[-\s]+loss)?|sl|tp\s*\d*|targets?|take[-\s]+profit|"
    r"einstieg|stopploss|ziel\w*|вход|стоп(?:[-\s]*лосс)?|цел[ьи]|тейк\w*)\s*[:=@-]?")
TRUNCATED_EXTRA_ENTRY = pattern(WORD + r"(?:(?:another|additional|second|also)\s+(?:\w+\s+){0,4}"
    r"(?:entry|bid|limit|buy|entry\s+point)\s*(?:at|in|on|:)|"
    r"(?:weitere\w*|zus[äa]tzlich\w*|auch|noch)\s+(?:\w+\s+){0,4}"
    r"(?:einstieg\w*|limit\w*|nachkauf\w*)\s*(?:bei|zu|in|:)|"
    r"(?:так\s*же|также|еще|дополнительно|дополнительн\w*|втор\w*)\s+(?:\w+\s+){0,4}"
    r"(?:точк\w*|вход\w*|лимит\w*|покупк\w*|добор\w*)\s*(?:в|на|по|у|при|:))\s*$")


def strict_deferred_price(text, entry_timing, entry_prices):
    return entry_timing == "deferred" and len(entry_prices) == 1 and bool(STRICT_ENTRY_PRICE.search(text))


def one_sided_live_entry_limit(packet, span, side):
    """One directional price boundary is a limit, never an invented second bound."""
    if span is None or span.source_id != packet["anchor_source_id"] or span.kind != "number" or "%" in span.quote:
        return False
    text = require_first(row["text"] for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
    return one_sided_entry_limit(text, span.start, span.end, side)


def one_sided_entry_limit(text, start, end, side):
    prefix = text[text.rfind("\n", 0, start) + 1:start]
    if not (ENTRY.search(prefix) or ENTRY_ZONE.search(prefix)):
        return False
    suffix = text[end:]
    return bool(side == "short" and (re.match(r"\s*\+(?!\d)", suffix) or re.search(r"(?:>=|≥)\s*$", prefix))
                or side == "long" and (re.match(r"\s*[-−](?!\s*\d)", suffix) or re.search(r"(?:<=|≤)\s*$", prefix)))


def source_price_boundary_failure(text, interpretation):
    if interpretation.entry == "limit" or len(interpretation.entry_prices) != 1:
        return None
    from .source_candidates import NUMBERS
    for match in NUMBERS.finditer(text):
        if (set(quoted_numbers(match.group())) == {interpretation.entry_prices[0]}
                and one_sided_entry_limit(text, match.start(), match.end(), interpretation.side)):
            return "source_price_boundary_requires_limit"
    return None


def source_leverage_interval_failure(text, interpretation):
    if interpretation.kind != "new_signal":
        return None
    atom = r"(?:[xх×]\s*)?\d+(?:[.,]\d+)?(?:\s*[xх×])?"
    interval = re.compile(r"(?<![\w.])" + atom + r"\s*[-–—]\s*" + atom + r"(?!\w)", re.I)
    for line in text.splitlines():
        for match in interval.finditer(line):
            labelled = bool(re.search(r"\b(?:leverage|hebel|плечо)\s*[:=@]?\s*$", line[:match.start()], re.I))
            symbol_in_line = bool(interpretation.symbol and interpretation.symbol.casefold() in line.casefold()
                                  or any(proof.field == "symbol" and proof.quote.casefold() in line.casefold()
                                         for proof in interpretation.evidence))
            header = bool(symbol_in_line
                          and re.search(r"[xх×]", match.group(), re.I))
            if labelled or header:
                return "source_leverage_interval_requires_review"
    return None


def unbound_setup_update_failure(packet, interpretation):
    if interpretation.kind != "trade_update" or packet.get("anchor_setup_bound"):
        return None
    text = require_first(row["text"] for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
    live = current_structure_text(text)
    if (NEW_ENTRY.search(live) or SETUP_HEADING.search(live)) and not current_actions(text, include_tp_footers=False):
        return "unbound_setup_edit_is_not_management"
    return None


def update_identity_failure(packet, interpretation):
    if (interpretation.kind != "trade_update" or packet.get("anchor_setup_bound")
            or interpretation.bound_trade_id is not None):
        return None
    from .source_candidates import linked_candidate_refs
    linked = linked_candidate_refs(packet)
    if not interpretation.symbol or not any(proof.field == "symbol" and proof.source_id in linked
                                            for proof in interpretation.evidence):
        return "update_identity_missing"
    anchor = require_first(row for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
    if (re.search(WORD + re.escape(interpretation.symbol) + END, anchor["text"], re.I)
            and not any(proof.field == "symbol" and proof.source_id == packet["anchor_source_id"]
                        for proof in interpretation.evidence)):
        return "update_identity_needs_current_quote"
    return None


def bounded_live_entry_range(packet, spans):
    """One original bounded price phrase, never two unrelated numeric mentions."""
    if len(spans) != 2 or any(span is None or span.source_id != packet["anchor_source_id"]
            or span.kind != "number" or "%" in span.quote for span in spans):
        return False
    first, second = sorted(spans, key=lambda span: span.start)
    if first.end > second.start or Decimal(first.value) == Decimal(second.value):
        return False
    text = require_first(row["text"] for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
    return (not entry_structure_failure(text) and bool(re.fullmatch(
        r"\s*(?:[-–—]|to|bis|до)\s*", text[first.end:second.start], re.I)))
VERB = r"(?:move|set|put|bring|raise|lower|trail|adjust|переносим|перенести|переводим|перевести|перестав\w*|двигаем|подтягиваем|ставим|ставлю|ставь|setz\w*|zieh\w*|verschieb\w*)"
STOP = r"(?:the\s+)?(?:stopp?(?:[-\s]+loss)?|стоп\w*)"
BREAK_EVEN = r"(?:break[-\s]?even|breakeven|entry|einstand|безубыт\w*|б[./\s]?у|вход\w*)"
STOP_ACTION = pattern(WORD + VERB + r"\s+(?:\w+\s+){0,3}" + STOP + r"|" + WORD + STOP + r"\s+(?:now\s+|jetzt\s+)?(?:to|auf|в|на)\s+")
BE_DESTINATION = pattern(BREAK_EVEN)
POSTFIX_PAST_STOP = pattern(r"^\s*(?:" + BREAK_EVEN + r"|\d+(?:[.,]\d+)?)\s+"
    r"(?:(?:already|bereits|уже)\s+)?" + PAST_VERB + END)
TARGET_ACTION = pattern(WORD + r"(?:replace|change|move|set|adjust|обнов\w*|замен\w*|переносим|перестав\w*|änder\w*|aender\w*|verschieb\w*)\s+(?:\w+\s+){0,3}(?:targets?|take[-\s]?profits?|tp|тейк\w*|цел\w*|ziel\w*)" + END)
CANCEL_ACTION = pattern(WORD + r"(?:cancel\w*|remove|отмен\w*|stornier\w*)\s+(?:\w+\s+){0,3}(?:entry|order|orders|entries|вход\w*|заяв\w*|ордер\w*|einstieg)" + END)
EXIT_ACTION = pattern(WORD + r"(?:reduce|reducing|trim|cut|close|closing|exit|exiting|take\s+(?:half|a\s+quarter)\s+off|"
                      r"сократ\w*|закрыва\w*|закрыть|закрой\w*|закроем|закрою|выхожу|выходим|выйти|фиксируем|фиксирую|зафиксиру\w*|"
                      r"фиксим|фиксан(?:у|ем|уть)|фиксн(?:у|ем)|часть\s+фикс(?:ировать|ить)?|фикс\s+часть|"
                      r"reduzier\w*|schließ\w*|schliess\w*|schließe\w*|schliesse\w*)" + END)


def current_actions(text, *, include_tp_footers=True):
    """Recognize explicit current actions with their original clause boundaries."""
    text = matching_words(text)
    actions = []
    normalized = text.casefold().replace("ё", "е")
    live = current_structure_text(text)
    initial_setup = bool(SETUP_HEADING.search(normalized) or NEW_ENTRY.search(live) or CURRENT_ENTRY.search(live))
    if not include_tp_footers:
        normalized = ";".join(sentence for sentence in re.split(r"[;!?\n]|(?<!\d)\.(?!\d)", normalized)
                              if not tp_break_even_footer(sentence))
    for sentence in SENTENCES.split(normalized):
        blocked = False
        for clause in CONJUNCTIONS.split(sentence):
            actions.extend(clause_actions(clause.strip(), blocked, initial_setup=initial_setup))
            markers = [(match.start(), True) for matcher in (PAST, HYPOTHETICAL, PAST_WORD) for match in matcher.finditer(clause)]
            markers += [(match.start(), False) for match in CURRENT.finditer(clause)]
            if markers:
                blocked = max(markers)[1]
    return actions


def clause_actions(clause, inherited_blocked, *, initial_setup=False):
    actions = []
    if not clause:
        return actions
    matches = [(match, "stop") for match in STOP_ACTION.finditer(clause)]
    matches += [(match, "replace_targets") for match in TARGET_ACTION.finditer(clause)]
    matches += [(match, "cancel_entry") for match in CANCEL_ACTION.finditer(clause)]
    matches += [(match, "exit") for match in EXIT_ACTION.finditer(clause)]
    for match, action in matches:
        before = clause[:match.start()]
        history_prefix = CURRENT.split(before)[-1]
        markers = [(point.start(), True) for matcher in (PAST, HYPOTHETICAL, PAST_WORD)
                   for point in matcher.finditer(clause[:match.end()])]
        markers += [(point.start(), False) for point in CURRENT.finditer(clause[:match.end()])]
        blocked = max(markers)[1] if markers else inherited_blocked
        # A governor scopes coordinated clauses until an explicit current marker
        # or hard sentence boundary establishes a new instruction.
        if (blocked or NEGATED.search(before) or PAST_PREFIX.search(history_prefix)
                or PAST_MATCH.search(match.group()) or PAST_GOVERNOR.search(history_prefix)):
            continue
        if action == "stop":
            if POSTFIX_PAST_STOP.search(clause[match.end():]):
                continue
            # A bare numeric stop field in a full setup is initial protection;
            # explicit move verbs and stop-to-entry instructions remain actions.
            if (initial_setup and match.start() == 0 and not re.match(WORD + VERB, match.group(), re.I)
                    and re.fullmatch(r"\s*\d+(?:[.,]\d+)*\s*", clause[match.end():])):
                continue
            action = "break_even" if BE_DESTINATION.search(clause[match.end():]) else "move_stop"
        elif action == "exit":
            if "фикс" in match.group() and PORTION.search(match.group()) and PAST.search(clause[match.end():]):
                continue
            quantities = [quantity for quantity in PORTION.finditer(clause) if not (
                "%" in quantity.group() and re.search(r"(?:[-−+]\s*|(?:at|на|в)\s*)$", clause[:quantity.start()]))]
            action = "reduce" if quantities or match.group().startswith(("reduce", "reducing", "trim", "cut", "сократ", "reduzier")) else "close"
        actions.append((action, clause))
    return actions


def matching_words(text):
    """Resolve lookalikes only inside Cyrillic/mixed words, never originals or prices."""
    lookalikes = str.maketrans("ACEHKMOPTXYaceopxy", "АСЕНКМОРТХУасеорху")
    def word(match):
        value = match.group()
        latin = len(re.findall(r"[a-z]", value, re.I))
        cyrillic = len(re.findall(r"[а-яё]", value, re.I))
        return value.translate(lookalikes) if latin and cyrillic >= latin else value
    return re.sub(r"[A-Za-zА-Яа-яЁё]+", word, text)


def analysis_only_entry_evidence(original, quote, value):
    """A real number still needs an entry role; reconstruct its original context."""
    from aqla_hyperliquid_mcp.rules import SOURCE_NUMBERS, number_is_ordinal
    technical = pattern(r"\bhistoric(?:al)?\b|all[- ]time|previous\s+(?:high|low|peak|trough)|historisch\w*|"
        r"историческ\w*|(?:прошл\w*|предыдущ\w*)\s+(?:минимум\w*|максимум\w*)|"
        r"\b(?:support|resistance|poi|fvg)\b|order[- ]?block|ключев\w*\s+зон\w*|ликвидност\w*|поддержк\w*|сопротивлен\w*")
    assignment = pattern(r"\b(?:entry|enter\w*|buy\w*|bought|sell\w*|sold|einstieg\w*|kauf\w*|verkauf\w*|"
        r"вход\w*|набор\w*|покупа\w*|прода\w*|открыва\w*|беру)\b|"
        r"current\s+(?:market\s+)?price|текущ\w*\s+цен\w*|aktuell\w*\s+(?:preis|kurs)")
    boundaries = list(SENTENCES.finditer(original))
    contexts = []
    for occurrence in re.finditer(re.escape(quote), original):
        for number in SOURCE_NUMBERS.finditer(quote):
            start, end = occurrence.start() + number.start(), occurrence.start() + number.end()
            if value not in set(quoted_numbers(number.group())) or number_is_ordinal(original, start, end):
                continue
            left = max((m.end() for m in boundaries if m.end() <= start), default=0)
            right = min((m.start() for m in boundaries if m.start() >= end), default=len(original))
            sentence = matching_words(original[left:right])
            contexts.append(bool(technical.search(sentence) and not assignment.search(sentence)))
    return bool(contexts) and all(contexts)


def managed_position_side_failure(text, interpretation):
    if interpretation.kind != "trade_update" or interpretation.update_action not in ("close", "reduce"):
        return None
    directions = set()
    for action, clause in current_actions(matching_words(text)):
        if action not in ("close", "reduce"):
            continue
        closing = r"(?:clos(?:e|ing)|exit(?:ing)?|reduc\w*|закрыва\w*|закрыть|сокраща\w*)"
        for side, position in (("long", r"(?:purchase|buy\s+position|покупк\w*)"),
                               ("short", r"(?:sale\s+position|sell\s+position|продаж\w*)")):
            if (re.search(WORD + position + END + r".{0,100}" + WORD + closing + END, clause, re.I)
                    or re.search(WORD + closing + END + r"\s+(?:(?:the|my|our)\s+)?" + position + END, clause, re.I)):
                directions.add(side)
    if len(directions) > 1:
        return "multiple_managed_position_directions_require_review"
    if directions and interpretation.side not in directions:
        return "managed_position_direction_missing_or_changed"
    return None


def current_structure_text(text):
    """Past/examples cannot become current entry constraints after conjunctions."""
    text = text.casefold().replace("ё", "е")
    live = []
    for sentence in SENTENCES.split(text):
        for index, piece in enumerate(CURRENT.split(sentence)):
            if not (PAST.search(piece) or HYPOTHETICAL.search(piece) or PAST_WORD.search(piece)):
                live.append(piece if index == 0 else "now " + piece)
    return ";".join(live)


def future_entry_opportunity_failure(text):
    """A forecasted qualitative opening opportunity is not a current entry order."""
    # A sentence may end immediately after a price ("Entry: 100. Forecast").
    # Keep decimal points intact while separating that independent instruction.
    sentences = re.split(r";|\.(?!\d)(?=\s|$)", current_structure_text(text))
    opportunities = [sentence for sentence in sentences if OPPORTUNITY.search(sentence)
        and OPENING_OPPORTUNITY.search(sentence) and (FUTURE_OPPORTUNITY.search(sentence)
            or FUTURE.search(sentence) or CONDITION.search(sentence))]
    if not opportunities:
        return None
    for sentence in sentences:
        # An independent current instruction or a concrete deferred price-bound
        # order remains eligible. An opportunity's price/target is not an order.
        if sentence in opportunities:
            continue
        if CURRENT_ENTRY.search(sentence):
            return None
        if re.search(r"\d", sentence) and (ENTRY_LABEL.search(sentence) or STRICT_ENTRY_PRICE.search(sentence)
                or LIMIT.search(sentence) and ENTRY.search(sentence)):
            return None
        if CURRENT.search(sentence) and (ENTRY.search(sentence) or SETUP_HEADING.search(sentence)) and MARKET.search(sentence):
            return None
    return "future_entry_opportunity_requires_review"


def incomplete_financial_instruction_failure(text):
    """Only truncated original financial fields/extra entries veto an action."""
    live = current_structure_text(text)
    if TRUNCATED_EXTRA_ENTRY.search(live):
        return "source_financial_instruction_incomplete"
    for field in FINANCIAL_FIELD.finditer(live):
        tail = live[field.end():].split(";", 1)[0]
        # Numeric percent clarification directly attached to a financial field,
        # with an unclosed original parenthesis. Ordinary prose parentheses and
        # fully closed price-plus-percent notation are unaffected.
        if re.search(r"^\s*[-+~]?\d+(?:[.,]\d+)*(?:\s*[-–]\s*\d+(?:[.,]\d+)*)?\s*"
                     r"\(\s*[-+~]?\d+(?:[.,]\d+)*\s*%[^)]*$", tail):
            return "source_financial_instruction_incomplete"
    return None


def tp_break_even_footer(sentence):
    """One conditional TP-triggered BE rule, in either original word order."""
    return (bool(CONDITION.search(sentence) and TP_LABEL.search(sentence))
            and [action for action, _ in current_actions(sentence)] == ["break_even"])


def entry_structure_failure(text):
    text = current_structure_text(matching_words(text))
    if STOP_CONDITION.search(text):
        return "conditional_stop_not_representable"
    # A legitimate zone has one form and two bounds. Separate MARKET and
    # LIMIT entry legs cannot be silently merged into that zone or one order.
    forms = set()
    for clause_group in SENTENCES.split(text):
        if not PAST.search(clause_group) and (ENTRY.search(clause_group) or PORTION.search(clause_group)
                or CURRENT_PRICE_ENTRY.search(clause_group) or PENDING_ENTRY_ORDER.search(clause_group)
                or re.search(r"(?:market|рынок|маркет\w*|limit|лимит\w*)\s*[:@=-]?\s*\d", clause_group)):
            for kind, matcher in (("market", MARKET), ("limit", LIMIT), ("market", CURRENT_PRICE_ENTRY), ("limit", PENDING_ENTRY_ORDER)):
                for match in matcher.finditer(clause_group):
                    before, after = clause_group[:match.start()], clause_group[match.end():]
                    if NEGATED.search(before):
                        continue
                    if kind == "limit" and (re.search(r"\b(?:risk|risiko|риск\w*)\s*$", before)
                            or re.match(r"\s*(?:(?:of|for|für|des)\s+)?(?:risk|risiko|риск\w*)\b", after)):
                        continue
                    forms.add(kind)
    if len(forms) > 1:
        return "entry_legs_not_representable"
    for clause in SENTENCES.split(text):
        if any(not NEGATED.search(clause[:match.start()]) for match in DISCRETE_ENTRIES.finditer(clause)) and not ENTRY_ZONE.search(clause) and len(
                re.findall(r"(?<![\w.])\d+(?:[.,]\d+)*(?!\w)", clause)) > 1:
            return "entry_legs_not_representable"
    if (ENTRY.search(text) or MARKET.search(text)) and any(
            not NEGATED.search(text[:match.start()]) and re.search(r"\d", text[max(0, match.start() - 35):match.end() + 80])
            for match in EXTRA_ENTRY.finditer(text)):
        return "additional_entry_legs_not_representable"
    return None


def split_entry_failure(text, interpretation):
    """A two-leg schema does not authorize omitted legs or independent setups."""
    legs = interpretation.entry_legs
    if len(legs) != 2 or re.search(r"\b(?:third|dritt\w*|трет\w*|(?:entry|einstieg|вход)\s*[#:]?\s*3)\b", text, re.I):
        return "entry_legs_not_representable"
    pairs = {re.sub(r"(?:USDT|USDC)$", "", value) for value in re.findall(r"\b[A-Z0-9]{2,}(?:USDT|USDC)\b", text)}
    normalized_text = "\n".join(SENTENCES.split(matching_words(text)))
    headings = {match.group().strip().split()[0].lstrip("#$").upper() for match in SETUP_HEADING.finditer(normalized_text)}
    headings.update(match.group(1).upper() for match in re.finditer(r"(?im)^\s*(?:long|short|лонг|шорт)\s+[$#]?([a-z0-9]+)\b", normalized_text))
    headings = {re.sub(r"(?:USDT|USDC)$", "", value) for value in headings}
    directions = {value.casefold() for value in re.findall(r"\b(?:long|short)\b", matching_words(text), re.I)}
    if len(pairs) > 1 or len(headings) > 1 or len(directions) > 1:
        return "entry_legs_not_representable"
    scopes = [part for part in SENTENCES.split(current_structure_text(text)) if re.search(r"\d|half|половин|hälfte", part, re.I)]
    if sum(len(MARKET.findall(part)) + len(LIMIT.findall(part)) for part in scopes) > 2:
        return "entry_legs_not_representable"
    spans = []
    for index, leg in enumerate(legs):
        proofs = [proof for proof in interpretation.evidence if proof.field == f"entry_legs.{index}.kind"]
        contexts = [proof.quote for proof in proofs if proof.quote in text and text.count(proof.quote) == 1]
        valid = []
        for quote in contexts:
            normalized = matching_words(quote)
            if HYPOTHETICAL.search(normalized):
                continue
            if any(NEGATED.search(normalized[:match.start()]) for matcher in (MARKET, LIMIT) for match in matcher.finditer(normalized)):
                continue
            if leg.kind == "market":
                form = bool(MARKET.search(normalized)) and not LIMIT.search(normalized)
            elif leg.kind == "unspecified":
                form = bool(CURRENT_PRICE_ENTRY.search(normalized)) and not MARKET.search(normalized) and not LIMIT.search(normalized)
            else:
                form = bool(LIMIT.search(normalized) or PENDING_ENTRY_ORDER.search(normalized)
                    or ENTRY.search(normalized) and leg.price is not None)
                form = form and not MARKET.search(normalized)
            if form and (leg.price is None or leg.price in set(quoted_numbers(quote))):
                from .evidence import position_fractions
                portions = {value for _, value in position_fractions(quote)}
                portions.update(Decimal(match.group(1).replace(",", ".")) for match in re.finditer(r"(\d+(?:[.,]\d+)?)\s*%", quote))
                if leg.weight_percent is not None and not portions:
                    return "split_entry_weights_missing_or_changed"
                if portions and portions != {leg.weight_percent or Decimal(50)}:
                    return "split_entry_weights_missing_or_changed"
                valid.append((text.index(quote), text.index(quote) + len(quote)))
        if not valid:
            return "split_entry_evidence_needs_role_review"
        spans.append(valid[0])
    if max(spans[0][0], spans[1][0]) < min(spans[0][1], spans[1][1]):
        return "split_entry_evidence_needs_role_review"
    return None


def management_structure_failure(text, *, ignore_tp_footers=False, allow_sequence=False):
    for sentence in SENTENCES.split(matching_words(text).casefold().replace("ё", "е")):
        permission = CONDITIONAL_EXIT_PERMISSION.search(sentence)
        if permission and not any(matcher.search(sentence[:permission.start()]) for matcher in (PAST, PAST_WORD, HYPOTHETICAL)):
            return "scheduled_management_not_representable"
    actions = current_actions(text)
    if any(STOP_CONDITION.search(clause) and action in ("move_stop", "break_even") for action, clause in actions):
        return "conditional_stop_not_representable"
    immediate_actions = current_actions(text, include_tp_footers=False)
    if immediate_actions or ignore_tp_footers:
        actions = immediate_actions
    sequence = [action for action, _ in actions]
    if len(actions) > 1 and not (allow_sequence and len(sequence) == 2 and sequence[0] == "reduce" and sequence[1] in ("move_stop", "break_even")):
        return "management_actions_not_representable"
    # One future update is still unsupported: Interpretation has no update
    # scheduler/trigger. Deferred ENTRY remains supported by its explicit field.
    if any(current_actions(sentence) and (FUTURE.search(sentence) or CONDITION.search(sentence))
           and not ((immediate_actions or ignore_tp_footers) and tp_break_even_footer(sentence))
           for sentence in re.split(r"[;!?\n]|(?<!\d)\.(?!\d)", text)):
        return "scheduled_management_not_representable"
    return None


def unsupported_structure(packet):
    """Original-only veto available when a model fails before assembly.

    It does not classify or grant execution. A definite current entry marker
    is required for entry/stop structural checks, avoiding old status context.
    """
    text = require_first(row["text"] for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
    failure = incomplete_financial_instruction_failure(text) or future_entry_opportunity_failure(text)
    if failure:
        return failure
    if (ENTRY.search(text) or re.search(r"\b(?:long|short|лонг|шорт)\b", text, re.I)
            or CURRENT_PRICE_ENTRY.search(matching_words(text)) or PENDING_ENTRY_ORDER.search(matching_words(text))):
        failure = entry_structure_failure(text)
        if failure:
            return failure
    return management_structure_failure(text, ignore_tp_footers=bool(NEW_ENTRY.search(text) or SETUP_HEADING.search(text)), allow_sequence=True)


def source_percentage_target_failure(text, interpretation):
    from .percent_targets import binding_percentage_target_interval, explicit_percentage_targets
    actionable = (interpretation.kind == "new_signal" and interpretation.symbol and interpretation.side
        or interpretation.kind == "trade_update" and interpretation.update_action == "replace_targets")
    if not actionable or HYPOTHETICAL.search(text) or PAST.search(text) or PAST_WORD.search(text):
        return None
    if binding_percentage_target_interval(text):
        return "source_percentage_target_interval_not_representable"
    values = explicit_percentage_targets(text)
    if values and (interpretation.take_profit_unit != "roe_percent" or interpretation.take_profits != values):
        return "source_percentage_targets_missing_or_changed"
    return None


def supported_action_failure(text, interpretation):
    """Return a stable rejection code for explicitly unrepresentable instructions."""
    if (interpretation.kind == "trade_update" and TP_LABEL.search(text)
            and re.search(r"(?:забыл\w*|забыли|forgot(?:ten)?)[^.!?\n]{0,45}(?:ордер\w*|order)|"
                          r"(?:order|auftrag)[^.!?\n]{0,25}vergessen", text, re.I)
            and not current_actions(text)):
        return "existing_target_catchup_requires_report_review"
    direction_failure = managed_position_side_failure(text, interpretation)
    if direction_failure:
        return direction_failure
    if (interpretation.kind == "trade_update" and interpretation.update_action in (None, "move_stop", "break_even")
            and not current_actions(text)
            and any(POSTFIX_PAST_STOP.search(clause[match.end():])
                for clause in SENTENCES.split(text.casefold().replace("ё", "е")) for match in STOP_ACTION.finditer(clause))):
        return "completed_stop_move_is_not_current_management"
    if (interpretation.kind in ("report", "unrelated")
            and re.fullmatch(r"(?:#\w+[\s,]*)+", text.strip())
            and re.search(r"(?:#|_)(?:scenarios?|setups?|plans?|szenari\w*|сценари\w*|сетап\w*|план\w*)(?:_|$)", text, re.I)):
        return "source_setup_topic_needs_original_context"
    scope = re.search(r"(?:everything\s+above\s+(?:is|was)\s+(?:an?\s+)?example|"
        r"alles\s+obige\s+(?:ist|war)\s+(?:ein\s+)?beispiel|"
        r"вс[её]\s*,?\s*что\s+выше[^\n.!?]{0,100}\bпример\w*)", text, re.I)
    if scope and interpretation.kind in ("new_signal", "trade_update"):
        tail = text[scope.end():]
        if not NEW_ENTRY.search(tail) and not SETUP_HEADING.search(tail) and not current_actions(tail):
            return "source_example_requires_report_review"
    if interpretation.kind in ("report", "unrelated"):
        if interpretation.kind == "report":
            # Original document framing scopes examples and chapter headings, never live orders.
            chapters = list(re.finditer(r"(?m)^\s*\d{1,2}:\d{2}(?::\d{2})?\s*[-–—]\s*[^\n]+", text))
            if len(chapters) >= 3 and re.search(r"\b(?:video|chapters?|webinar|kapitel|видео|разбираем|обсуждаем)\b", text, re.I):
                text = re.sub(r"(?m)^\s*\d{1,2}:\d{2}(?::\d{2})?\s*[-–—]\s*[^\n]+", "", text)
            if scope:
                text = text[scope.end():]
            if PAST_WORD.search(text):
                text = ";".join(sentence for sentence in SENTENCES.split(text)
                    if not (CONDITION.search(sentence) and OPPORTUNITY.search(sentence) and not CURRENT.search(sentence)))
        if (ENTRY.search(text) or re.search(r"\b(?:long|short|лонг|шорт)\b", text, re.I)
            or CURRENT_PRICE_ENTRY.search(matching_words(text)) or PENDING_ENTRY_ORDER.search(matching_words(text))):
            failure = entry_structure_failure(text)
            if failure:
                return failure
        failure = management_structure_failure(text, ignore_tp_footers=bool(NEW_ENTRY.search(text) or SETUP_HEADING.search(text)))
        if failure:
            return failure
        # Bare exit/close words also describe market analysis (e.g. a candle close).
        if any(action in ("break_even", "move_stop") for action, _ in current_actions(text, include_tp_footers=False)):
            return "classification_omits_management_instruction"
        return None
    if interpretation.kind not in ("new_signal", "trade_update"):
        return None
    failure = incomplete_financial_instruction_failure(text)
    if failure:
        return failure
    failure = source_percentage_target_failure(text, interpretation)
    if failure:
        return failure
    if interpretation.kind == "new_signal":
        if (re.search(r"\b(?:entry\s+\d+\s*[✅✔]|all\s+entries\s+(?:achieved|filled|executed))", text, re.I)
                and re.search(r"\baverage\s+entry\s+price\s*[:=@]", text, re.I)
                and not any(CURRENT_ENTRY.search(sentence.strip()) for sentence in SENTENCES.split(current_structure_text(text)))):
            return "entry_execution_report_not_new_signal"
        failure = future_entry_opportunity_failure(text)
        if failure:
            return failure
    if interpretation.update_action in ("move_stop", "break_even") and STOP_CONDITION.search(current_structure_text(text)):
        return "conditional_stop_not_representable"
    if interpretation.kind == "new_signal":
        failure = split_entry_failure(text, interpretation) if interpretation.entry == "split" else entry_structure_failure(text)
        if failure:
            return failure
        failure = source_leverage_interval_failure(text, interpretation)
        if failure:
            return failure
        failure = source_price_boundary_failure(text, interpretation)
        if failure:
            return failure
        if current_actions(text, include_tp_footers=False):
            return "signal_contains_management_instruction"
        return None
    failure = management_structure_failure(text, allow_sequence=interpretation.update_action == "sequence")
    if failure:
        return failure
    actions = current_actions(text, include_tp_footers=False) or current_actions(text)
    if interpretation.update_action == "sequence":
        if [step.action for step in interpretation.management_steps] != [action for action, _ in actions]:
            return "management_action_mismatch"
        from aqla_hyperliquid_mcp.rules import management_steps
        for child in management_steps(interpretation):
            if not any([action for action, _ in current_actions(proof.quote, include_tp_footers=False)] == [child.update_action]
                       for proof in child.evidence if proof.field == "update_action"):
                return "management_action_mismatch"
        return None
    if actions and interpretation.update_action not in {action for action, _ in actions}:
        return "management_action_mismatch"
    return None
