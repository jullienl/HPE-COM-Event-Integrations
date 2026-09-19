"""Bounded Redfish evidence collector.

Fetches a *small, relevant* slice of a server's Redfish tree so an analyzer has
something concrete to reason about. Everything here exists to keep that slice
small — an unbounded bundle is the failure mode that actually bites:

* **Size.** An HPE iLO Integrated Management Log can hold hundreds of entries
  (~240 KB). Shipping that whole collection has been observed to truncate in
  transit behind a proxy and to blow an analyzer's context window. We keep only
  non-``OK`` entries, most recent first, capped by ``ILO_MAX_LOG_ENTRIES``.
* **Relevance.** Health rollups alone don't say *which* part failed. A fault in
  a DIMM, a fan, a PSU or a drive lives in a different resource, so the
  collector expands component collections but keeps **only the unhealthy
  members** (plus a count of the healthy ones, so "3 of 24 DIMMs degraded" is
  still expressible). The same trimming is applied to arrays *inside* a single
  document — a DL360 ``Thermal`` resource carries 48 temperature sensors and 7
  fans, ~26 KB of which is healthy-sensor noise on a server whose fault is
  somewhere else entirely.
* **Reachability.** Not every iLO exposes every path, and instance ids are not
  predictable — a Smart Array controller is as likely to be ``ArrayControllers/12``
  as ``/0``. Storage paths are therefore *discovered*, never assumed, and a
  missing resource is recorded as a note and skipped rather than raising: a
  partial bundle is far better than none.

Read-only by construction: this module issues ``GET`` requests only. Use a
dedicated read-only Redfish account; nothing here needs more.
"""

from __future__ import annotations

import logging
import os

import httpx

from ..secrets import get_secret

log = logging.getLogger("com-event-core.enrich.redfish")

# Always fetched: the overall state of the machine and its environmentals.
_BASE_PATHS = (
    "/redfish/v1/Systems/1",
    "/redfish/v1/Chassis/1/Thermal",
    "/redfish/v1/Chassis/1/Power",
)

# Component collections whose *unhealthy* members are expanded individually.
# Storage is NOT listed here: its path contains an unpredictable controller
# instance id, so it is discovered at runtime by _discover_storage_paths().
_COLLECTION_PATHS = ("/redfish/v1/Systems/1/Memory",)

# HPE's OEM storage tree, and the standard one. Both are tried: on an HPE iLO
# the drive health is reported under SmartStorage, and the standard
# Chassis/<id>/Drives copy of the same drives can legitimately still read OK.
_OEM_ARRAY_CONTROLLERS = "/redfish/v1/Systems/1/SmartStorage/ArrayControllers"
_STANDARD_STORAGE = "/redfish/v1/Systems/1/Storage"

# Arrays carried *inside* a resource that are trimmed to their interesting
# entries. These are the big ones: Temperatures alone is ~23 KB on a DL360.
_TRIMMED_ARRAYS = ("Temperatures", "Fans", "PowerSupplies")

# The Integrated Management Log — the richest signal, and the one that must be
# bounded hardest.
_LOG_PATH = "/redfish/v1/Systems/1/LogServices/IML/Entries"

# Status values that are not a problem.
_HEALTHY = {"ok", "enabled", "absent", "", "none"}


def _health_of(resource: dict) -> str:
    """Best-effort health of a Redfish resource, lower-cased ('' if unstated).

    Reads ``Status.Health`` from the resource's **own** Status block. Note this
    deliberately does not search the document for any ``Health`` key: a nested
    payload carries many, most belonging to sub-resources that are legitimately
    OK while the parent is not.
    """
    status = resource.get("Status") or {}
    return str(status.get("Health") or "").strip().lower()


def _crosses_threshold(entry: dict) -> bool:
    """True if a sensor reading has reached one of its own alarm thresholds.

    This is deliberately independent of ``Status.Health``, because the two are
    not populated consistently: a threshold-bearing sensor typically flags both,
    but a plain package/aggregate sensor often ships a reading with no
    thresholds and a permanently ``OK`` health. Keeping an entry whose reading
    has crossed a threshold means a hot component survives trimming even when
    the BMC never set its health field.
    """
    reading = entry.get("ReadingCelsius")
    if reading is None:
        reading = entry.get("Reading")
    if not isinstance(reading, (int, float)):
        return False

    for key in ("UpperThresholdCritical", "UpperThresholdNonCritical"):
        limit = entry.get(key)
        if isinstance(limit, (int, float)) and limit and reading >= limit:
            return True
    for key in ("LowerThresholdCritical", "LowerThresholdNonCritical"):
        limit = entry.get(key)
        if isinstance(limit, (int, float)) and limit and reading <= limit:
            return True
    return False


def _trim_arrays(path: str, resource: dict, bundle: dict) -> dict:
    """Drop healthy entries from the big sensor arrays inside `resource`.

    ``Thermal`` and ``Power`` are single documents holding dozens of sensors,
    nearly all of them fine. Unlike a collection of separate resources, the
    unhealthy-only rule has to be applied *within* the document or the noise
    ships regardless. Entries are kept when their own health is not OK **or**
    their reading has crossed a threshold; a per-array count is recorded so
    "1 of 48 sensors" stays expressible.
    """
    trimmed = dict(resource)
    for name in _TRIMMED_ARRAYS:
        entries = resource.get(name)
        if not isinstance(entries, list) or not entries:
            continue
        kept = [
            e for e in entries
            if _health_of(e) not in _HEALTHY or _crosses_threshold(e)
        ]
        trimmed[name] = kept
        bundle["trimmed"].append(
            f"{path}:{name} kept {len(kept)} of {len(entries)} (unhealthy or "
            f"threshold-crossing only)"
        )
    return trimmed


def _discover_storage_paths(client: RedfishClient, bundle: dict) -> list[str]:
    """Find this server's drive collections instead of assuming their ids.

    A Smart Array controller's instance id is arbitrary (``ArrayControllers/12``
    is as normal as ``/0``), so a hardcoded path silently yields no storage
    evidence at all — it degrades to a harmless-looking "not present" note while
    a failing drive goes unreported. Both the HPE OEM tree and the standard one
    are walked; on HPE hardware the drive health is the OEM copy.
    """
    paths: list[str] = []

    controllers = client.get(_OEM_ARRAY_CONTROLLERS)
    for ref in (controllers or {}).get("Members") or []:
        controller_path = ref.get("@odata.id")
        if controller_path:
            paths.append(f"{controller_path.rstrip('/')}/DiskDrives")

    storage = client.get(_STANDARD_STORAGE)
    for ref in (storage or {}).get("Members") or []:
        # A Storage resource is not a collection; its drives hang off a "Drives"
        # array, which _collect_unhealthy_members reads directly.
        if ref.get("@odata.id"):
            paths.append(ref["@odata.id"])

    if not paths:
        bundle["notes"].append("no storage controllers discovered on this iLO")
    return paths


class RedfishClient:
    """Minimal read-only Redfish client for one BMC."""

    def __init__(self, base_url: str) -> None:
        self._base = base_url.rstrip("/")

        username = os.environ.get("ILO_USERNAME")
        password = get_secret("ILO_PASSWORD", required=False)
        if not username or not password:
            raise RuntimeError(
                "iLO evidence collection needs ILO_USERNAME and ILO_PASSWORD "
                "(or ILO_PASSWORD_FILE). Use a dedicated read-only Redfish "
                "account — the collector only issues GET requests."
            )

        # iLOs ship a self-signed certificate, so verification needs an explicit
        # decision. ILO_CA_BUNDLE is the right answer; ILO_INSECURE=1 is a loud,
        # logged opt-out. It is NOT the default: the fleet-wide BMC credential
        # travels on this connection, and an unverified TLS session on the
        # management LAN is interceptable.
        #
        # Caveat worth knowing before you reach for ILO_CA_BUNDLE: pointing it at
        # the device's OWN self-signed certificate only works if that certificate
        # carries `basicConstraints: CA:TRUE`. OpenSSL will not use a plain leaf
        # certificate as a trust anchor and fails with "invalid CA certificate"
        # (verified against the HPE iLO emulator, whose generated cert has no
        # basicConstraints extension at all -- and PARTIAL_CHAIN does not help).
        # When the iLO's cert is a plain leaf, the options are to install a
        # CA-issued certificate on the iLO and trust that CA, or to accept
        # ILO_INSECURE=1 on a trusted management LAN.
        verify: str | bool = True
        ca_bundle = os.environ.get("ILO_CA_BUNDLE")
        if os.environ.get("ILO_INSECURE", "").strip().lower() in ("1", "true", "yes"):
            verify = False
            log.warning(
                "ILO_INSECURE is set: iLO TLS certificates are NOT verified. The "
                "iLO credential is exposed to interception on the management "
                "network. Set ILO_CA_BUNDLE instead for production."
            )
        elif ca_bundle:
            verify = ca_bundle

        self._client = httpx.Client(
            auth=(username, password),
            verify=verify,
            timeout=float(os.environ.get("ILO_TIMEOUT", "10")),
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def get(self, path: str) -> dict | None:
        """GET a Redfish resource. Returns None if it does not exist (404).

        Any other error propagates: a 401 is a config problem the operator must
        see, and a connection error means the whole bundle is worthless anyway.
        """
        r = self._client.get(f"{self._base}{path}")
        if r.status_code == 404:
            return None
        if r.is_error:
            raise RuntimeError(f"Redfish GET {path} failed: HTTP {r.status_code}")
        return r.json()


def _collect_log(client: RedfishClient, limit: int) -> tuple[list[dict], int]:
    """Return (interesting IML entries, total entry count).

    Keeps only entries whose ``Severity`` is not OK, most recent first, capped at
    `limit`. A 500-entry log becomes a handful of lines without losing the fault.
    """
    collection = client.get(_LOG_PATH)
    if not collection:
        return [], 0

    members = collection.get("Members") or []
    interesting = [
        m for m in members
        if str(m.get("Severity") or "").strip().lower() not in _HEALTHY
    ]
    # Most recent first. Entries without a Created stamp sort last.
    interesting.sort(key=lambda m: str(m.get("Created") or ""), reverse=True)

    trimmed = [
        {
            "Created": m.get("Created"),
            "Severity": m.get("Severity"),
            "Message": m.get("Message"),
            "EntryType": m.get("EntryType"),
        }
        for m in interesting[:limit]
    ]
    return trimmed, len(members)


def _collect_unhealthy_members(
    client: RedfishClient, path: str, max_members: int
) -> dict | None:
    """Expand a component collection, keeping only members that are not healthy.

    Returns a small summary dict, or None when the resource does not exist on
    this iLO. Handles both a true collection (``Members``) and a resource that
    references its children directly (a standard ``Storage`` resource lists
    ``Drives``).
    """
    collection = client.get(path)
    if not collection:
        return None

    members = collection.get("Members")
    if not members:
        members = collection.get("Drives") or []
    unhealthy: list[dict] = []
    inspected = 0

    for ref in members[:max_members]:
        member_path = ref.get("@odata.id")
        if not member_path:
            continue
        try:
            member = client.get(member_path)
        except Exception as e:
            log.debug("skipping %s: %s", member_path, e)
            continue
        if not member:
            continue
        inspected += 1
        if _health_of(member) not in _HEALTHY:
            unhealthy.append(member)

    return {
        "collection": path,
        "total_members": len(members),
        "inspected": inspected,
        "unhealthy_count": len(unhealthy),
        "unhealthy": unhealthy,
    }


def collect_evidence(mgmt_url: str) -> dict:
    """Build a bounded Redfish evidence bundle for the server at `mgmt_url`.

    Never raises for a *missing* resource — absent paths are recorded under
    ``notes``. Raises only when the BMC itself is unusable (auth failure,
    unreachable), which the caller treats as "deliver unenriched".
    """
    max_log = int(os.environ.get("ILO_MAX_LOG_ENTRIES", "25"))
    max_members = int(os.environ.get("ILO_MAX_MEMBERS", "64"))
    extra = [
        p.strip() for p in os.environ.get("ILO_EXTRA_PATHS", "").split(",") if p.strip()
    ]

    client = RedfishClient(mgmt_url)
    bundle: dict = {"source": mgmt_url, "resources": {}, "notes": [], "trimmed": []}

    try:
        for path in (*_BASE_PATHS, *extra):
            resource = client.get(path)
            if resource is None:
                bundle["notes"].append(f"{path} not present on this iLO")
                continue
            bundle["resources"][path] = _trim_arrays(path, resource, bundle)

        components: list[dict] = []
        for path in (*_COLLECTION_PATHS, *_discover_storage_paths(client, bundle)):
            summary = _collect_unhealthy_members(client, path, max_members)
            if summary is None:
                bundle["notes"].append(f"{path} not present on this iLO")
                continue
            components.append(summary)
        bundle["components"] = components

        entries, total = _collect_log(client, max_log)
        bundle["log"] = {
            "path": _LOG_PATH,
            "total_entries": total,
            "returned": len(entries),
            "filter": f"non-OK severity only, most recent {max_log}",
            "entries": entries,
        }
    finally:
        client.close()

    return bundle
