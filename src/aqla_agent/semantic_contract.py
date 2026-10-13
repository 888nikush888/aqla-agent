"""Conservative original-text constraints for the single-action public schema.

These checks are vetoes, never authorizations or substitutes for semantic model
review. They inspect only the selected original revision, not old reply plans.
"""
from __future__ import annotations

import re


def pattern(expression):
    return re.compile(expression, re.I)


WORD = r"(?<!\w)"
END = r"(?!\w)"
MARKET = pattern(WORD + r"(?:market|markt|рынок|маркет(?:у|ом)?|по\s+рынку)" + END)
LIMIT = pattern(WORD + r"(?:limit|лимит(?:ный|ная|ные|ка|кой|ом)?)" + END)
ENTRY = pattern(WORD + r"(?:entry|enter|buy|sell|einstieg|einsteigen|вход|входим|войти)" + END)
PORTION = pattern(WORD + r"(?:half|quarter|hälfte|halbe\w*|viertel|половин\w*|четверть|[1-3]\s*/\s*[24]|\d+\s*%)" + END)
PAST = pattern(WORD + r"(?:yesterday|previously|already|earlier|gestern|bereits|zuvor|вчера|ранее|уже)" + END)
NEGATED = pattern(WORD + r"(?:do\s+not|don't|never|not|nicht|kein\w*|не|нельзя)\s+(?:\w+\s+){0,2}$")
PAST_PREFIX = pattern(WORD + r"(?:did|had|have|has|was|were|habe|haben|hatte|hat|wurde|был\w*)\s+(?:\w+\s+){0,3}$")
CLAUSES = pattern(r"\n+|[;!?]+|(?<!\d)\.(?!\d)|\b(?:and|und|и|затем)\b|\s*[,+]\s*")
FUTURE = pattern(WORD + r"(?:tomorrow|tonight|later|next\s+\w+|in\s+\d+\s*(?:min\w*|hours?|days?)|"
                 r"morgen|später|spaeter|nach\s+\d+\s*\w+|завтра|позже|потом|через\s+\d+\s*\w+)" + END)
CONDITION = pattern(WORD + r"(?:if|when|once|after|until|falls|wenn|sobald|nachdem|если|когда|после|при\s+(?:достижении|касании|пробое))" + END)
STOP_CONDITION = pattern(WORD + r"(?:stop(?:[-\s]+loss)?|sl|стоп(?:[-\s]*лосс)?)\s*[:=-]?\s*"
                         r"(?:\w+\s+){0,4}(?:h\d+|\d+\s*[hm]|hourly|daily|candle\w*|свеч\w*|часов\w*|kerz\w*)" + END)
EXTRA_ENTRY = pattern(WORD + r"(?:bids?|averag(?:e|ing)|additional\s+(?:limit|entries|entry)|"
                      r"усреднен\w*|докуп\w*|nachkauf\w*|zusätzlich\w*\s+(?:limit|einstieg))" + END)
TP_BE_FOOTER = pattern(WORD + r"(?:after|when|once|после|при|nach|wenn|future)\b.{0,50}"
                       r"(?:tp\s*\d|target\s*\d|тейк\w*|цел\w*|ziel\w*).{0,70}"
                       r"(?:break[-\s]?even|breakeven|безубыт\w*|б[./\s]?у|einstand)")
DISCRETE_ENTRIES = pattern(WORD + r"(?:limit\s+(?:orders|entries|bids)|(?:orders|entries|bids)\s+at|лимитк(?:и|ами)|лимитны\w*\s+(?:заявк\w*|ордер\w*)|einstiegsorders|limitorders)" + END)
ENTRY_ZONE = pattern(WORD + r"(?:range|zone|диапазон|зона|bereich)" + END)
STRICT_ENTRY_PRICE = pattern(WORD + r"(?:strictly|only|exclusively)\s+(?:\w+\s+){0,4}(?:specified|stated|given)\s+price\b|"
    + WORD + r"(?:строго|только|исключительно)\s+(?:по\s+)?(?:указанной|обозначенной|заданной)\s+цене\b|"
    + WORD + r"(?:nur|ausschließlich|ausschliesslich)\s+(?:zum?\s+)?(?:angegebenen|genannten)\s+preis\b")


def strict_deferred_price(text, entry_timing, entry_prices):
    return entry_timing == "deferred" and len(entry_prices) == 1 and bool(STRICT_ENTRY_PRICE.search(text))
VERB = r"(?:move|set|put|bring|raise|lower|trail|adjust|переносим|перенести|переводим|перевести|перестав\w*|двигаем|подтягиваем|ставим|ставлю|ставь|setz\w*|zieh\w*|verschieb\w*)"
STOP = r"(?:the\s+)?(?:stop(?:[-\s]+loss)?|стоп\w*)"
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
    if not include_tp_footers:
        normalized = ";".join(sentence for sentence in re.split(r"[;!?\n]|(?<!\d)\.(?!\d)", normalized)
                              if not TP_BE_FOOTER.search(sentence))
    for clause in CLAUSES.split(normalized):
        clause = clause.strip()
        if not clause:
            continue
        matches = [(match, "stop") for match in STOP_ACTION.finditer(clause)]
        matches += [(match, "replace_targets") for match in TARGET_ACTION.finditer(clause)]
        matches += [(match, "cancel_entry") for match in CANCEL_ACTION.finditer(clause)]
        matches += [(match, "exit") for match in EXIT_ACTION.finditer(clause)]
        for match, action in matches:
            before = clause[:match.start()]
            # Past/negated descriptions do not become new actions. A separate
            # current clause is still examined even after a past-result clause.
            if NEGATED.search(before) or PAST_PREFIX.search(before) or PAST.search(clause):
                continue
            if action == "stop":
                action = "break_even" if BE_DESTINATION.search(clause[match.end():]) else "move_stop"
            elif action == "exit":
                quantities = [quantity for quantity in PORTION.finditer(clause) if not (
                    "%" in quantity.group() and re.search(r"(?:[-−+]\s*|(?:at|на|в)\s*)$", clause[:quantity.start()]))]
                action = "reduce" if quantities or match.group().startswith(("reduce", "reducing", "trim", "cut", "сократ", "reduzier")) else "close"
            actions.append((action, clause))
    return actions


def entry_structure_failure(text):
    if STOP_CONDITION.search(text):
        return "conditional_stop_not_representable"
        # A legitimate zone has one form and two bounds. Separate MARKET and
        # LIMIT entry legs cannot be silently merged into that zone or one order.
    forms = set()
    for clause_group in re.split(r"[;!?]|(?<!\d)\.(?!\d)", text.casefold()):
        if not PAST.search(clause_group) and (ENTRY.search(clause_group) or PORTION.search(clause_group)
                or re.search(r"(?:market|рынок|маркет\w*|limit|лимит\w*)\s*[:@=-]?\s*\d", clause_group)):
            if MARKET.search(clause_group):
                forms.add("market")
            if LIMIT.search(clause_group):
                forms.add("limit")
    if len(forms) > 1:
        return "entry_legs_not_representable"
    for clause in re.split(r"[;!?\n]|(?<!\d)\.(?!\d)", text):
        if DISCRETE_ENTRIES.search(clause) and not ENTRY_ZONE.search(clause) and len(
                re.findall(r"(?<![\w.])\d+(?:[.,]\d+)*(?!\w)", clause)) > 1:
            return "entry_legs_not_representable"
    if (ENTRY.search(text) or MARKET.search(text)) and any(
            not NEGATED.search(text[:match.start()]) and re.search(r"\d", text[max(0, match.start() - 35):match.end() + 80])
            for match in EXTRA_ENTRY.finditer(text)):
        return "additional_entry_legs_not_representable"
    return None


def management_structure_failure(text):
    actions = current_actions(text)
    immediate_actions = current_actions(text, include_tp_footers=False)
    if immediate_actions:
        actions = immediate_actions
    if len(actions) > 1:
        return "management_actions_not_representable"
    # One future update is still unsupported: Interpretation has no update
    # scheduler/trigger. Deferred ENTRY remains supported by its explicit field.
    if any(current_actions(sentence) and (FUTURE.search(sentence) or CONDITION.search(sentence))
           and not (immediate_actions and TP_BE_FOOTER.search(sentence))
           for sentence in re.split(r"[;!?\n]|(?<!\d)\.(?!\d)", text)):
        return "scheduled_management_not_representable"
    return None


def unsupported_structure(packet):
    """Original-only veto available when a model fails before assembly.

    It does not classify or grant execution. A definite current entry marker
    is required for entry/stop structural checks, avoiding old status context.
    """
    text = next(row["text"] for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
    if ENTRY.search(text) or re.search(r"\b(?:long|short|лонг|шорт)\b", text, re.I):
        failure = entry_structure_failure(text)
        if failure:
            return failure
    return management_structure_failure(text)


def supported_action_failure(text, interpretation):
    """Return a stable rejection code for explicitly unrepresentable instructions."""
    if interpretation.kind not in ("new_signal", "trade_update"):
        return None
    if interpretation.kind == "new_signal":
        failure = entry_structure_failure(text)
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
