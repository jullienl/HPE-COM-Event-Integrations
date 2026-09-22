"""Regression tests for `_normalize_alert()` robustness against malformed
COM alert payloads (a webhook payload is less trusted than a COM API
response, so a shaped/malformed field must never crash normalization).
"""

from __future__ import annotations

from com_event_core.normalize import normalize


def test_non_string_description_does_not_crash():
    # A malformed payload where 'description' is a dict, not a string.
    # title = (description or ...).splitlines() used to raise AttributeError.
    payload = {
        "type": "compute-ops-mgmt/alert",
        "id": "alert-1",
        "operation": "Created",
        "severity": "Critical",
        "description": {"unexpected": "shape"},
        "device": {"id": "P1-B21+SN1"},
    }
    events = normalize(payload)
    assert len(events) == 1
    assert events[0].title  # coerced to a string, not raised


def test_non_dict_device_does_not_crash():
    payload = {
        "type": "compute-ops-mgmt/alert",
        "id": "alert-2",
        "operation": "Created",
        "severity": "Critical",
        "description": "SNMPv1 is enabled.",
        "device": ["not", "a", "dict"],
    }
    events = normalize(payload)
    assert len(events) == 1
    assert events[0].resource_serial is None
