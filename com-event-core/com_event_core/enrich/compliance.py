"""Group firmware-compliance lookup — is an assigned baseline actually applied?

A server's own `firmwareBundleUri`/`lastFirmwareUpdate` only reflect a direct,
one-off "update firmware" action on that device. A COM **group** can separately
have a firmware baseline *assigned* to it without every member actually being
compliant with it yet (adds since assigned, a failed/partial update, drift after
the fact, ...). So "this event's server has a `firmwareBundleUri`" is NOT proof
that bundle is what is actually installed — only `GET /groups/{id}/compliance`
(the documented, supported endpoint for this) says that.

There is no reverse "which group is this device in" lookup, so group membership
is found by paging `GET /groups` and checking each group's own `devices` array —
bounded by `max_groups` so a very large fleet can't turn one event into an
unbounded scan.
"""

from __future__ import annotations

import logging

from ..com_client import ComClient

log = logging.getLogger("com_event_core.enrich.compliance")

_PAGE_SIZE = 100


def get_ui_doorway_compliance(client: ComClient, device_id: str) -> dict | None:
    """Normalize the server-level compliance report exposed by the GreenLake UI.

    This is a fallback for devices that have no group. The UI response carries
    the installed/baseline bundle under ``baselineDerived_`` and the device's
    score/deviations under ``serverFirmwareCompliance_``. Missing compliance
    data is a normal ``None`` result; endpoint/shape failures are allowed to
    propagate as ``ComUiDoorwayError`` so the caller can identify the boundary
    that drifted while still failing open for delivery.
    """
    body = client.get_ui_doorway_server(device_id)
    baseline = body.get("baselineDerived_") or {}
    report = body.get("serverFirmwareCompliance_") or {}
    if not baseline and not report:
        return None
    if not isinstance(baseline, dict) or not isinstance(report, dict):
        raise ValueError("UI-doorway compliance fields are not JSON objects")

    return {
        "source": "server_ui_doorway",
        "group_id": None,
        "group_name": None,
        "group_firmware_status": report.get("complianceState"),
        "assigned_bundle_id": report.get("bundleId"),
        "baseline_bundle_id": baseline.get("id"),
        "baseline_release_version": baseline.get("releaseVersion"),
        "baseline_display_name": baseline.get("displayName"),
        "compliance_state": report.get("complianceState"),
        "score": report.get("score"),
        "deviations": [
            {
                "category": d.get("category"),
                "component": d.get("componentName"),
                "expected_version": d.get("recommendedVersion"),
                "installed_version": d.get("installedVersion"),
            }
            for d in (report.get("deviations") or [])
            if isinstance(d, dict)
        ],
    }


def find_group_for_device(client: ComClient, device_id: str, *, max_groups: int = 500) -> dict | None:
    """Return the first group whose `devices` list contains `device_id`, else None.

    Pages through `GET /groups` up to `max_groups` groups. A device in more than
    one group is unusual (COM groups are generally non-overlapping in practice)
    so the first match is returned rather than searching for every group.
    """
    scanned = 0
    offset = 0
    while scanned < max_groups:
        page = client.list_groups(limit=_PAGE_SIZE, offset=offset)
        items = page.get("items") or []
        if not items:
            break
        for group in items:
            devices = group.get("devices") or []
            if any(d.get("deviceId") == device_id or d.get("id") == device_id for d in devices):
                return group
        scanned += len(items)
        offset += len(items)
        if len(items) < _PAGE_SIZE or offset >= (page.get("total") or 0):
            break
    return None


def get_device_compliance(client: ComClient, group: dict, device_id: str) -> dict | None:
    """Compact per-device compliance record for `device_id` within `group`.

    Returns None when the group has no compliance record for this device (no
    baseline assigned, or the device isn't tracked for compliance) — a normal,
    unremarkable outcome, not an error.
    """
    page = client.get_group_compliance(group["id"], limit=_PAGE_SIZE)
    record = next(
        (r for r in (page.get("items") or []) if r.get("deviceId") == device_id),
        None,
    )
    if record is None:
        return None

    return {
        "group_id": group.get("id"),
        "group_name": group.get("name"),
        # The group's own overall firmware rollup, already on the group object
        # itself (no extra call) — useful context even when this device's own
        # record below is missing/partial.
        "group_firmware_status": ((group.get("groupCompliance") or {}).get("firmware") or {}).get("status"),
        "assigned_bundle_id": record.get("bundleId"),
        "compliance_state": record.get("complianceState"),
        "score": record.get("score"),
        # Trimmed to the fields useful for root-cause reasoning; `remediation`
        # (update-planning metadata: prerequisites, shutdown mode, denied
        # lists, ...) is dropped as noise for this purpose.
        "deviations": [
            {
                "category": d.get("category"),
                "component": d.get("componentName"),
                "expected_version": d.get("expectedVersion"),
                "installed_version": d.get("installedVersion"),
            }
            for d in (record.get("deviations") or [])
        ],
    }


def resolve_compliance(client: ComClient, device_id: str) -> dict | None:
    """Find `device_id`'s group (if any) and its compliance against that group's
    assigned baseline. Returns None when the device isn't in a group, the group
    has no compliance data for it, or any lookup step fails — all normal,
    fail-open outcomes handled by the caller the same way.
    """
    group = find_group_for_device(client, device_id)
    if group is None:
        return None
    return get_device_compliance(client, group, device_id)
