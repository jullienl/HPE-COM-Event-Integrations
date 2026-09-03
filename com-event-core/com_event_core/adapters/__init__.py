"""Adapter registry — selects a TargetAdapter by the TARGET env var.

Backends are imported lazily so a given deployment only needs the dependencies
and config for the target it actually uses.
"""

from __future__ import annotations

import os

from .base import TargetAdapter

# Known adapters: TARGET value -> (module, class).
_ADAPTERS = {
    "obm": ("com_event_core.adapters.obm", "ObmAdapter"),
    "servicenow": ("com_event_core.adapters.servicenow", "ServiceNowAdapter"),
    "splunk": ("com_event_core.adapters.splunk", "SplunkAdapter"),
    "webhook": ("com_event_core.adapters.webhook", "WebhookAdapter"),
}


def get_adapter() -> TargetAdapter:
    """Instantiate the adapter named by TARGET (default 'obm')."""
    target = os.environ.get("TARGET", "obm").strip().lower()
    if target not in _ADAPTERS:
        supported = ", ".join(sorted(_ADAPTERS))
        raise ValueError(f"Unsupported TARGET '{target}'. Supported: {supported}.")

    module_name, class_name = _ADAPTERS[target]
    module = __import__(module_name, fromlist=[class_name])
    adapter_cls = getattr(module, class_name)
    return adapter_cls()
