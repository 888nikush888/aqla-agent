"""Real MCP 2.2 results through the pinned native renderer/envelope, offline."""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from aqla_hyperliquid_mcp.models import require_first


async def produce(destination: Path):
    import socket

    import httpx

    assert socket.socket.connect.__module__ == "sitecustomize", "MCP child offline guard missing"
    sys.path.insert(0, str(destination / "src"))
    os.environ.pop("HL_PRIVATE_KEY", None)
    from test_notifications import lifecycle_fixture, result_payload

    from aqla_agent import notifications
    from aqla_hyperliquid_mcp.api import Hyperliquid
    from aqla_hyperliquid_mcp.models import Settings, digest
    from aqla_hyperliquid_mcp.server import close_server, create_server
    from aqla_hyperliquid_mcp.state import State

    assert Path(notifications.__file__).is_relative_to(destination / "src")
    output = destination / "native-notifications-wire.jsonl"
    actions = []
    def transport(request):
        if request.url.path == "/exchange":
            actions.append(request.url.path)
            raise AssertionError("Exchange mutation in notification read probe")
        return httpx.Response(200, json={})

    maxima, reach = {}, {}
    with output.open("w", encoding="utf-8") as target:
        async def emit(server, tool, label, **arguments):
            result = await server.call_tool(tool, arguments)
            payload = result_payload(result)
            wire = result.model_dump(mode="json", by_alias=True)
            target.write(json.dumps({"tool": tool, "label": label, "wire": wire}, ensure_ascii=False) + "\n")
            size = notifications.native_response_bytes(payload)
            maxima[tool] = max(maxima.get(tool, 0), size)
            if tool in ("notifications_read", "notifications_details"):
                assert size <= notifications.NATIVE_BYTE_BUDGET
            return payload

        for count, fills in ((400, 100), (10000, 2), (2, 4096)):
            settings = Settings(network="testnet", account="0x" + "0" * 40,
                                state_path=destination / f"fixture-{count}-{fills}.sqlite3",
                                integration_key_file=destination / "secrets/integration.secret")
            state = State(settings)
            state.update(lambda data, count=count, fills=fills, settings=settings: lifecycle_fixture(data, settings, count=count // 2, fills=fills))
            client = httpx.AsyncClient(base_url=settings.base_url, transport=httpx.MockTransport(transport))
            exchange = Hyperliquid(settings, state, client)
            server = create_server(settings, state=state, exchange=exchange)
            try:
                before = state.snapshot()
                snapshot = await emit(server, "integration", f"snapshot-{count}", action="snapshot", token=server.aqla_authority.key.decode())
                assert snapshot["notification_pending"] == count and "notifications" not in snapshot
                if count == 400:
                    await emit(server, "status", "status-400")
                cursor, identities = None, []
                while True:
                    page = await emit(server, "notifications_read", f"page-{count}", cursor=cursor)
                    identities.extend(page["notifications"])
                    for identity, notice in page["notifications"].items():
                        if notice.get("details_available"):
                            version = page["notification_versions"][identity]
                            fragments, detail_cursor = [], None
                            while True:
                                detail = await emit(server, "notifications_details", "large-fills", notification_id=identity,
                                                    version=version, cursor=detail_cursor, limit=65536)
                                fragments.append(detail["fragment"])
                                detail_cursor = detail["next_cursor"]
                                if detail["complete"]:
                                    break
                            assert "".join(fragments).encode() == notifications.canonical_json(before["notifications"][identity])
                    cursor = page["next_cursor"]
                    if cursor is None:
                        break
                assert len(identities) == count and len(set(identities)) == count
                assert set(identities) == set(before["notifications"])
                reach[str(count)] = count
                assert state.snapshot() == before
                if count == 400:
                    venue = server.aqla_trading.engine_for("hyperliquid", "testnet").state
                    long_reason = "🚀 [Daten]* \\ \"\n" * 100000
                    venue.pause_entries(long_reason)
                    page = await emit(server, "notifications_read", "protection-A", limit=1)
                    identity = require_first(iter(page["notifications"]))
                    version = page["notification_versions"][identity]
                    assert page["notifications"][identity]["detail_level"] == "summary"
                    originals = state.snapshot()["notifications"][identity]
                    fragments, detail_cursor = [], None
                    while True:
                        detail = await emit(server, "notifications_details", "unicode-reason", notification_id=identity,
                                            version=version, cursor=detail_cursor, limit=65536)
                        fragments.append(detail["fragment"])
                        detail_cursor = detail["next_cursor"]
                        if detail["complete"]:
                            break
                    assert "".join(fragments).encode() == notifications.canonical_json(originals)
                    venue.pause_entries("replacement B")
                    token = server.aqla_authority.sign("source", "synthetic-wake")
                    ack = await emit(server, "notifications_ack", "stale-A", notification_ids=[identity], versions={identity: version}, context_auth=token)
                    assert ack["stale"] == [identity] and ack["processed"] == []
                    b = await emit(server, "notifications_read", "protection-B", limit=1)
                    venue.pause_entries("replacement B")
                    again = await emit(server, "notifications_read", "same-reason-new-generation", limit=1)
                    assert b["notification_versions"][identity] != again["notification_versions"][identity]
                    assert state.snapshot()["notifications"][identity]["error"] == "replacement B"
                    ack = await emit(server, "notifications_ack", "current-version", notification_ids=[identity], versions=again["notification_versions"], context_auth=token)
                    assert ack["processed"] == [identity] and not ack["stale"]
                    assert digest(originals) == version
            finally:
                await close_server(server)
    assert not actions
    (destination / "native-notifications-producer.json").write_text(json.dumps({"max_predicted_bytes": maxima, "reachable": reach,
                                                                             "versioned_ack": True, "exchange_actions": 0}), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-source")
    parser.add_argument("--destination", required=True)
    parser.add_argument("--mcp-python")
    parser.add_argument("--producer", action="store_true")
    args = parser.parse_args()
    destination = Path(args.destination).absolute()
    if args.producer:
        asyncio.run(produce(destination))
        return
    if not args.hermes_source or not args.mcp_python:
        parser.error("--hermes-source and --mcp-python are required")
    project = Path(__file__).resolve().parents[1]
    os.environ["HERMES_HOME"] = str(destination / "hermes-home")
    os.environ["HERMES_SKIP_CONFIG_MIGRATION"] = "1"
    sys.path[:0] = [str(project / "src"), str(Path(args.hermes_source))]

    import yaml

    from aqla_agent.install import prepare
    prepare(project, destination, "12345", "synthetic-model", "https://example.invalid/v1")
    from tools.mcp_tool_config import _build_safe_env

    config_path = destination / "hermes-home/config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    guard = require_first(Path(item) for item in os.environ["PYTHONPATH"].split(os.pathsep) if (Path(item) / "sitecustomize.py").is_file())
    extra = {"PYTHONPATH": str(guard) + os.pathsep + str(destination / "src"), "UV_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1",
             "HERMES_DISABLE_LAZY_INSTALLS": "1", "HERMES_SKIP_UPDATE_CHECK": "1", "AQLA_AUDIT_NETWORK_LOG": os.environ["AQLA_AUDIT_NETWORK_LOG"]}
    for server in config["mcp_servers"].values():
        server["env"] = {**server.get("env", {}), **extra}
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    environment = _build_safe_env(extra)
    subprocess.run([args.mcp_python, "-c", "import socket; assert socket.socket.connect.__module__=='sitecustomize'"], env=environment, check=True)
    subprocess.run([args.mcp_python, str(Path(__file__).absolute()), "--producer", "--destination", str(destination)],
                   env=environment, check=True, timeout=600)
    from hermes_cli.plugins import PluginContext
    from mcp.types import CallToolResult
    from tools.mcp_tool_handlers import _render_call_tool_result

    from aqla_agent.events import HermesExtension
    from aqla_agent.hermes_runtime import install_strict_profile

    undo = install_strict_profile()
    maximum, calls = {}, 0
    try:
        with (destination / "native-notifications-wire.jsonl").open(encoding="utf-8") as source:
            for line in source:
                record = json.loads(line)
                wire = CallToolResult.model_validate(record["wire"])
                payload = record["wire"].get("structuredContent") or json.loads(require_first(block["text"] for block in record["wire"]["content"] if block["type"] == "text"))
                raw = _render_call_tool_result(wire, "aqla")
                size = len(raw.encode("utf-8"))
                tool = record["tool"]
                maximum[tool] = max(maximum.get(tool, 0), size)
                if tool in ("notifications_read", "notifications_details"):
                    assert size <= 600000
                envelope = PluginContext._mcp_envelope(raw)
                assert envelope.get("ok") and not envelope.get("truncated")
                extension = object.__new__(HermesExtension)
                extension.ctx = SimpleNamespace(call_mcp=lambda *_args, envelope=envelope: envelope)
                value = asyncio.run(extension.mcp_call("aqla", tool, {}))
                assert value == payload, "Native renderer changed/truncated the actual MCP result"
                # Exercise the real renderer's dual-emission contract using the
                # actual text block and its identical decoded structured value.
                dual = CallToolResult.model_validate({**record["wire"], "structuredContent": payload})
                assert _render_call_tool_result(dual, "aqla") == raw
                calls += 1
    finally:
        undo()
    producer = json.loads((destination / "native-notifications-producer.json").read_text(encoding="utf-8"))
    assert maximum == producer["max_predicted_bytes"], "Pinned serializer/render contract changed"
    print(json.dumps({"native_renderer_budget": True, "all_notifications_reachable": True, "versioned_ack": producer["versioned_ack"],
                      "exchange_actions": producer["exchange_actions"], "telegram_network_calls": 0, "max_native_bytes": maximum,
                      "real_native_results": calls, "reachable": producer["reachable"]}))


if __name__ == "__main__":
    main()
