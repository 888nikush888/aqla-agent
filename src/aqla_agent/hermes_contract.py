"""Pinned private Hermes hooks checked before any Aqla monkeypatch is installed."""
import importlib
import inspect
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

PIN = "1a4508e2aff2db5f50409893a2115be777bd5643"


@dataclass(frozen=True)
class HookContract:
    target: object
    name: str
    parameters: str
    asynchronous: bool = False


def parameter_contract(function) -> str:
    while hasattr(function, "__aqla_contract_original__"):
        function = function.__aqla_contract_original__
    values = []
    for parameter in inspect.signature(function).parameters.values():
        prefix = {inspect.Parameter.POSITIONAL_ONLY: "/", inspect.Parameter.VAR_POSITIONAL: "*", inspect.Parameter.VAR_KEYWORD: "**", inspect.Parameter.KEYWORD_ONLY: "@"}.get(parameter.kind, "")
        values.append(prefix + parameter.name + ("=" if parameter.default is not inspect.Parameter.empty else ""))
    return ",".join(values)


def validate_hooks(contracts: list[HookContract]) -> None:
    failures = []
    for contract in contracts:
        function = getattr(contract.target, contract.name, None)
        original = function
        while hasattr(original, "__aqla_contract_original__"):
            original = original.__aqla_contract_original__
        try:
            valid = callable(function) and parameter_contract(function) == contract.parameters and inspect.iscoroutinefunction(original) == contract.asynchronous
        except (TypeError, ValueError):
            valid = False
        if not valid:
            failures.append(contract.name)
    if failures:
        raise RuntimeError("Pinned Hermes compatibility contract changed; no Aqla patches installed: " + ", ".join(failures))


def check_native_contract() -> None:
    specifications = [
        ("agent.fallback_cooldown", "", "_arm_rate_limit_cooldown", "agent,reason,reset_at=", False),
        ("agent.agent_runtime_helpers", "", "restore_primary_runtime", "agent", False),
        ("gateway.run_turn", "GatewayTurnMixin", "_resolve_turn_toolsets", "self,user_config,source,platform_key", False),
        ("agent.turn_usage", "", "record_response_usage", "agent,response,@messages,@api_call_count,@api_duration,@compression_attempts,@max_compression_attempts", False),
        ("agent.turn_response_check", "", "record_response_usage", "agent,response,@messages,@api_call_count,@api_duration,@compression_attempts,@max_compression_attempts", False),
        ("agent.context_compressor", "", "call_llm", "task=,@provider=,@model=,@base_url=,@api_key=,@main_runtime=,@messages,@temperature=,@max_tokens=,@tools=,@timeout=,@extra_body=,@reasoning_config=,@extra_headers=,@api_mode=,@stream=,@stream_options=,@route_info=,@latency_info=", False),
        ("agent.context_compressor", "", "_summarize_tool_result", "tool_name,tool_args,tool_content", False),
        ("agent.auxiliary_client", "", "call_llm", "task=,@provider=,@model=,@base_url=,@api_key=,@main_runtime=,@messages,@temperature=,@max_tokens=,@tools=,@timeout=,@extra_body=,@reasoning_config=,@extra_headers=,@api_mode=,@stream=,@stream_options=,@route_info=,@latency_info=", False),
        ("agent.auxiliary_client", "", "extract_content_or_reasoning", "response,@max_reasoning_chars=", False),
        ("agent.usage_pricing", "", "estimate_usage_cost", "model_name,usage,@provider=,@base_url=,@api_key=", False),
        ("agent.aux_accounting", "", "set_accounting_context", "session_db,session_id", False),
        ("agent.aux_accounting", "", "reset_accounting_context", "token", False),
        ("gateway.run_turn", "GatewayTurnMixin", "_resolve_session_agent_runtime", "self,@source=,@session_key=,@user_config=", False),
        ("gateway.run", "", "_start_gateway_start_cron_and_housekeeping", "runner", False),
        ("gateway.run_turn", "GatewayTurnMixin", "_run_agent", "self,message,context_prompt,history,source,session_id,**turn_kwargs", True),
        ("gateway.run_turn", "GatewayTurnMixin", "_run_agent_queued_followup", "self,turn_ctx,adapter,pending,pending_event,response,result,stream_task", True),
        ("gateway.run_adapters", "GatewayAdapterLifecycleMixin", "_install_reconnected_adapter", "self,platform,adapter", True),
        ("gateway.run_adapters", "GatewayAdapterLifecycleMixin", "_create_adapter", "self,platform,config", False),
        ("gateway.run_turn_runner", "TurnRunner", "_clarify_callback_sync", "self,questions", False),
        ("gateway.slash_commands", "GatewaySlashCommandsMixin", "_handle_help_command", "self,event", True),
        ("gateway.slash_commands", "GatewaySlashCommandsMixin", "_handle_commands_command", "self,event", True),
        ("hermes_cli.commands", "", "gateway_help_lines", "allowed=", False),
        ("gateway.platforms.base", "BasePlatformAdapter", "_can_merge_text_debounce_events", "self,existing,event", False),
        ("gateway.run_startup", "GatewayStartupMixin", "_start_finish_wiring", "self,connected_count", True),
        ("gateway.run_startup", "GatewayStartupMixin", "_start_post_connect_services", "self,connected_count", True),
        ("hermes_cli.plugins", "PluginContext", "_mcp_envelope", "raw", False),
        ("hermes_cli.plugins", "PluginContext", "call_mcp", "self,server,tool,arguments=,timeout=", False),
        ("tools.write_approval", "", "write_approval_enabled", "subsystem", False),
        ("tools.write_approval", "", "stage_write", "subsystem,payload,@summary,@origin", False),
        ("tools.write_approval", "", "list_pending", "subsystem", False),
        ("tools.write_approval", "", "get_pending", "subsystem,pending_id", False),
        ("tools.skill_manager_tool", "", "apply_skill_pending", "payload", False),
        ("tools.skill_manager_tool", "", "_skill_mutation_lock", "name", False),
        ("tools.skill_usage", "", "adopt_skill", "skill_name", False),
        ("tools.fuzzy_match", "", "fuzzy_find_and_replace", "content,old_string,new_string,replace_all=", False),
        ("agent.background_review", "", "spawn_background_review_thread", "agent,messages_snapshot,review_memory=,review_skills=,focus=,task_cfg=,review_run=,explicit=", False),
        ("agent.background_review", "", "finish_background_review_run", "agent,run", False),
        ("agent.background_review", "", "_action_lines", "data,detail,verbose", False),
        ("agent.background_review", "", "_publish_review_summary", "agent,actions", False),
        ("agent.tool_executor", "", "_unwrap_tool_search_call", "agent,function_name,function_args,@flatten_probe=", False),
        ("agent.tool_executor", "", "_commit_tool_result", "agent,messages,ref,function_result,@budget,@tool_duration,@is_error,@blocked,@effect_disposition,@observed=,@error_preview=,@success_log_chars=,@verbose_text=", False),
        ("agent.tool_executor", "", "_finalize_tool_batch", "agent,messages,effective_task_id,num_tools,budget", False),
        ("agent.tool_executor", "", "maybe_persist_tool_result", "content,tool_name,tool_use_id,env=,config=,threshold=", False),
        ("tools.tool_result_storage", "", "maybe_persist_tool_result", "content,tool_name,tool_use_id,env=,config=,threshold=", False),
        ("tools.tool_search_validation", "", "_schema_for_local_validation", "node", False),
        ("tools.tool_search_validation", "", "_schema_has_external_ref", "node", False),
        ("model_tools", "", "_dispatch_bridge_tool", "function_name,function_args,enabled_toolsets,disabled_toolsets", False),
        ("agent.tool_guardrails", "ToolCallGuardrailController", "_build_result_reference_stub", "self,tool_name,args", False),
        ("gateway.run_plugin_rewire", "GatewayPluginRewireMixin", "_rewire_plugin_handlers", "self,profile_name=,profile_home=", False),
        ("agent.tool_dispatch_helpers", "", "_is_untrusted_tool", "name", False),
    ]
    contracts = []
    for module_name, class_name, name, parameters, asynchronous in specifications:
        try:
            module = importlib.import_module(module_name)
            target = getattr(module, class_name) if class_name else module
        except (ImportError, AttributeError) as error:
            raise RuntimeError(f"Pinned Hermes compatibility target missing: {module_name}.{class_name or name}; no Aqla patches installed") from error
        contracts.append(HookContract(target, name, parameters, asynchronous))
    gateway = importlib.import_module("gateway.run")
    root = Path(gateway.__file__).resolve().parents[1]
    try:
        revision = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL, timeout=10).strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("Pinned Hermes source revision could not be verified; no Aqla patches installed") from error
    if revision != PIN:
        raise RuntimeError("Pinned Hermes source revision differs; no Aqla patches installed")
    validate_hooks(contracts)
    for module_name, class_name, name, kind in (
        ("gateway.run", "GatewayRunner", "_SECURITY_METADATA_KEYS", tuple),
        ("gateway.run_startup", "GatewayStartupMixin", "_PRE_RECONNECT_WATCHERS", tuple),
        ("gateway.run_startup", "GatewayStartupMixin", "_POST_RECONNECT_WATCHERS", tuple),
        ("hermes_cli.plugins", "PluginContext", "_MCP_RESULT_CHAR_CAP", int),
    ):
        target = getattr(importlib.import_module(module_name), class_name)
        if not isinstance(getattr(target, name, None), kind):
            raise RuntimeError(f"Pinned Hermes compatibility attribute changed: {name}; no Aqla patches installed")


def check_telegram_contract(adapter) -> None:
    """Validate the discovered factory product, never a separately imported lookalike."""
    specifications = (
        ("_build_message_event", "self,message,msg_type,update_id=", False),
        ("_register_handlers", "self,app", False),
        ("_accept_update", "self", False),
        ("_text_batch_key", "self,event", False),
        ("_photo_batch_key", "self,event,msg", False),
        ("_queue_media_group_event", "self,media_group_id,event", True),
        ("_register_command_menu", "self", True),
        ("_bot_commands", "menu_commands", False),
        ("send", "self,chat_id,content,reply_to=,metadata=", True),
    )
    validate_hooks([HookContract(type(adapter), name, parameters, asynchronous) for name, parameters, asynchronous in specifications])
    actual = SimpleNamespace(**{name: getattr(getattr(adapter, name, None), "__func__", getattr(adapter, name, None))
                               for name, _, _ in specifications})
    validate_hooks([HookContract(actual, name, parameters, asynchronous) for name, parameters, asynchronous in specifications])
