from types import SimpleNamespace

import pytest

from aqla_agent.hermes_contract import HookContract, validate_hooks


def test_hook_contract_checks_names_parameter_kinds_defaults_and_async_without_mutation():
    async def correct(self, event, *, enabled=False):
        pass
    target = SimpleNamespace(hook=correct)
    contract = HookContract(target, "hook", "self,event,@enabled=", True)
    validate_hooks([contract])
    before = target.hook
    with pytest.raises(RuntimeError, match="no Aqla patches"):
        validate_hooks([HookContract(target, "missing", "self,event", True)])
    assert target.hook is before
    def changed(self, event, enabled=False):
        pass
    target.hook = changed
    with pytest.raises(RuntimeError, match="hook"):
        validate_hooks([contract])
    assert target.hook is changed
