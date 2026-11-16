"""Local entitlement, device identity and production-access checks."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.local_runtime import LocalRuntimeLayout


LICENSE_SCHEMA_VERSION = 1
SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class LicensingError(RuntimeError):
    """Base class for local licensing failures."""


class LicenseStoreError(LicensingError):
    """Raised when persisted license data is missing or invalid."""


class LicenseVerificationError(LicensingError):
    """Raised when an entitlement signature or device binding is invalid."""


class LicenseRequiredError(LicensingError):
    """Raised when production access requires a valid entitlement."""

    def __init__(self, status: "LicenseStatus") -> None:
        super().__init__(status.reason or "valid license is required")
        self.status = status


class LicenseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeviceIdentity(LicenseModel):
    schema_version: Literal[1] = 1
    installation_id: str
    display_name: str | None = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @model_validator(mode="after")
    def validate_identity(self) -> "DeviceIdentity":
        if not SAFE_ID.fullmatch(self.installation_id):
            raise ValueError("invalid installation_id")
        return self


class EntitlementToken(LicenseModel):
    schema_version: Literal[1] = 1
    account_id: str
    device_id: str
    plan: Literal["trial", "monthly", "yearly"]
    entitlements: list[str] = Field(
        default_factory=lambda: ["all_features"]
    )
    trial_remaining: int | None = Field(default=None, ge=0)
    issued_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    not_before: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    expires_at: datetime
    grace_until: datetime | None = None
    license_id: str
    nonce: str
    key_id: str
    signature_algorithm: Literal["ed25519"] = "ed25519"
    signature: str | None = None

    @model_validator(mode="after")
    def validate_token(self) -> "EntitlementToken":
        for value, name in (
            (self.account_id, "account_id"),
            (self.device_id, "device_id"),
            (self.license_id, "license_id"),
            (self.nonce, "nonce"),
            (self.key_id, "key_id"),
        ):
            if not SAFE_ID.fullmatch(value):
                raise ValueError(f"invalid {name}")
        if self.grace_until and self.grace_until < self.expires_at:
            raise ValueError("grace_until cannot be before expires_at")
        for value in (
            self.issued_at,
            self.not_before,
            self.expires_at,
            self.grace_until,
        ):
            if value is not None and value.tzinfo is None:
                raise ValueError("entitlement timestamps must include timezone")
        if self.plan == "trial" and self.trial_remaining is None:
            raise ValueError("trial entitlement requires trial_remaining")
        return self

    def signing_payload(self) -> bytes:
        return _canonical_json(
            self.model_dump(
                mode="json",
                exclude={"signature"},
                exclude_none=True,
            )
        )

    def digest(self) -> str:
        return hashlib.sha256(self.signing_payload()).hexdigest()

    def sign(self, private_key: Ed25519PrivateKey) -> "EntitlementToken":
        self.signature = base64.b64encode(
            private_key.sign(self.signing_payload())
        ).decode("ascii")
        return self


@dataclass(frozen=True)
class LicenseStatus:
    state: str
    valid: bool
    production_allowed: bool
    reason: str | None = None
    account_id: str | None = None
    device_id: str | None = None
    plan: str | None = None
    expires_at: datetime | None = None
    grace_until: datetime | None = None
    trial_remaining: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "valid": self.valid,
            "production_allowed": self.production_allowed,
            "reason": self.reason,
            "account_id": self.account_id,
            "device_id": self.device_id,
            "plan": self.plan,
            "expires_at": (
                self.expires_at.isoformat()
                if self.expires_at is not None
                else None
            ),
            "grace_until": (
                self.grace_until.isoformat()
                if self.grace_until is not None
                else None
            ),
            "trial_remaining": self.trial_remaining,
        }


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError:
            pass


def public_key_from_base64(value: str) -> Ed25519PublicKey:
    try:
        raw = base64.b64decode(value, validate=True)
        return Ed25519PublicKey.from_public_bytes(raw)
    except Exception as exc:
        raise LicenseVerificationError("invalid Ed25519 public key") from exc


def public_key_base64(key: Ed25519PublicKey) -> str:
    raw = key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def verify_entitlement(
    token: EntitlementToken,
    public_key: Ed25519PublicKey,
    *,
    expected_device_id: str,
    now: datetime | None = None,
) -> LicenseStatus:
    if not token.signature:
        return LicenseStatus(
            state="invalid",
            valid=False,
            production_allowed=False,
            reason="entitlement is not signed",
            account_id=token.account_id,
            device_id=token.device_id,
            plan=token.plan,
        )
    if token.device_id != expected_device_id:
        return LicenseStatus(
            state="invalid",
            valid=False,
            production_allowed=False,
            reason="entitlement is bound to another device",
            account_id=token.account_id,
            device_id=token.device_id,
            plan=token.plan,
        )
    try:
        signature = base64.b64decode(token.signature, validate=True)
        public_key.verify(signature, token.signing_payload())
    except (ValueError, InvalidSignature):
        return LicenseStatus(
            state="invalid",
            valid=False,
            production_allowed=False,
            reason="entitlement signature verification failed",
            account_id=token.account_id,
            device_id=token.device_id,
            plan=token.plan,
        )

    current = now or datetime.now(timezone.utc)
    if current < token.not_before:
        state = "not_yet_valid"
        valid = False
        production_allowed = False
        reason = "entitlement is not active yet"
    elif current <= token.expires_at:
        state = (
            "trial_exhausted"
            if token.plan == "trial" and token.trial_remaining == 0
            else "active"
        )
        valid = state == "active"
        production_allowed = state == "active"
        reason = (
            "free trial is exhausted"
            if state == "trial_exhausted"
            else None
        )
    elif token.grace_until and current <= token.grace_until:
        state = "grace"
        valid = True
        production_allowed = False
        reason = "subscription expired; offline grace is read-only"
    else:
        state = "expired"
        valid = False
        production_allowed = False
        reason = "subscription has expired"
    return LicenseStatus(
        state=state,
        valid=valid,
        production_allowed=production_allowed,
        reason=reason,
        account_id=token.account_id,
        device_id=token.device_id,
        plan=token.plan,
        expires_at=token.expires_at,
        grace_until=token.grace_until,
        trial_remaining=token.trial_remaining,
    )


class DeviceIdentityStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load_or_create(
        self,
        *,
        display_name: str | None = None,
    ) -> DeviceIdentity:
        if self.path.exists():
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                return DeviceIdentity.model_validate(payload)
            except Exception as exc:
                raise LicenseStoreError(
                    f"device identity is invalid: {self.path}"
                ) from exc
        identity = DeviceIdentity(
            installation_id=secrets.token_hex(16),
            display_name=display_name,
        )
        _atomic_json(self.path, identity.model_dump(mode="json"))
        return identity


class LocalEntitlementStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def save(self, token: EntitlementToken) -> Path:
        _atomic_json(self.path, token.model_dump(mode="json"))
        return self.path

    def load(self) -> EntitlementToken:
        if not self.path.exists():
            raise LicenseStoreError("entitlement is missing")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return EntitlementToken.model_validate(payload)
        except Exception as exc:
            raise LicenseStoreError("entitlement is invalid") from exc

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class LicenseGuard:
    """Evaluate a locally stored signed entitlement before production writes."""

    def __init__(
        self,
        *,
        entitlement_store: LocalEntitlementStore,
        public_key: Ed25519PublicKey,
        device_id: str,
        enforcement: bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.entitlement_store = entitlement_store
        self.public_key = public_key
        self.device_id = device_id
        self.enforcement = enforcement
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def status(self) -> LicenseStatus:
        if not self.enforcement:
            return LicenseStatus(
                state="disabled",
                valid=True,
                production_allowed=True,
                device_id=self.device_id,
                reason="license enforcement is disabled for development",
            )
        try:
            token = self.entitlement_store.load()
        except LicenseStoreError as exc:
            return LicenseStatus(
                state="missing",
                valid=False,
                production_allowed=False,
                reason=str(exc),
                device_id=self.device_id,
            )
        return verify_entitlement(
            token,
            self.public_key,
            expected_device_id=self.device_id,
            now=self.clock(),
        )

    def require_production_access(self) -> LicenseStatus:
        status = self.status()
        if not status.production_allowed:
            raise LicenseRequiredError(status)
        return status


def build_license_guard(
    layout: LocalRuntimeLayout,
    public_key_text: str,
    *,
    enforcement: bool = True,
    display_name: str | None = None,
) -> LicenseGuard:
    identity = DeviceIdentityStore(
        layout.device_identity_path
    ).load_or_create(display_name=display_name)
    return LicenseGuard(
        entitlement_store=LocalEntitlementStore(layout.entitlement_path),
        public_key=public_key_from_base64(public_key_text),
        device_id=identity.installation_id,
        enforcement=enforcement,
    )
