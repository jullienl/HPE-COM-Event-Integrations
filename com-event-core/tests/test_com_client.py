"""Tests for `ComClient`'s authentication: client_credentials token caching/
refresh, static PAT fallback, and the firmware-bundle path-prefix fix.

No real network calls: the SSO token endpoint and COM API are mocked.
"""

from __future__ import annotations

import pytest

from com_event_core import com_client as com_client_mod
from com_event_core.com_client import ComApiError, ComClient, _TokenCache


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "COM_BASE_URL", "COM_PAT", "COM_PAT_FILE",
        "COM_CLIENT_ID", "COM_CLIENT_SECRET", "COM_CLIENT_SECRET_FILE",
        "COM_SSO_TOKEN_URL", "COM_TENANT_ACID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COM_BASE_URL", "https://eu-central.api.greenlake.hpe.com")
    # Every test gets its own fresh token cache so refresh behaviour is isolated.
    monkeypatch.setattr(com_client_mod, "_token_cache", _TokenCache())


class TestStaticPatFallback:
    def test_uses_pat_when_no_client_credentials(self, monkeypatch):
        monkeypatch.setenv("COM_PAT", "static-token-123")
        client = ComClient()
        assert client._client.headers["Authorization"] == "Bearer static-token-123"
        client.close()

    def test_raises_helpful_error_with_no_credentials_at_all(self):
        with pytest.raises(RuntimeError, match="COM_CLIENT_ID.*COM_PAT|COM credentials"):
            ComClient()

    def test_raises_when_base_url_missing(self, monkeypatch):
        monkeypatch.delenv("COM_BASE_URL", raising=False)
        monkeypatch.setenv("COM_PAT", "x")
        with pytest.raises(RuntimeError, match="COM_BASE_URL"):
            ComClient()


class TestClientCredentials:
    def test_prefers_client_credentials_over_pat(self, monkeypatch):
        monkeypatch.setenv("COM_CLIENT_ID", "cid")
        monkeypatch.setenv("COM_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("COM_PAT", "should-not-be-used")

        calls = []

        def fake_fetch(token_url, client_id, client_secret, timeout):
            calls.append((token_url, client_id, client_secret))
            return "fresh-token", 7199

        monkeypatch.setattr(com_client_mod, "_fetch_oauth_token", fake_fetch)
        client = ComClient()
        assert client._client.headers["Authorization"] == "Bearer fresh-token"
        assert len(calls) == 1
        assert calls[0][1] == "cid" and calls[0][2] == "csecret"
        client.close()

    def test_token_is_cached_across_instances(self, monkeypatch):
        monkeypatch.setenv("COM_CLIENT_ID", "cid")
        monkeypatch.setenv("COM_CLIENT_SECRET", "csecret")

        calls = {"n": 0}

        def fake_fetch(token_url, client_id, client_secret, timeout):
            calls["n"] += 1
            return f"token-{calls['n']}", 7199

        monkeypatch.setattr(com_client_mod, "_fetch_oauth_token", fake_fetch)

        c1 = ComClient()
        c2 = ComClient()
        assert c1._client.headers["Authorization"] == "Bearer token-1"
        assert c2._client.headers["Authorization"] == "Bearer token-1"  # reused
        assert calls["n"] == 1
        c1.close()
        c2.close()

    def test_expired_token_is_refreshed(self, monkeypatch):
        monkeypatch.setenv("COM_CLIENT_ID", "cid")
        monkeypatch.setenv("COM_CLIENT_SECRET", "csecret")

        calls = {"n": 0}

        def fake_fetch(token_url, client_id, client_secret, timeout):
            calls["n"] += 1
            # Expire (almost) immediately so the next get() call must refetch.
            return f"token-{calls['n']}", 0

        monkeypatch.setattr(com_client_mod, "_fetch_oauth_token", fake_fetch)

        c1 = ComClient()
        c2 = ComClient()
        assert calls["n"] == 2
        assert c1._client.headers["Authorization"] == "Bearer token-1"
        assert c2._client.headers["Authorization"] == "Bearer token-2"
        c1.close()
        c2.close()

    def test_default_sso_token_url_used(self, monkeypatch):
        monkeypatch.setenv("COM_CLIENT_ID", "cid")
        monkeypatch.setenv("COM_CLIENT_SECRET", "csecret")

        seen = {}

        def fake_fetch(token_url, client_id, client_secret, timeout):
            seen["url"] = token_url
            return "t", 7199

        monkeypatch.setattr(com_client_mod, "_fetch_oauth_token", fake_fetch)
        ComClient().close()
        assert seen["url"] == "https://sso.common.cloud.hpe.com/as/token.oauth2"

    def test_sso_token_url_override(self, monkeypatch):
        monkeypatch.setenv("COM_CLIENT_ID", "cid")
        monkeypatch.setenv("COM_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("COM_SSO_TOKEN_URL", "https://custom.sso.example/token")

        seen = {}

        def fake_fetch(token_url, client_id, client_secret, timeout):
            seen["url"] = token_url
            return "t", 7199

        monkeypatch.setattr(com_client_mod, "_fetch_oauth_token", fake_fetch)
        ComClient().close()
        assert seen["url"] == "https://custom.sso.example/token"

    def test_oauth_error_response_raises_com_api_error(self, monkeypatch):
        class FakeResponse:
            is_error = True
            status_code = 401

            def json(self):
                return {"error": "invalid_client", "error_description": "bad secret"}

        def fake_post(url, data=None, headers=None, timeout=None):
            return FakeResponse()

        monkeypatch.setattr(com_client_mod.httpx, "post", fake_post)
        with pytest.raises(ComApiError, match="bad secret"):
            com_client_mod._fetch_oauth_token("https://sso.example/token", "cid", "sec", 15)


class TestFirmwareBundlePathFix:
    def test_root_relative_path_missing_prefix_is_patched(self, monkeypatch):
        monkeypatch.setenv("COM_PAT", "t")
        client = ComClient()
        seen = {}

        def fake_get(path_or_url, params=None):
            seen["path"] = path_or_url
            return {}

        monkeypatch.setattr(client, "get", fake_get)
        client.get_firmware_bundle("/v1/firmware-bundles/abc123")
        assert seen["path"] == "/compute-ops-mgmt/v1/firmware-bundles/abc123"
        client.close()

    def test_path_already_prefixed_is_untouched(self, monkeypatch):
        monkeypatch.setenv("COM_PAT", "t")
        client = ComClient()
        seen = {}

        def fake_get(path_or_url, params=None):
            seen["path"] = path_or_url
            return {}

        monkeypatch.setattr(client, "get", fake_get)
        client.get_firmware_bundle("/compute-ops-mgmt/v1/firmware-bundles/abc123")
        assert seen["path"] == "/compute-ops-mgmt/v1/firmware-bundles/abc123"
        client.close()

    def test_absolute_url_is_untouched(self, monkeypatch):
        monkeypatch.setenv("COM_PAT", "t")
        client = ComClient()
        seen = {}

        def fake_get(path_or_url, params=None):
            seen["path"] = path_or_url
            return {}

        monkeypatch.setattr(client, "get", fake_get)
        client.get_firmware_bundle("https://eu-central.api.greenlake.hpe.com/compute-ops-mgmt/v1/firmware-bundles/abc123")
        assert seen["path"].startswith("https://")
        client.close()

    def test_bare_id_gets_full_path_constructed(self, monkeypatch):
        monkeypatch.setenv("COM_PAT", "t")
        client = ComClient()
        seen = {}

        def fake_get(path_or_url, params=None):
            seen["path"] = path_or_url
            return {}

        monkeypatch.setattr(client, "get", fake_get)
        client.get_firmware_bundle("abc123")
        assert seen["path"] == "/compute-ops-mgmt/v1/firmware-bundles/abc123"
        client.close()


class TestGetHostGuard:
    """`get()` must never send the COM Bearer token to a host other than
    COM_BASE_URL — a raw COM webhook payload can carry an absolute URL
    (e.g. an alert's device.resourceUri) and must not be trusted blindly."""

    def test_relative_path_is_sent_to_configured_base(self, monkeypatch):
        monkeypatch.setenv("COM_BASE_URL", "https://eu-central.api.greenlake.hpe.com")
        monkeypatch.setenv("COM_PAT", "t")
        client = ComClient()
        seen = {}

        def fake_get(url, params=None):
            seen["url"] = url
            class _Resp:
                is_error = False
                def json(self):
                    return {}
            return _Resp()

        monkeypatch.setattr(client._client, "get", fake_get)
        client.get("/compute-ops-mgmt/v1/servers/abc")
        assert seen["url"] == "https://eu-central.api.greenlake.hpe.com/compute-ops-mgmt/v1/servers/abc"
        client.close()

    def test_absolute_url_matching_base_host_is_allowed(self, monkeypatch):
        monkeypatch.setenv("COM_BASE_URL", "https://eu-central.api.greenlake.hpe.com")
        monkeypatch.setenv("COM_PAT", "t")
        client = ComClient()
        seen = {}

        def fake_get(url, params=None):
            seen["url"] = url
            class _Resp:
                is_error = False
                def json(self):
                    return {}
            return _Resp()

        monkeypatch.setattr(client._client, "get", fake_get)
        client.get("https://eu-central.api.greenlake.hpe.com/compute-ops-mgmt/v1/servers/abc")
        assert seen["url"] == "https://eu-central.api.greenlake.hpe.com/compute-ops-mgmt/v1/servers/abc"
        client.close()

    def test_absolute_url_to_a_different_host_is_refused(self, monkeypatch):
        monkeypatch.setenv("COM_BASE_URL", "https://eu-central.api.greenlake.hpe.com")
        monkeypatch.setenv("COM_PAT", "t")
        client = ComClient()

        def fake_get(url, params=None):
            raise AssertionError("must never send a request to an unverified host")

        monkeypatch.setattr(client._client, "get", fake_get)
        with pytest.raises(ComApiError, match="does not match COM_BASE_URL"):
            client.get("https://attacker.example/steal-token")
        client.close()
