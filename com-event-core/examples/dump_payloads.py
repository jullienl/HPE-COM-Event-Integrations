#!/usr/bin/env python3
"""Dump the exact object each adapter builds for a sample COM event.

This is the *live* companion to the "Example: one COM event across every
adapter" section in the README. The README snapshot is handy for a quick read,
but this script always reflects the **current** adapter mappings — run it after
changing an adapter to see (and copy) the real payloads.

It doesn't talk to any target: it sets placeholder credentials, instantiates
each adapter, and calls its private ``_to_*`` transform (or ``asdict`` for the
generic webhook) on a normalised event. Nothing is sent over the network.

Usage:
    python examples/dump_payloads.py                 # server raise (default)
    python examples/dump_payloads.py server clear
    python examples/dump_payloads.py alert  raise
    python examples/dump_payloads.py alert  clear
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import sys

# Placeholder config so each adapter's __init__ (which reads env) succeeds.
# These are never used to send anything — the script only calls _to_* mappers.
_PLACEHOLDER_ENV = {
    "OBM_EVENT_API_URL": "https://obm.example.com/event",
    "OBM_USER": "user",
    "OBM_PASSWORD": "pass",
    "SNOW_INSTANCE": "https://acme.service-now.com",
    "SNOW_USER": "user",
    "SNOW_PASSWORD": "pass",
    "OPSRAMP_API_URL": "https://acme.api.opsramp.com",
    "OPSRAMP_TENANT_ID": "client_1234",
    "OPSRAMP_KEY": "key",
    "OPSRAMP_SECRET": "secret",
    "HALO_API_URL": "https://acme.haloitsm.com",
    "HALO_CLIENT_ID": "id",
    "HALO_CLIENT_SECRET": "secret",
    "SPLUNK_HEC_URL": "https://splunk.example.com:8088/services/collector/event",
    "SPLUNK_HEC_TOKEN": "token",
    "WEBHOOK_URL": "https://example.com/inbound",
}
for _k, _v in _PLACEHOLDER_ENV.items():
    os.environ.setdefault(_k, _v)

from com_event_core.normalize import normalize  # noqa: E402
from com_event_core.adapters.obm import ObmAdapter  # noqa: E402
from com_event_core.adapters.servicenow import ServiceNowAdapter  # noqa: E402
from com_event_core.adapters.opsramp import OpsRampAdapter  # noqa: E402
from com_event_core.adapters.halo import HaloAdapter  # noqa: E402
from com_event_core.adapters.splunk import SplunkAdapter  # noqa: E402


# --- Sample COM payloads --------------------------------------------------

_SERVER_RAISE = {
    "type": "compute-ops-mgmt/server",
    "id": "P28948-B21+CZ2311004G",
    "name": "ESX-node-01",
    "operation": "Updated",
    "updatedAt": "2025-01-01T10:00:00Z",
    "hardware": {
        "serialNumber": "CZ2311004G",
        "productId": "P28948-B21",
        "model": "ProLiant DL360 Gen11",
        "bmc": {"ip": "10.0.0.5"},
        "health": {
            "summary": "CRITICAL",
            "fans": "OK",
            "powerSupplies": "CRITICAL",
            "memory": "OK",
        },
    },
}

_ALERT_RAISE = {
    "type": "compute-ops-mgmt/alert",
    "id": "alert-abc-123",
    "operation": "Created",
    "severity": "Critical",
    "createdAt": "2025-01-01T10:00:00Z",
    "description": "Power supply 2 failed",
    "resolution": "Replace PSU 2",
    "category": "hardware",
    "device": {"id": "P28948-B21+CZ2311004G"},
}


def _build_payload(resource: str, action: str) -> dict:
    """Return a sample COM payload for the requested resource/action."""
    if resource == "server":
        payload = copy.deepcopy(_SERVER_RAISE)
        if action == "clear":
            payload["hardware"]["health"] = {
                "summary": "OK",
                "fans": "OK",
                "powerSupplies": "OK",
            }
        return payload
    if resource == "alert":
        payload = copy.deepcopy(_ALERT_RAISE)
        if action == "clear":
            payload["operation"] = "Deleted"
            payload["cleared"] = True
            payload["clearedAt"] = "2025-01-01T11:00:00Z"
        return payload
    raise SystemExit(f"unknown resource '{resource}' (use 'server' or 'alert')")


def _dump(label: str, obj: object) -> None:
    print(f"\n----- {label} -----")
    print(json.dumps(obj, indent=2, default=str))


def main() -> None:
    resource = sys.argv[1] if len(sys.argv) > 1 else "server"
    action = sys.argv[2] if len(sys.argv) > 2 else "raise"

    payload = _build_payload(resource, action)
    events = normalize(payload)

    print(f"===== COM {resource} {action} =====")
    _dump("1. raw COM payload in", payload)
    if len(events) > 1:
        _dump(
            f"2. normalize() -> {len(events)} CanonicalEvents (one per condition)",
            [dataclasses.asdict(e) for e in events],
        )
    else:
        _dump("2. normalize() -> CanonicalEvent", dataclasses.asdict(events[0]))

    # Adapters map one event at a time; show what each builds for the first.
    event = events[0]

    print("\n===== 3. what each adapter builds =====")
    _dump("obm", ObmAdapter()._to_obm(event))
    _dump("servicenow (em_event)", ServiceNowAdapter()._to_event(event))
    os.environ["SNOW_TABLE"] = "incident"
    _dump("servicenow (incident)", ServiceNowAdapter()._to_incident(event))
    os.environ.pop("SNOW_TABLE", None)
    _dump("opsramp", OpsRampAdapter()._to_alert(event))
    _dump("halo", [HaloAdapter()._to_ticket(event)])
    _dump("splunk", SplunkAdapter()._to_hec(event))
    _dump("webhook (full CanonicalEvent)", dataclasses.asdict(event))


if __name__ == "__main__":
    main()
