"""Minimal read-only HPE GreenLake Compute Ops Management (COM) API client.

Used by optional enrichers that need authoritative COM data beyond what already
travels on a webhook payload — currently: resolving a server's firmware bundle
(and its ``advisories`` link) when the payload didn't carry
``firmwareBundleUri``. Read-only by construction: only GET is issued.

Two auth options, in preference order:

1. **Client credentials** (``COM_CLIENT_ID`` + ``COM_CLIENT_SECRET``) — a
   GreenLake service client's id/secret are exchanged for a short-lived
   (~2h) access token via the SSO token endpoint, cached in-process and
   auto-refreshed shortly before it expires. This is the only option that
   works unattended for a long-running shim/bridge: a manually-issued PAT
   expires in ~2h with no refresh mechanism of its own.
2. **A static Personal Access Token** (``COM_PAT`` / ``COM_PAT_FILE``) — fine
   for a quick manual test, but it will expire (~2h) with nothing to refresh
   it; expect `401`s once it does.

Read secrets only through ``get_secret()`` — ``_FILE`` in production, the
plain env var as the dev fallback — never ``os.environ`` directly, per this
repo's file-or-env secrets rule.

``COM_BASE_URL`` has no default: COM's API base URL is region/environment
specific (this is the same ``<COM-API-base-URL>`` placeholder already used
throughout the webhook setup docs), so an operator must set it explicitly
rather than the client silently guessing wrong.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from urllib.parse import quote, urlparse

import httpx

from .secrets import get_secret

log = logging.getLogger("com_event_core.com_client")

# Default GreenLake SSO token endpoint for the client_credentials grant.
# Unlike COM_BASE_URL (region-specific), this is the same host for every
# GreenLake account; override with COM_SSO_TOKEN_URL only if HPE tells you to.
_DEFAULT_SSO_TOKEN_URL = "https://sso.common.cloud.hpe.com/as/token.oauth2"

# A token is refreshed this many seconds before its stated expiry, so a
# request already in flight doesn't get built with a token that expires
# mid-call.
_REFRESH_MARGIN_SECONDS = 60


class ComApiError(RuntimeError):
    """A COM API call failed; the message includes the body HPE sent back."""


class ComUiDoorwayError(ComApiError):
    """The internal UI-doorway compliance fallback failed or changed shape."""


class _TokenCache:
    """Process-wide cache of one client_credentials access token.

    Shared across every `ComClient` instance so that the short-lived,
    one-call-then-close usage pattern in the enrichers (`with ComClient() as
    client: ...`) does not mint a brand new token on every single call — the
    SSO token endpoint is called only once per ~2h token lifetime, not once
    per COM API request.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    def get(self, fetch: "callable[[], tuple[str, int]]") -> str:
        with self._lock:
            if self._token and time.time() < self._expires_at:
                return self._token
            token, expires_in = fetch()
            self._token = token
            self._expires_at = time.time() + max(expires_in - _REFRESH_MARGIN_SECONDS, 0)
            log.info("COM access token refreshed; valid for %ss", expires_in)
            return token


_token_cache = _TokenCache()


def _fetch_oauth_token(token_url: str, client_id: str, client_secret: str, timeout: float) -> tuple[str, int]:
    """Exchange client_id/client_secret for a Bearer token (client_credentials grant).

    Verified against a live GreenLake SSO endpoint (2026-09): POST form-encoded
    (not JSON) to `token_url`, response is `{"token_type", "expires_in",
    "access_token"}`. `expires_in` was ~7199s (~2h) for a COM service client —
    treat that as typical, not a documented guarantee.
    """
    r = httpx.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=timeout,
    )
    if r.is_error:
        detail = ""
        try:
            body = r.json()
            detail = str(body.get("error_description") or body.get("error") or body)
        except ValueError:
            detail = r.text[:500]
        raise ComApiError(
            f"COM SSO token request failed: HTTP {r.status_code} - {detail} "
            "(check COM_CLIENT_ID/COM_CLIENT_SECRET)"
        )
    body = r.json()
    token = body.get("access_token")
    if not token:
        raise ComApiError("COM SSO token response had no access_token")
    return token, int(body.get("expires_in") or 3600)


def _check(r: httpx.Response, what: str) -> None:
    """Raise on a failed COM call, keeping the body's explanation.

    A bare status code doesn't say whether an expired PAT, a wrong tenant, or a
    bad server id caused a 4xx — surface the body the same way the adapters'
    ``_check()`` helpers already do.
    """
    if not r.is_error:
        return
    detail = ""
    try:
        body = r.json()
        detail = str(body.get("message") or body.get("error_description") or body)
    except ValueError:
        detail = r.text[:500]
    hint = ""
    if r.status_code in (401, 403):
        hint = " (check credentials are valid, unexpired, and scoped for this tenant)"
    raise ComApiError(f"COM {what} failed: HTTP {r.status_code}{hint} - {detail}")


class ComClient:
    """Minimal authenticated GET client for the COM REST API."""

    def __init__(self) -> None:
        base = os.environ.get("COM_BASE_URL")
        if not base:
            raise RuntimeError(
                "This feature needs COM_BASE_URL — the COM API base URL for your "
                "region/environment (see your GreenLake workspace's API "
                "reference), e.g. https://eu-central.api.greenlake.hpe.com."
            )
        self._base = base.rstrip("/")

        token = self._resolve_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        # MSP/multi-tenant callers select the tenant to act on. The header name
        # a given GreenLake gateway expects for this varies by deployment —
        # adjust below if yours differs from this default.
        tenant = os.environ.get("COM_TENANT_ACID")
        if tenant:
            headers["GLP-Tenant-Id"] = tenant

        self._timeout = float(os.environ.get("COM_TIMEOUT", "15"))
        self._client = httpx.Client(headers=headers, timeout=self._timeout)

    def _resolve_token(self) -> str:
        """Client credentials first (auto-refreshing); a static PAT otherwise."""
        client_id = os.environ.get("COM_CLIENT_ID")
        client_secret = get_secret("COM_CLIENT_SECRET", required=False)
        if client_id and client_secret:
            token_url = os.environ.get("COM_SSO_TOKEN_URL", _DEFAULT_SSO_TOKEN_URL)
            timeout = float(os.environ.get("COM_TIMEOUT", "15"))
            return _token_cache.get(
                lambda: _fetch_oauth_token(token_url, client_id, client_secret, timeout)
            )

        token = get_secret("COM_PAT", required=False)
        if token:
            return token

        raise RuntimeError(
            "This feature needs COM credentials: set COM_CLIENT_ID + "
            "COM_CLIENT_SECRET (preferred — auto-refreshing, from a GreenLake "
            "service client) or a static COM_PAT/COM_PAT_FILE (a Personal "
            "Access Token, which expires in ~2h with nothing to refresh it)."
        )

    def close(self) -> None:
        self._client.close()

    def get(self, path_or_url: str, params: dict | None = None) -> dict:
        """GET a COM resource, given either an absolute URI or a `/`-prefixed path.

        Bundle/advisory references from COM sometimes travel as full URIs and
        sometimes as bare paths — accept both so callers don't need to know
        which form a given field uses. Both forms can originate from a raw COM
        webhook payload (e.g. an alert's ``device.resourceUri`` or a server's
        ``firmwareBundleUri``), so an absolute URL is only followed when its
        host matches ``COM_BASE_URL`` — otherwise this would send the COM
        Bearer token to whatever host a malformed or malicious payload named.
        """
        parsed = urlparse(path_or_url)
        if parsed.scheme:
            base_host = urlparse(self._base).netloc
            if parsed.netloc != base_host:
                raise ComApiError(
                    f"refusing to GET {path_or_url}: host does not match "
                    f"COM_BASE_URL ({base_host}); this credential is only sent "
                    "to the configured COM API"
                )
            url = path_or_url
        else:
            url = f"{self._base}{path_or_url}"
        r = self._client.get(url, params=params)
        _check(r, f"GET {path_or_url}")
        return r.json()

    def get_server(self, server_id: str, select: str | None = None) -> dict:
        params = {"select": select} if select else None
        return self.get(f"/compute-ops-mgmt/v1/servers/{server_id}", params=params)

    def get_ui_doorway_server(self, server_id: str) -> dict:
        """Fetch the server-level compliance report used by the GreenLake UI.

        This is intentionally isolated from the public COM methods because it
        is a UI-doorway fallback, not the primary API contract. Its exception
        type and message identify that boundary explicitly when the endpoint
        changes or disappears, while callers can still fail open.
        """
        path = f"/api/ui-doorway/compute/v2/servers/{quote(server_id, safe='')}"
        url = f"{self._base}{path}"
        try:
            response = self._client.get(url)
            _check(response, f"UI-doorway GET {path}")
            body = response.json()
        except ComApiError as exc:
            raise ComUiDoorwayError(
                f"COM UI-doorway compliance fallback failed for {path}: {exc}"
            ) from exc
        except (ValueError, TypeError) as exc:
            raise ComUiDoorwayError(
                f"COM UI-doorway compliance fallback returned invalid JSON for {path}: {exc}"
            ) from exc
        if not isinstance(body, dict):
            raise ComUiDoorwayError(
                f"COM UI-doorway compliance fallback returned {type(body).__name__}, "
                f"expected an object for {path}"
            )
        return body

    def get_firmware_bundle(self, bundle_ref: str) -> dict:
        """Fetch a firmware bundle by id, bare path, or full resource URI.

        Verified against a live GreenLake account (2026-09): a server's own
        `firmwareBundleUri`/`lastFirmwareUpdate.attemptedBaselineUri` values are
        root-relative but **omit** the `/compute-ops-mgmt` service-mount segment
        that every other COM path (including the bundle's *own* `resourceUri`)
        carries — e.g. `/v1/firmware-bundles/<id>`, which 404s as-is
        (`HPE_GL_ERROR_NOT_FOUND`) and only resolves once that segment is added
        back: `/compute-ops-mgmt/v1/firmware-bundles/<id>`. So a root-relative
        path missing that segment is patched here rather than trusted verbatim.
        """
        if urlparse(bundle_ref).scheme:
            return self.get(bundle_ref)
        if bundle_ref.startswith("/"):
            if not bundle_ref.startswith("/compute-ops-mgmt/"):
                bundle_ref = f"/compute-ops-mgmt{bundle_ref}"
            return self.get(bundle_ref)
        return self.get(f"/compute-ops-mgmt/v1/firmware-bundles/{bundle_ref}")

    def list_groups(self, *, limit: int = 100, offset: int = 0) -> dict:
        """List device groups (a page of them; use `offset` to page further)."""
        return self.get(
            "/compute-ops-mgmt/v1/groups", params={"limit": limit, "offset": offset}
        )

    def get_group_compliance(self, group_id: str, *, limit: int = 100, offset: int = 0) -> dict:
        """Per-device firmware compliance for a group's assigned baseline.

        One record per device: `deviceId`, `bundleId` (the group's assigned
        baseline), `complianceState`, `score` (0-100), and `deviations`
        (component-level expected-vs-installed version mismatches). Verified
        against a live GreenLake account (2026-09) — this is the documented,
        supported way to learn whether a device has actually had a group's
        assigned baseline applied, as opposed to a device's own
        `firmwareBundleUri`/`lastFirmwareUpdate`, which only reflects a direct,
        one-off update and says nothing about a *group* baseline assigned to it.
        """
        return self.get(
            f"/compute-ops-mgmt/v1/groups/{group_id}/compliance",
            params={"limit": limit, "offset": offset},
        )

    def __enter__(self) -> "ComClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
