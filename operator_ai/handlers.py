"""Import every module that registers job handlers or queue approval hooks."""
from __future__ import annotations

import importlib

MODULES = [
    "operator_ai.agents.sales", "operator_ai.agents.accountability", "operator_ai.actions", "operator_ai.webinar_chain",
    "operator_ai.studio", "operator_ai.outreach_loop", "operator_ai.departments", "operator_ai.skills",
    "operator_ai.live.chain", "operator_ai.channels", "operator_ai.crm",
    # feature modules: importing each registers its queue hooks, event subscribers and job handlers
    "operator_ai.missions", "operator_ai.prospects", "operator_ai.proposals", "operator_ai.academy",
    "operator_ai.roleplay", "operator_ai.persona", "operator_ai.delivery", "operator_ai.inbound",
    "operator_ai.escalations", "operator_ai.webinars", "operator_ai.client_ops", "operator_ai.playbooks",
    "operator_ai.team",  # on_sent: removes a one-time link from the queue copy once the email has really gone out
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
