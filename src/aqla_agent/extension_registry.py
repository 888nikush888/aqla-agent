"""Weak native extension bindings shared by admission and catalog hooks."""
from __future__ import annotations

import weakref
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .events import HermesExtension

EXTENSIONS: weakref.WeakSet[HermesExtension] = weakref.WeakSet()


def gateway_extension(gateway):
    return next((extension for extension in tuple(EXTENSIONS) if extension.gateway is gateway), None)
