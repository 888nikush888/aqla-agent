"""Owner-supplied labels are independent of Telegram reader resolution."""
import re

from aqla_hyperliquid_mcp.models import AqlaError


def resolve_source(data, selector):
    names = data.get("source_names", {})
    matches = [identity for identity, name in names.items() if name.casefold() == selector.strip().casefold()]
    if len(matches) > 1:
        raise AqlaError("Source name is ambiguous; use its Telegram ID")
    return matches[0] if matches else selector


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
