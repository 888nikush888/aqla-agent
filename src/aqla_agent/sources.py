"""Owner-supplied labels are independent of Telegram reader resolution."""
import re

from aqla_hyperliquid_mcp.models import AqlaError


def resolve_source(data, selector):
    names = data.get("source_names", {})
    matches = [identity for identity, name in names.items() if name.casefold() == selector.strip().casefold()]
    if len(matches) > 1:
        raise AqlaError("Source name is ambiguous; use its Telegram ID")
    return matches[0] if matches else selector


def supported_source(data, selector):
    if not isinstance(selector, str):
        raise AqlaError("Supply a Telegram ID, @username or a registered source name")
    selector = resolve_source(data, selector.strip()).strip()
    if re.fullmatch(r"-?[1-9]\d*", selector):
        return selector
    match = re.fullmatch(r"(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{3,31})/?", selector)
    if match:
        return "@" + match[1].lower()
    if re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{3,31}", selector):
        return selector.lower()
    raise AqlaError("Unknown source title; use its Telegram ID, @username or register its name first")


def change_sources(data, action, selectors, revision):
    if action not in ("set_sources", "add_sources", "remove_sources") or type(revision) is not int or revision != data["configuration_revision"]:
        raise AqlaError("Read the current configuration revision before changing sources")
    if selectors is None or not isinstance(selectors, list) or len(selectors) > 250 or (not selectors and action != "set_sources"):
        raise AqlaError("Supply explicit Telegram source selectors (at most 250)")
    selected = {supported_source(data, item) for item in selectors}
    existing = set(data["sources"])
    aliases = data.get("source_aliases", {})
    removed = {aliases.get(value, value) for value in selected}
    result = selected if action == "set_sources" else existing | selected if action == "add_sources" else {
        value for value in existing if aliases.get(value, value) not in removed}
    data["sources"] = sorted(result)
    from .ranking import source_rank
    for source in result:
        source_rank(data, source)
    data["source_aliases"] = {key: value for key, value in data.get("source_aliases", {}).items() if key in result}
    if result != existing:
        data["configuration_revision"] += 1
    return {"sources": data["sources"], "configuration_revision": data["configuration_revision"], "selection_action": action}


def set_source_name(data, identity, name, revision):
    if not identity or not re.fullmatch(r"-?[1-9]\d*", identity):
        raise AqlaError("Supply an actual numeric Telegram ID")
    if revision != data.get("source_names_revision", 0):
        raise AqlaError("Read source_names_revision before changing a name")
    names = data.setdefault("source_names", {})
    if name is None:
        names.pop(identity, None)
    else:
        name = name.strip()
        if (not 1 <= len(name) <= 100 or any(ord(char) < 32 for char in name)
                or name.casefold() in ("*", "owner_forward") or re.fullmatch(r"-?\d+", name) or name.startswith("@")):
            raise AqlaError("Supply a nonempty display name of at most 100 characters, distinct from IDs and selectors")
        if any(key != identity and value.casefold() == name.casefold() for key, value in names.items()):
            raise AqlaError("This source name already belongs to another Telegram ID")
        names[identity] = name
    data["source_names_revision"] = data.get("source_names_revision", 0) + 1
    return {"source_names": names, "source_names_revision": data["source_names_revision"]}
