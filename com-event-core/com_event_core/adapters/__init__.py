"""Adapter registry — selects TargetAdapter(s) by the TARGETS env var.

Backends are imported lazily so a given deployment only needs the dependencies
and config for the target(s) it actually uses.
"""

from __future__ import annotations

import os

from .base import TargetAdapter

# Known adapters: TARGETS value -> (module, class).
_ADAPTERS = {
    "obm": ("com_event_core.adapters.obm", "ObmAdapter"),
    "servicenow": ("com_event_core.adapters.servicenow", "ServiceNowAdapter"),
    "opsramp": ("com_event_core.adapters.opsramp", "OpsRampAdapter"),
    "halo": ("com_event_core.adapters.halo", "HaloAdapter"),
    "splunk": ("com_event_core.adapters.splunk", "SplunkAdapter"),
    "github": ("com_event_core.adapters.github", "GitHubAdapter"),
    "slack": ("com_event_core.adapters.slack", "SlackAdapter"),
    "teams": ("com_event_core.adapters.teams", "TeamsAdapter"),
    "jira": ("com_event_core.adapters.jira", "JiraAdapter"),
    "pagerduty": ("com_event_core.adapters.pagerduty", "PagerDutyAdapter"),
    "sentinel": ("com_event_core.adapters.sentinel", "SentinelAdapter"),
    "datadog": ("com_event_core.adapters.datadog", "DatadogAdapter"),
    "elastic": ("com_event_core.adapters.elastic", "ElasticAdapter"),
    "bmc_helix": ("com_event_core.adapters.bmc_helix", "BmcHelixAdapter"),
    "dynatrace": ("com_event_core.adapters.dynatrace", "DynatraceAdapter"),
    "grafana": ("com_event_core.adapters.grafana", "GrafanaAdapter"),
    "webhook": ("com_event_core.adapters.webhook", "WebhookAdapter"),
}


def _instantiate(name: str) -> TargetAdapter:
    """Import + construct the adapter registered under `name`."""
    if name not in _ADAPTERS:
        supported = ", ".join(sorted(_ADAPTERS))
        raise ValueError(f"Unsupported target '{name}'. Supported: {supported}.")

    module_name, class_name = _ADAPTERS[name]
    module = __import__(module_name, fromlist=[class_name])
    adapter_cls = getattr(module, class_name)
    return adapter_cls()


def get_adapters() -> list[TargetAdapter]:
    """Instantiate the target adapter(s) for this deployment (one or many).

    `TARGETS` is the single selector: one name (e.g. "halo") or a comma-separated
    list for fan-out (e.g. "halo,slack"); it defaults to "webhook" (the
    vendor-neutral target) when unset.

    Names are lower-cased and de-duplicated with order preserved; each must be a
    known adapter or a ValueError is raised. Always returns at least one adapter.
    """
    raw = os.environ.get("TARGETS", "webhook")
    ordered: list[str] = []
    for part in raw.split(","):
        name = part.strip().lower()
        if name and name not in ordered:
            ordered.append(name)
    if not ordered:
        ordered = ["webhook"]
    return [_instantiate(name) for name in ordered]
