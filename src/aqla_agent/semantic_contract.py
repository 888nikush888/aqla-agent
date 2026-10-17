"""Conservative original-text constraints for the single-action public schema.

These checks are vetoes, never authorizations or substitutes for semantic model
review. They inspect only the selected original revision, not old reply plans.
"""
from __future__ import annotations

import re
from decimal import Decimal

from aqla_hyperliquid_mcp.rules import quoted_numbers


def pattern(expression):
    return re.compile(expression, re.I)


WORD = r"(?<!\w)"
END = r"(?!\w)"
MARKET = pattern(WORD + r"(?:market|markt|рынок|маркет(?:у|ом)?|по\s+рынку)" + END)
LIMIT = pattern(WORD + r"(?:limit|лимит(?:н(?:ый|ая|ые|ым|ую|ого|ой|ому|ом|ыми|ых)|к(?:а|и|ой|у|ами|ах)|ом)?)" + END)
ENTRY = pattern(WORD + r"(?:entry|enter|buy|sell|einstieg|einsteigen|вход|входим|войти)" + END)
PORTION = pattern(WORD + r"(?:half|quarter|hälfte|halbe\w*|viertel|половин\w*|четверть|[1-3]\s*/\s*[24]|\d+\s*%)" + END)
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
    text = next(row["text"] for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
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
    from .decisions import NUMBERS
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
    text = next(row["text"] for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
    live = current_structure_text(text)
    if (NEW_ENTRY.search(live) or SETUP_HEADING.search(live)) and not current_actions(text, include_tp_footers=False):
        return "unbound_setup_edit_is_not_management"
    return None


def update_identity_failure(packet, interpretation):
    if (interpretation.kind != "trade_update" or packet.get("anchor_setup_bound")
            or interpretation.bound_trade_id is not None):
        return None
    from .decisions import linked_candidate_refs
    linked = linked_candidate_refs(packet)
    if not interpretation.symbol or not any(proof.field == "symbol" and proof.source_id in linked
                                            for proof in interpretation.evidence):
        return "update_identity_missing"
    return None


def bounded_live_entry_range(packet, spans):
    """One original bounded price phrase, never two unrelated numeric mentions."""
    if len(spans) != 2 or any(span is None or span.source_id != packet["anchor_source_id"]
            or span.kind != "number" or "%" in span.quote for span in spans):
        return False
    first, second = sorted(spans, key=lambda span: span.start)
    if first.end > second.start or Decimal(first.value) == Decimal(second.value):
        return False
    text = next(row["text"] for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
    return (not entry_structure_failure(text) and bool(re.fullmatch(
        r"\s*(?:[-–—]|to|bis|до)\s*", text[first.end:second.start], re.I)))
VERB = r"(?:move|set|put|bring|raise|lower|trail|adjust|переносим|перенести|переводим|перевести|перестав\w*|двигаем|подтягиваем|ставим|ставлю|ставь|setz\w*|zieh\w*|verschieb\w*)"
STOP = r"(?:the\s+)?(?:stopp?(?:[-\s]+loss)?|стоп\w*)"
BREAK_EVEN = r"(?:break[-\s]?even|breakeven|entry|einstand|безубыт\w*|б[./\s]?у|вход\w*)"
STOP_ACTION = pattern(WORD + VERB + r"\s+(?:\w+\s+){0,3}" + STOP + r"|" + WORD + STOP + r"\s+(?:now\s+|jetzt\s+)?(?:to|auf|в|на)\s+")
BE_DESTINATION = pattern(BREAK_EVEN)
TARGET_ACTION = pattern(WORD + r"(?:replace|change|move|set|adjust|обнов\w*|замен\w*|переносим|перестав\w*|änder\w*|aender\w*|verschieb\w*)\s+(?:\w+\s+){0,3}(?:targets?|take[-\s]?profits?|tp|тейк\w*|цел\w*|ziel\w*)" + END)
CANCEL_ACTION = pattern(WORD + r"(?:cancel\w*|remove|отмен\w*|stornier\w*)\s+(?:\w+\s+){0,3}(?:entry|order|orders|entries|вход\w*|заяв\w*|ордер\w*|einstieg)" + END)
EXIT_ACTION = pattern(WORD + r"(?:reduce|reducing|trim|cut|close|closing|exit|exiting|take\s+(?:half|a\s+quarter)\s+off|"
                      r"сократ\w*|закрыва\w*|закрыть|закрой\w*|закроем|закрою|выхожу|выходим|выйти|фиксируем|фиксирую|зафиксиру\w*|"
                      r"reduzier\w*|schließ\w*|schliess\w*|schließe\w*|schliesse\w*)" + END)


def current_actions(text, *, include_tp_footers=True):
    """Recognize explicit current actions with their original clause boundaries."""
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
            # A bare numeric stop field in a full setup is initial protection;
            # explicit move verbs and stop-to-entry instructions remain actions.
            if (initial_setup and match.start() == 0 and not re.match(WORD + VERB, match.group(), re.I)
                    and re.fullmatch(r"\s*\d+(?:[.,]\d+)*\s*", clause[match.end():])):
                continue
            action = "break_even" if BE_DESTINATION.search(clause[match.end():]) else "move_stop"
        elif action == "exit":
            quantities = [quantity for quantity in PORTION.finditer(clause) if not (
                "%" in quantity.group() and re.search(r"(?:[-−+]\s*|(?:at|на|в)\s*)$", clause[:quantity.start()]))]
            action = "reduce" if quantities or match.group().startswith(("reduce", "reducing", "trim", "cut", "сократ", "reduzier")) else "close"
        actions.append((action, clause))
    return actions


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
    text = current_structure_text(text)
    if STOP_CONDITION.search(text):
        return "conditional_stop_not_representable"
    # A legitimate zone has one form and two bounds. Separate MARKET and
    # LIMIT entry legs cannot be silently merged into that zone or one order.
    forms = set()
    for clause_group in SENTENCES.split(text):
        if not PAST.search(clause_group) and (ENTRY.search(clause_group) or PORTION.search(clause_group)
                or re.search(r"(?:market|рынок|маркет\w*|limit|лимит\w*)\s*[:@=-]?\s*\d", clause_group)):
            for kind, matcher in (("market", MARKET), ("limit", LIMIT)):
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


def management_structure_failure(text, *, ignore_tp_footers=False):
    actions = current_actions(text)
    if any(STOP_CONDITION.search(clause) and action in ("move_stop", "break_even") for action, clause in actions):
        return "conditional_stop_not_representable"
    immediate_actions = current_actions(text, include_tp_footers=False)
    if immediate_actions or ignore_tp_footers:
        actions = immediate_actions
    if len(actions) > 1:
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
    text = next(row["text"] for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
    failure = incomplete_financial_instruction_failure(text) or future_entry_opportunity_failure(text)
    if failure:
        return failure
    if ENTRY.search(text) or re.search(r"\b(?:long|short|лонг|шорт)\b", text, re.I):
        failure = entry_structure_failure(text)
        if failure:
            return failure
    return management_structure_failure(text, ignore_tp_footers=bool(NEW_ENTRY.search(text) or SETUP_HEADING.search(text)))


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
        if ENTRY.search(text) or re.search(r"\b(?:long|short|лонг|шорт)\b", text, re.I):
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
        failure = entry_structure_failure(text)
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
    failure = management_structure_failure(text)
    if failure:
        return failure
    actions = current_actions(text, include_tp_footers=False) or current_actions(text)
    if actions and interpretation.update_action not in {action for action, _ in actions}:
        return "management_action_mismatch"
    return None
