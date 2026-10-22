"""Hyperliquid execution accounts and historical operation redaction."""
from aqla_hyperliquid_mcp.models import AqlaError


def execution_settings(root, key):
    configured = root.execution_accounts.get(key)
    if configured is None or not configured.private_key_file or not configured.private_key_file.is_file():
        return None
    if configured.exchange != "hyperliquid" or key != "hyperliquid:" + configured.network:
        raise AqlaError("Execution account key differs from Hyperliquid/network")
    if not configured.account:
        return None
    return root.model_copy(update={"network": configured.network, "account": configured.account, "vault_address": configured.vault_address,
        "signer_role": configured.signer_role, "private_key_file": configured.private_key_file, "shared_account": configured.shared_account})


def public_operation(row):
    if isinstance(row, list):
        return [public_operation(value) for value in row]
    if isinstance(row, dict):
        return {key: public_operation(value) for key, value in row.items() if key != "signed_requests"}
    return row
