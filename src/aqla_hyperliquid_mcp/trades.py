"""Compatibility module alias, including existing monkeypatch/import boundaries."""
import sys

from aqla_agent import trading

sys.modules[__name__] = trading
