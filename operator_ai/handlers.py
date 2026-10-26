"""Import every module that registers job handlers or queue approval hooks."""
from __future__ import annotations

import importlib

MODULES = [
    "operator_ai.agents.sales", "operator_ai.agents.accountability", "operator_ai.actions", "operator_ai.webinar_chain",
    "operator_ai.studio", "operator_ai.outreach_loop", "operator_ai.departments", "operator_ai.skills",
    "operator_ai.live.chain", "operator_ai.channels", "operator_ai.crm",
]
_loaded = False


def load() -> None:
    global _loaded
    if _loaded:
        return
    for m in MODULES:
        try:
            importlib.import_module(m)
        except ModuleNotFoundError as e:
            if e.name != m:
                raise
    _loaded = True
