"""Local entitlement, device identity and production gate tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from src.local_runtime import LocalRuntimeLayout
from src.licensing import (
    DeviceIdentityStore,
    EntitlementToken,
    LicenseGuard,
    LicenseRequiredError,
    LocalEntitlementStore,
    verify_entitlement,
)
from src.plugin_service import PluginJobService, StonebotProtocolError
from src.workflow_session import WorkflowStage


def _token(
    *,
    device_id: str,
    now,
    plan="monthly",
    trial_remaining=None,
    expires_delta=timedelta(days=30),
    grace_delta=timedelta(days=7),
):
    return EntitlementToken(
        account_id="account-1",
        device_id=device_id,
        plan=plan,
        trial_remaining=trial_remaining,
        issued_at=now,
        not_before=now,
        expires_at=now + expires_delta,
        grace_until=now + expires_delta + grace_delta,
        license_id="license-1",
        nonce="nonce-1",
        key_id="license-key-1",
    )


def test_device_identity_is_stable_and_entitlement_status_tracks_time(tmp_path):
    layout = LocalRuntimeLayout.resolve(tmp_path / "runtime").ensure()
    identity_store = DeviceIdentityStore(layout.device_identity_path)
    identity = identity_store.load_or_create(display_name="Workstation")
    assert identity_store.load_or_create().installation_id == identity.installation_id

    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    now = datetime.now(timezone.utc)
    token = _token(device_id=identity.installation_id, now=now).sign(private)
    LocalEntitlementStore(layout.entitlement_path).save(token)

    guard = LicenseGuard(
        entitlement_store=LocalEntitlementStore(layout.entitlement_path),
        public_key=public,
        device_id=identity.installation_id,
        clock=lambda: now,
    )
    active = guard.require_production_access()
    assert active.state == "active"
    assert active.production_allowed is True

    grace_guard = LicenseGuard(
        entitlement_store=LocalEntitlementStore(layout.entitlement_path),
        public_key=public,
        device_id=identity.installation_id,
        clock=lambda: token.expires_at + timedelta(days=1),
    )
    grace = grace_guard.status()
    assert grace.state == "grace"
    assert grace.valid is True
    assert grace.production_allowed is False
    with pytest.raises(LicenseRequiredError):
        grace_guard.require_production_access()

    expired_guard = LicenseGuard(
        entitlement_store=LocalEntitlementStore(layout.entitlement_path),
        public_key=public,
        device_id=identity.installation_id,
        clock=lambda: token.grace_until + timedelta(seconds=1),
    )
    assert expired_guard.status().state == "expired"


def test_entitlement_rejects_tampering_and_device_mismatch(tmp_path):
    layout = LocalRuntimeLayout.resolve(tmp_path / "runtime").ensure()
    private = Ed25519PrivateKey.generate()
    now = datetime.now(timezone.utc)
    token = _token(
        device_id="device-a",
        now=now,
    ).sign(private)

    mismatch = verify_entitlement(
        token,
        private.public_key(),
        expected_device_id="device-b",
    )
    assert mismatch.state == "invalid"
    assert "another device" in str(mismatch.reason)

    token.plan = "yearly"
    tampered = verify_entitlement(
        token,
        private.public_key(),
        expected_device_id="device-a",
    )
    assert tampered.state == "invalid"
    assert "signature" in str(tampered.reason)


def test_trial_with_zero_remaining_is_exhausted():
    private = Ed25519PrivateKey.generate()
    now = datetime.now(timezone.utc)
    token = _token(
        device_id="device-a",
        now=now,
        plan="trial",
        trial_remaining=0,
    ).sign(private)
    status = verify_entitlement(
        token,
        private.public_key(),
        expected_device_id="device-a",
        now=now,
    )
    assert status.state == "trial_exhausted"
    assert status.production_allowed is False


def test_plugin_service_blocks_finalization_without_license(tmp_path):
    source = tmp_path / "source.dxf"
    source.write_bytes(b"dxf")
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    private = Ed25519PrivateKey.generate()
    layout = LocalRuntimeLayout.resolve(tmp_path / "runtime").ensure()
    guard = LicenseGuard(
        entitlement_store=LocalEntitlementStore(layout.entitlement_path),
        public_key=private.public_key(),
        device_id="device-a",
    )
    service = PluginJobService(tmp_path / "jobs", license_guard=guard)
    runtime = SimpleNamespace(
        session=SimpleNamespace(stage=WorkflowStage.POSTPROCESS_CONFIRMED),
        dxf_path=str(source),
        nested_path=str(source),
        context=SimpleNamespace(
            numbered_dxf_path=str(source),
            nested_dxf_path=str(source),
            parts=[],
        ),
    )
    manifest = {"delivery": None}
    service._runtime_and_manifest = lambda _job_id: (runtime, manifest)

    with pytest.raises(StonebotProtocolError) as exc:
        service.finalize_job(
            {
                "job_id": "job-1",
                "decision": "confirm",
                "output_directory": str(delivery),
            }
        )

    assert exc.value.code == "LICENSE_REQUIRED"
    assert exc.value.details["state"] == "missing"
