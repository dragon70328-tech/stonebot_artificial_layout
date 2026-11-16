"""Local online-license client and server-authoritative trial tests."""

from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import src.license_client as client_module
from src.license_client import (
    LicenseClientError,
    LicenseServiceClient,
    LocalSessionStore,
)
from src.licensing import (
    LicenseGuard,
    LicenseRequiredError,
    LocalEntitlementStore,
)


class _FakeResponse:
    def __init__(self, payload: dict, url: str = "https://license.example.com"):
        self._body = json.dumps(payload).encode("utf-8")
        self._url = url
        self.status = 200

    def read(self, _limit: int) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None


def test_license_client_login_and_authenticated_usage(tmp_path, monkeypatch):
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        if request.full_url.endswith("/v1/auth/verify-code"):
            return _FakeResponse(
                {
                    "ok": True,
                    "account_id": "account-1",
                    "device_id": "device-1",
                    "session_token": "session-token-1234567890",
                    "session_expires_in": 3600,
                    "trial_remaining": 3,
                }
            )
        if request.full_url.endswith("/v1/usage/reserve"):
            return _FakeResponse(
                {
                    "ok": True,
                    "allowed": True,
                    "authorization_id": "use-1",
                    "trial_remaining": 2,
                }
            )
        return _FakeResponse({"ok": True, "committed": True})

    monkeypatch.setattr(client_module, "urlopen", fake_urlopen)
    store = LocalSessionStore(tmp_path / "license" / "session.json")
    client = LicenseServiceClient("https://license.example.com", store)

    login = client.verify_code(
        identity_type="email",
        destination="user@example.com",
        code="123456",
        installation_id="device-1",
    )
    assert login["trial_remaining"] == 3
    assert client.current_session().account_id == "account-1"
    session_text = store.path.read_text(encoding="utf-8")
    assert "session-token-1234567890" not in session_text

    reserved = client.reserve_use(job_id="job-1", input_sha256="a" * 64)
    assert reserved["authorization_id"] == "use-1"
    assert requests[-1].get_header("Authorization") == (
        "Bearer session-token-1234567890"
    )
    committed = client.commit_use("use-1")
    assert committed["committed"] is True


def test_license_client_rejects_insecure_non_loopback_url(tmp_path):
    with pytest.raises(ValueError, match="HTTPS or loopback"):
        LicenseServiceClient(
            "http://license.example.com",
            LocalSessionStore(tmp_path / "session.json"),
        )


def test_online_guard_reserves_and_commits_trial_use(tmp_path):
    class FakeClient:
        def __init__(self):
            self.committed = []
            self.released = []

        def reserve_use(self, *, job_id, input_sha256):
            assert job_id == "job-1"
            assert input_sha256 == "b" * 64
            return {
                "ok": True,
                "allowed": True,
                "authorization_id": "usage-1",
                "trial_remaining": 2,
            }

        def commit_use(self, authorization_id):
            self.committed.append(authorization_id)
            return {"ok": True}

        def release_use(self, authorization_id):
            self.released.append(authorization_id)
            return {"ok": True}

    fake_client = FakeClient()
    guard = LicenseGuard(
        entitlement_store=LocalEntitlementStore(tmp_path / "entitlement.json"),
        public_key=Ed25519PrivateKey.generate().public_key(),
        device_id="device-1",
        service_client=fake_client,
    )
    authorization = guard.begin_production(
        job_id="job-1",
        input_sha256="b" * 64,
    )
    assert authorization.authorization_id == "usage-1"
    guard.commit_production(authorization)
    assert fake_client.committed == ["usage-1"]

    second = guard.begin_production(
        job_id="job-1",
        input_sha256="b" * 64,
    )
    guard.release_production(second)
    assert fake_client.released == ["usage-1"]


def test_online_guard_maps_service_denial_to_license_required(tmp_path):
    class DeniedClient:
        def reserve_use(self, *, job_id, input_sha256):
            raise LicenseClientError(
                "TRIAL_EXHAUSTED",
                "免费试用次数已用完",
                status_code=403,
            )

    guard = LicenseGuard(
        entitlement_store=LocalEntitlementStore(tmp_path / "entitlement.json"),
        public_key=Ed25519PrivateKey.generate().public_key(),
        device_id="device-1",
        service_client=DeniedClient(),
    )

    with pytest.raises(LicenseRequiredError) as exc:
        guard.begin_production(job_id="job-1")
    assert exc.value.status.state == "blocked"
    assert "TRIAL_EXHAUSTED" in str(exc.value.status.reason)
