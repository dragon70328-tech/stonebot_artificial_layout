"""SQLite-backed account and licensing service for StoneBot Local.

This is the control-plane foundation. It never receives drawings or nesting
geometry. Email/SMS delivery is injected so development can use a captured
sender while production supplies a real provider.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from src.licensing import EntitlementToken


EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_CLEAN = re.compile(r"[^0-9+]")
MAX_TRIAL_USES = 3
CODE_TTL_SECONDS = 10 * 60
CODE_COOLDOWN_SECONDS = 60
CODE_MAX_ATTEMPTS = 5
USAGE_RESERVATION_SECONDS = 15 * 60


class LicenseApiError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class CodeSender(Protocol):
    def send_email(
        self,
        destination: str,
        code: str,
        purpose: str,
    ) -> None: ...

    def send_sms(
        self,
        destination: str,
        code: str,
        purpose: str,
    ) -> None: ...


@dataclass
class CapturedMessage:
    channel: str
    destination: str
    code: str
    purpose: str
    sent_at: float


class CapturedCodeSender:
    """Development sender; production must inject a real email/SMS adapter."""

    def __init__(self) -> None:
        self.messages: list[CapturedMessage] = []
        self._lock = threading.Lock()

    def send_email(self, destination: str, code: str, purpose: str) -> None:
        self._capture("email", destination, code, purpose)

    def send_sms(self, destination: str, code: str, purpose: str) -> None:
        self._capture("sms", destination, code, purpose)

    def _capture(
        self,
        channel: str,
        destination: str,
        code: str,
        purpose: str,
    ) -> None:
        with self._lock:
            self.messages.append(
                CapturedMessage(
                    channel=channel,
                    destination=destination,
                    code=code,
                    purpose=purpose,
                    sent_at=time.time(),
                )
            )

    def latest_code(
        self,
        *,
        destination: str,
        purpose: str,
    ) -> str | None:
        with self._lock:
            for message in reversed(self.messages):
                if (
                    message.destination == destination
                    and message.purpose == purpose
                ):
                    return message.code
        return None


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now_ts() -> float:
    return time.time()


def _dt_from_ts(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _safe_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(12)}"


def _normalize_identity(
    identity_type: Literal["email", "phone"],
    destination: str,
) -> str:
    value = destination.strip()
    if identity_type == "email":
        value = value.casefold()
        if not EMAIL_PATTERN.fullmatch(value):
            raise LicenseApiError(400, "INVALID_EMAIL", "邮箱格式不正确")
        return value
    value = PHONE_CLEAN.sub("", value)
    if len(value) < 6 or len(value) > 20:
        raise LicenseApiError(400, "INVALID_PHONE", "手机号格式不正确")
    return value


class LicenseService:
    """Transactional account, trial, order and entitlement service."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        private_key: Ed25519PrivateKey,
        key_id: str,
        sender: CodeSender,
        code_pepper: str,
        max_devices_per_account: int = 2,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.private_key = private_key
        self.key_id = key_id
        self.sender = sender
        self.code_pepper = code_pepper.encode("utf-8")
        self.max_devices_per_account = max(1, int(max_devices_per_account))
        self._lock = threading.RLock()
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        with self._lock, self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id TEXT PRIMARY KEY,
                    email TEXT UNIQUE,
                    phone TEXT UNIQUE,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS verification_codes (
                    code_id TEXT PRIMARY KEY,
                    identity_type TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    code_hash TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    consumed_at REAL
                );

                CREATE TABLE IF NOT EXISTS devices (
                    account_id TEXT NOT NULL,
                    installation_id TEXT NOT NULL,
                    display_name TEXT,
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    revoked_at REAL,
                    PRIMARY KEY(account_id, installation_id)
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    installation_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    revoked_at REAL
                );

                CREATE TABLE IF NOT EXISTS trial_usages (
                    usage_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    input_sha256 TEXT,
                    status TEXT NOT NULL,
                    reserved_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    committed_at REAL,
                    UNIQUE(account_id, job_id)
                );

                CREATE TABLE IF NOT EXISTS orders (
                    order_no TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    plan TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    payment_reference TEXT UNIQUE,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    paid_at REAL,
                    confirmed_by TEXT
                );

                CREATE TABLE IF NOT EXISTS activation_codes (
                    activation_id TEXT PRIMARY KEY,
                    order_no TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    code_hash TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    consumed_at REAL
                );

                CREATE TABLE IF NOT EXISTS entitlements (
                    license_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    plan TEXT NOT NULL,
                    issued_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    grace_until REAL NOT NULL,
                    token_json TEXT NOT NULL,
                    order_no TEXT,
                    revoked_at REAL
                );

                CREATE TABLE IF NOT EXISTS admin_audit (
                    audit_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )
            columns = {
                row["name"]
                for row in self.conn.execute(
                    "PRAGMA table_info(entitlements)"
                ).fetchall()
            }
            if "order_no" not in columns:
                self.conn.execute(
                    "ALTER TABLE entitlements ADD COLUMN order_no TEXT"
                )

    def request_code(
        self,
        *,
        identity_type: Literal["email", "phone"],
        destination: str,
        purpose: str = "login",
    ) -> dict:
        destination = _normalize_identity(identity_type, destination)
        now = _now_ts()
        with self._lock, self.conn:
            latest = self.conn.execute(
                """
                SELECT created_at FROM verification_codes
                WHERE identity_type=? AND destination=? AND purpose=?
                ORDER BY created_at DESC LIMIT 1
                """,
                (identity_type, destination, purpose),
            ).fetchone()
            if latest and now - float(latest["created_at"]) < CODE_COOLDOWN_SECONDS:
                raise LicenseApiError(
                    429,
                    "CODE_RATE_LIMITED",
                    "验证码发送过于频繁，请稍后再试",
                )
            code = f"{secrets.randbelow(1_000_000):06d}"
            code_id = _safe_id("code")
            self.conn.execute(
                """
                INSERT INTO verification_codes (
                    code_id, identity_type, destination, purpose,
                    code_hash, attempts, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    code_id,
                    identity_type,
                    destination,
                    purpose,
                    self._hash_code(destination, purpose, code),
                    now,
                    now + CODE_TTL_SECONDS,
                ),
            )
            try:
                if identity_type == "email":
                    self.sender.send_email(destination, code, purpose)
                else:
                    self.sender.send_sms(destination, code, purpose)
            except Exception as exc:
                self.conn.execute(
                    "DELETE FROM verification_codes WHERE code_id=?",
                    (code_id,),
                )
                raise LicenseApiError(
                    503,
                    "CODE_DELIVERY_FAILED",
                    "验证码发送失败",
                ) from exc
        return {
            "ok": True,
            "channel": identity_type,
            "destination": _mask_destination(destination),
            "expires_in": CODE_TTL_SECONDS,
        }

    def verify_code(
        self,
        *,
        identity_type: Literal["email", "phone"],
        destination: str,
        code: str,
        installation_id: str,
        display_name: str | None = None,
        trial_days: int = 7,
    ) -> dict:
        destination = _normalize_identity(identity_type, destination)
        now = _now_ts()
        with self._lock:
            with self.conn:
                row = self.conn.execute(
                    """
                    SELECT * FROM verification_codes
                    WHERE identity_type=? AND destination=? AND purpose='login'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (identity_type, destination),
                ).fetchone()
                self._verify_code_row(row, destination, "login", code, now)
                self.conn.execute(
                    "UPDATE verification_codes SET consumed_at=? WHERE code_id=?",
                    (now, row["code_id"]),
                )
                account = self._find_or_create_account(
                    identity_type,
                    destination,
                    now,
                )
                self._bind_device(
                    account["account_id"],
                    installation_id,
                    display_name,
                    now,
                )
                session_token = self._issue_session(
                    account["account_id"],
                    installation_id,
                    now,
                )
                remaining = self._trial_remaining(
                    account["account_id"],
                    now,
                )
                paid = self._active_paid_entitlement(
                    account["account_id"],
                    now,
                )
                token = (
                    self._issue_paid_lease(
                        account["account_id"],
                        installation_id,
                        paid,
                        now,
                    )
                    if paid is not None
                    else self._issue_trial_entitlement(
                        account["account_id"],
                        installation_id,
                        remaining,
                        now,
                        lease_days=max(1, int(trial_days)),
                    )
                )
        return {
            "ok": True,
            "account_id": account["account_id"],
            "device_id": installation_id,
            "session_token": session_token,
            "session_expires_in": 30 * 24 * 60 * 60,
            "trial_remaining": remaining,
            "entitlement": token.model_dump(mode="json"),
        }

    def reserve_trial_use(
        self,
        *,
        account_id: str,
        device_id: str,
        job_id: str,
        input_sha256: str | None = None,
    ) -> dict:
        now = _now_ts()
        with self._lock:
            with self.conn:
                self._require_device(account_id, device_id, now)
                self._expire_reservations(now)
                paid = self._active_paid_entitlement(account_id, now)
                if paid is not None:
                    token = self._issue_paid_lease(
                        account_id,
                        device_id,
                        paid,
                        now,
                    )
                    return {
                        "ok": True,
                        "allowed": True,
                        "subscription": True,
                        "trial_remaining": self._trial_remaining(
                            account_id,
                            now,
                        ),
                        "entitlement": token.model_dump(mode="json"),
                    }
                existing = self.conn.execute(
                    """
                    SELECT * FROM trial_usages
                    WHERE account_id=? AND job_id=?
                    """,
                    (account_id, job_id),
                ).fetchone()
                if existing:
                    if existing["status"] == "committed":
                        remaining = self._trial_remaining(account_id, now)
                        token = self._issue_trial_entitlement(
                            account_id,
                            device_id,
                            remaining,
                            now,
                            lease_days=7,
                        )
                        return {
                            "ok": True,
                            "allowed": True,
                            "already_consumed": True,
                            "trial_remaining": remaining,
                            "entitlement": token.model_dump(mode="json"),
                        }
                    if (
                        existing["status"] == "reserved"
                        and float(existing["expires_at"]) >= now
                    ):
                        remaining = self._trial_remaining(account_id, now)
                        token = self._issue_trial_entitlement(
                            account_id,
                            device_id,
                            remaining,
                            now,
                            lease_days=7,
                        )
                        return {
                            "ok": True,
                            "allowed": True,
                            "authorization_id": existing["usage_id"],
                            "trial_remaining": remaining,
                            "entitlement": token.model_dump(mode="json"),
                        }
                used = self._count_trial_uses(account_id, now)
                if used >= MAX_TRIAL_USES:
                    raise LicenseApiError(
                        403,
                        "TRIAL_EXHAUSTED",
                        "免费试用次数已用完",
                    )
                usage_id = _safe_id("use")
                self.conn.execute(
                    """
                    INSERT INTO trial_usages (
                        usage_id, account_id, device_id, job_id,
                        input_sha256, status, reserved_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?)
                    """,
                    (
                        usage_id,
                        account_id,
                        device_id,
                        job_id,
                        input_sha256,
                        now,
                        now + USAGE_RESERVATION_SECONDS,
                    ),
                )
                remaining = MAX_TRIAL_USES - self._count_trial_uses(
                    account_id,
                    now,
                )
                token = self._issue_trial_entitlement(
                    account_id,
                    device_id,
                    remaining,
                    now,
                    lease_days=7,
                )
        return {
            "ok": True,
            "allowed": True,
            "authorization_id": usage_id,
            "trial_remaining": remaining,
            "entitlement": token.model_dump(mode="json"),
        }

    def commit_trial_use(
        self,
        *,
        account_id: str,
        device_id: str,
        authorization_id: str,
    ) -> dict:
        now = _now_ts()
        with self._lock, self.conn:
            self._require_device(account_id, device_id, now)
            row = self.conn.execute(
                "SELECT * FROM trial_usages WHERE usage_id=?",
                (authorization_id,),
            ).fetchone()
            if row is None or row["account_id"] != account_id:
                raise LicenseApiError(
                    404,
                    "AUTHORIZATION_NOT_FOUND",
                    "试用授权不存在",
                )
            if row["status"] == "reserved":
                self.conn.execute(
                    """
                    UPDATE trial_usages
                    SET status='committed', committed_at=?
                    WHERE usage_id=?
                    """,
                    (now, authorization_id),
                )
            remaining = MAX_TRIAL_USES - self._count_trial_uses(
                account_id,
                now,
            )
        return {
            "ok": True,
            "committed": True,
            "trial_remaining": max(0, remaining),
        }

    def release_trial_use(
        self,
        *,
        account_id: str,
        device_id: str,
        authorization_id: str,
    ) -> dict:
        now = _now_ts()
        with self._lock, self.conn:
            self._require_device(account_id, device_id, now)
            self.conn.execute(
                """
                DELETE FROM trial_usages
                WHERE usage_id=? AND account_id=? AND status='reserved'
                """,
                (authorization_id, account_id),
            )
        return {"ok": True, "released": True}

    def create_order(
        self,
        *,
        account_id: str,
        plan: Literal["monthly", "yearly"],
        amount_cents: int,
        currency: str,
        payment_reference: str,
        actor: str,
    ) -> dict:
        if amount_cents <= 0:
            raise LicenseApiError(400, "INVALID_AMOUNT", "订单金额必须大于 0")
        now = _now_ts()
        order_no = _safe_id("ord")
        with self._lock, self.conn:
            if self.conn.execute(
                "SELECT 1 FROM accounts WHERE account_id=?",
                (account_id,),
            ).fetchone() is None:
                raise LicenseApiError(404, "ACCOUNT_NOT_FOUND", "账号不存在")
            try:
                self.conn.execute(
                    """
                    INSERT INTO orders (
                        order_no, account_id, plan, amount_cents,
                        currency, payment_reference, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'created', ?)
                    """,
                    (
                        order_no,
                        account_id,
                        plan,
                        amount_cents,
                        currency.upper(),
                        payment_reference,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise LicenseApiError(
                    409,
                    "PAYMENT_REFERENCE_EXISTS",
                    "付款流水号已经存在",
                ) from exc
            self._audit(actor, "create_order", order_no, {"plan": plan})
        return self.get_order(order_no)

    def confirm_order(
        self,
        *,
        order_no: str,
        actor: str,
        activation_ttl_seconds: int = CODE_TTL_SECONDS,
    ) -> dict:
        now = _now_ts()
        with self._lock, self.conn:
            order = self.conn.execute(
                "SELECT * FROM orders WHERE order_no=?",
                (order_no,),
            ).fetchone()
            if order is None:
                raise LicenseApiError(404, "ORDER_NOT_FOUND", "订单不存在")
            if order["status"] not in {"created", "paid"}:
                raise LicenseApiError(
                    409,
                    "ORDER_NOT_ACTIVATABLE",
                    "订单状态不允许发送激活码",
                )
            account = self.conn.execute(
                "SELECT * FROM accounts WHERE account_id=?",
                (order["account_id"],),
            ).fetchone()
            if account is None:
                raise LicenseApiError(404, "ACCOUNT_NOT_FOUND", "账号不存在")
            code = f"{secrets.randbelow(1_000_000):06d}"
            activation_id = _safe_id("activate")
            self.conn.execute(
                """
                INSERT INTO activation_codes (
                    activation_id, order_no, account_id, code_hash,
                    attempts, created_at, expires_at
                ) VALUES (?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    activation_id,
                    order_no,
                    account["account_id"],
                    self._hash_code(
                        account["account_id"],
                        f"activation:{order_no}",
                        code,
                    ),
                    now,
                    now + activation_ttl_seconds,
                ),
            )
            channel, destination = self._account_destination(account)
            try:
                if channel == "email":
                    self.sender.send_email(destination, code, "activation")
                else:
                    self.sender.send_sms(destination, code, "activation")
            except Exception:
                self.conn.execute(
                    "DELETE FROM activation_codes WHERE activation_id=?",
                    (activation_id,),
                )
                raise
            self.conn.execute(
                """
                UPDATE orders
                SET status='paid', paid_at=COALESCE(paid_at, ?), confirmed_by=?
                WHERE order_no=?
                """,
                (now, actor, order_no),
            )
            self._audit(actor, "confirm_order", order_no, {})
        return {
            "ok": True,
            "order_no": order_no,
            "status": "paid",
            "destination": _mask_destination(destination),
            "expires_in": activation_ttl_seconds,
        }

    def activate_order(
        self,
        *,
        account_id: str,
        device_id: str,
        order_no: str,
        activation_code: str,
    ) -> dict:
        now = _now_ts()
        with self._lock:
            with self.conn:
                self._require_device(account_id, device_id, now)
                order = self.conn.execute(
                    "SELECT * FROM orders WHERE order_no=? AND account_id=?",
                    (order_no, account_id),
                ).fetchone()
                if order is None:
                    raise LicenseApiError(404, "ORDER_NOT_FOUND", "订单不存在")
                if order["status"] != "paid":
                    raise LicenseApiError(
                        409,
                        "ORDER_NOT_PAID",
                        "订单尚未确认付款",
                    )
                activation = self.conn.execute(
                    """
                    SELECT * FROM activation_codes
                    WHERE order_no=? AND account_id=? AND consumed_at IS NULL
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (order_no, account_id),
                ).fetchone()
                self._verify_activation_row(
                    activation,
                    account_id,
                    order_no,
                    activation_code,
                    now,
                )
                self.conn.execute(
                    "UPDATE activation_codes SET consumed_at=? WHERE activation_id=?",
                    (now, activation["activation_id"]),
                )
                previous = self.conn.execute(
                    """
                    SELECT expires_at FROM entitlements
                    WHERE account_id=? AND plan=? AND revoked_at IS NULL
                    ORDER BY expires_at DESC LIMIT 1
                    """,
                    (account_id, order["plan"]),
                ).fetchone()
                start = max(
                    _now_dt(),
                    _dt_from_ts(float(previous["expires_at"]))
                    if previous
                    else _now_dt(),
                )
                duration = timedelta(days=30 if order["plan"] == "monthly" else 365)
                expires = start + duration
                grace = expires + timedelta(days=7)
                token = EntitlementToken(
                    account_id=account_id,
                    device_id=device_id,
                    plan=order["plan"],
                    issued_at=_now_dt(),
                    not_before=_now_dt(),
                    expires_at=expires,
                    grace_until=grace,
                    license_id=_safe_id("lic"),
                    nonce=secrets.token_hex(16),
                    key_id=self.key_id,
                ).sign(self.private_key)
                self._store_entitlement(token, now, order_no=order_no)
                self.conn.execute(
                    "UPDATE orders SET status='activated' WHERE order_no=?",
                    (order_no,),
                )
        return {
            "ok": True,
            "account_id": account_id,
            "device_id": device_id,
            "plan": order["plan"],
            "entitlement": token.model_dump(mode="json"),
        }

    def get_order(self, order_no: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM orders WHERE order_no=?",
            (order_no,),
        ).fetchone()
        if row is None:
            raise LicenseApiError(404, "ORDER_NOT_FOUND", "订单不存在")
        return {key: row[key] for key in row.keys()}

    def list_accounts(
        self,
        *,
        query: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        limit = min(200, max(1, int(limit)))
        offset = max(0, int(offset))
        pattern = f"%{query.strip()}%"
        rows = self.conn.execute(
            """
            SELECT account_id, email, phone, status, created_at
            FROM accounts
            WHERE ?='' OR email LIKE ? OR phone LIKE ? OR account_id LIKE ?
            ORDER BY created_at DESC LIMIT ? OFFSET ?
            """,
            (query.strip(), pattern, pattern, pattern, limit, offset),
        ).fetchall()
        return {
            "ok": True,
            "items": [dict(row) for row in rows],
            "limit": limit,
            "offset": offset,
        }

    def list_devices(self, account_id: str) -> dict:
        rows = self.conn.execute(
            """
            SELECT account_id, installation_id, display_name,
                   first_seen, last_seen, revoked_at
            FROM devices WHERE account_id=?
            ORDER BY first_seen
            """,
            (account_id,),
        ).fetchall()
        return {"ok": True, "items": [dict(row) for row in rows]}

    def list_orders(
        self,
        *,
        account_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        limit = min(200, max(1, int(limit)))
        offset = max(0, int(offset))
        clauses = []
        values: list[object] = []
        if account_id:
            clauses.append("account_id=?")
            values.append(account_id)
        if status:
            clauses.append("status=?")
            values.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT * FROM orders {where}
            ORDER BY created_at DESC LIMIT ? OFFSET ?
            """,
            (*values, limit, offset),
        ).fetchall()
        return {
            "ok": True,
            "items": [dict(row) for row in rows],
            "limit": limit,
            "offset": offset,
        }

    def list_entitlements(
        self,
        account_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        limit = min(200, max(1, int(limit)))
        offset = max(0, int(offset))
        rows = self.conn.execute(
            """
            SELECT license_id, account_id, device_id, plan, order_no,
                   issued_at, expires_at, grace_until, revoked_at
            FROM entitlements WHERE account_id=?
            ORDER BY issued_at DESC LIMIT ? OFFSET ?
            """,
            (account_id, limit, offset),
        ).fetchall()
        return {
            "ok": True,
            "items": [dict(row) for row in rows],
            "limit": limit,
            "offset": offset,
        }

    def unbind_device(
        self,
        *,
        account_id: str,
        installation_id: str,
        actor: str,
    ) -> dict:
        now = _now_ts()
        with self._lock, self.conn:
            row = self.conn.execute(
                """
                SELECT 1 FROM devices
                WHERE account_id=? AND installation_id=?
                """,
                (account_id, installation_id),
            ).fetchone()
            if row is None:
                raise LicenseApiError(404, "DEVICE_NOT_FOUND", "设备不存在")
            self.conn.execute(
                """
                UPDATE devices SET revoked_at=?
                WHERE account_id=? AND installation_id=?
                """,
                (now, account_id, installation_id),
            )
            self.conn.execute(
                """
                UPDATE sessions SET revoked_at=?
                WHERE account_id=? AND installation_id=?
                  AND revoked_at IS NULL
                """,
                (now, account_id, installation_id),
            )
            self._audit(
                actor,
                "unbind_device",
                installation_id,
                {"account_id": account_id},
            )
        return {
            "ok": True,
            "account_id": account_id,
            "installation_id": installation_id,
            "revoked": True,
        }

    def refund_order(self, *, order_no: str, actor: str) -> dict:
        now = _now_ts()
        with self._lock, self.conn:
            order = self.conn.execute(
                "SELECT * FROM orders WHERE order_no=?",
                (order_no,),
            ).fetchone()
            if order is None:
                raise LicenseApiError(404, "ORDER_NOT_FOUND", "订单不存在")
            if order["status"] == "refunded":
                return {"ok": True, **dict(order)}
            if order["status"] not in {"paid", "activated"}:
                raise LicenseApiError(
                    409,
                    "ORDER_NOT_REFUNDABLE",
                    "订单状态不允许退款",
                )
            self.conn.execute(
                "UPDATE orders SET status='refunded' WHERE order_no=?",
                (order_no,),
            )
            self.conn.execute(
                """
                UPDATE entitlements SET revoked_at=?
                WHERE order_no=? AND revoked_at IS NULL
                """,
                (now, order_no),
            )
            self._audit(actor, "refund_order", order_no, {})
            refreshed = self.conn.execute(
                "SELECT * FROM orders WHERE order_no=?",
                (order_no,),
            ).fetchone()
        return {"ok": True, **dict(refreshed)}

    def revoke_entitlement(
        self,
        *,
        license_id: str,
        actor: str,
    ) -> dict:
        now = _now_ts()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM entitlements WHERE license_id=?",
                (license_id,),
            ).fetchone()
            if row is None:
                raise LicenseApiError(
                    404,
                    "ENTITLEMENT_NOT_FOUND",
                    "许可不存在",
                )
            self.conn.execute(
                """
                UPDATE entitlements SET revoked_at=COALESCE(revoked_at, ?)
                WHERE license_id=?
                """,
                (now, license_id),
            )
            self._audit(
                actor,
                "revoke_entitlement",
                license_id,
                {"account_id": row["account_id"]},
            )
        return {"ok": True, "license_id": license_id, "revoked": True}

    def authenticate_session(
        self,
        token: str,
        *,
        now: float | None = None,
    ) -> sqlite3.Row:
        current = _now_ts() if now is None else now
        row = self.conn.execute(
            """
            SELECT * FROM sessions
            WHERE token_hash=? AND revoked_at IS NULL AND expires_at>=?
            """,
            (self._hash_session_token(token), current),
        ).fetchone()
        if row is None:
            raise LicenseApiError(
                401,
                "SESSION_INVALID",
                "登录会话无效或已过期",
            )
        self._require_device(
            row["account_id"],
            row["installation_id"],
            current,
        )
        return row

    def _find_or_create_account(
        self,
        identity_type: str,
        destination: str,
        now: float,
    ) -> sqlite3.Row:
        column = "email" if identity_type == "email" else "phone"
        account = self.conn.execute(
            f"SELECT * FROM accounts WHERE {column}=?",
            (destination,),
        ).fetchone()
        if account:
            if account["status"] != "active":
                raise LicenseApiError(403, "ACCOUNT_DISABLED", "账号已停用")
            return account
        account_id = _safe_id("acc")
        self.conn.execute(
            f"""
            INSERT INTO accounts (account_id, {column}, status, created_at)
            VALUES (?, ?, 'active', ?)
            """,
            (account_id, destination, now),
        )
        return self.conn.execute(
            "SELECT * FROM accounts WHERE account_id=?",
            (account_id,),
        ).fetchone()

    def _bind_device(
        self,
        account_id: str,
        installation_id: str,
        display_name: str | None,
        now: float,
    ) -> None:
        if not installation_id or len(installation_id) > 128:
            raise LicenseApiError(400, "INVALID_DEVICE", "设备 ID 无效")
        other = self.conn.execute(
            """
            SELECT account_id FROM devices
            WHERE installation_id=? AND account_id<>? AND revoked_at IS NULL
            """,
            (installation_id, account_id),
        ).fetchone()
        if other:
            raise LicenseApiError(
                409,
                "DEVICE_BOUND_TO_OTHER_ACCOUNT",
                "该设备已经绑定其他账号",
            )
        existing = self.conn.execute(
            """
            SELECT 1 FROM devices
            WHERE account_id=? AND installation_id=?
            """,
            (account_id, installation_id),
        ).fetchone()
        if existing is None:
            active_count = self.conn.execute(
                """
                SELECT COUNT(*) AS total FROM devices
                WHERE account_id=? AND revoked_at IS NULL
                """,
                (account_id,),
            ).fetchone()
            if int(active_count["total"]) >= self.max_devices_per_account:
                raise LicenseApiError(
                    409,
                    "DEVICE_LIMIT_REACHED",
                    "账号可绑定设备数量已达上限",
                )
        self.conn.execute(
            """
            INSERT INTO devices (
                account_id, installation_id, display_name,
                first_seen, last_seen, revoked_at
            ) VALUES (?, ?, ?, ?, ?, NULL)
            ON CONFLICT(account_id, installation_id) DO UPDATE SET
                display_name=COALESCE(excluded.display_name, devices.display_name),
                last_seen=excluded.last_seen,
                revoked_at=NULL
            """,
            (account_id, installation_id, display_name, now, now),
        )

    def _issue_session(
        self,
        account_id: str,
        installation_id: str,
        now: float,
    ) -> str:
        token = secrets.token_urlsafe(32)
        self.conn.execute(
            """
            INSERT INTO sessions (
                session_id, account_id, installation_id,
                token_hash, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _safe_id("session"),
                account_id,
                installation_id,
                self._hash_session_token(token),
                now,
                now + 30 * 24 * 60 * 60,
            ),
        )
        return token

    def _hash_session_token(self, token: str) -> str:
        return hmac.new(
            self.code_pepper,
            f"session:{token}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _require_device(
        self,
        account_id: str,
        device_id: str,
        now: float,
    ) -> sqlite3.Row:
        device = self.conn.execute(
            """
            SELECT * FROM devices
            WHERE account_id=? AND installation_id=? AND revoked_at IS NULL
            """,
            (account_id, device_id),
        ).fetchone()
        if device is None:
            raise LicenseApiError(403, "DEVICE_NOT_AUTHORIZED", "设备未授权")
        self.conn.execute(
            """
            UPDATE devices SET last_seen=?
            WHERE account_id=? AND installation_id=?
            """,
            (now, account_id, device_id),
        )
        return device

    def _issue_trial_entitlement(
        self,
        account_id: str,
        device_id: str,
        remaining: int,
        now: float,
        *,
        lease_days: int,
    ) -> EntitlementToken:
        issued = _dt_from_ts(now)
        token = EntitlementToken(
            account_id=account_id,
            device_id=device_id,
            plan="trial",
            trial_remaining=remaining,
            issued_at=issued,
            not_before=issued,
            expires_at=issued + timedelta(days=lease_days),
            grace_until=None,
            license_id=_safe_id("lic"),
            nonce=secrets.token_hex(16),
            key_id=self.key_id,
        ).sign(self.private_key)
        self._store_entitlement(token, now)
        return token

    def _active_paid_entitlement(
        self,
        account_id: str,
        now: float,
    ) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT * FROM entitlements
            WHERE account_id=?
              AND plan IN ('monthly', 'yearly')
              AND revoked_at IS NULL
              AND expires_at >= ?
            ORDER BY expires_at DESC LIMIT 1
            """,
            (account_id, now),
        ).fetchone()

    def _issue_paid_lease(
        self,
        account_id: str,
        device_id: str,
        entitlement_row: sqlite3.Row,
        now: float,
    ) -> EntitlementToken:
        issued = _dt_from_ts(now)
        token = EntitlementToken(
            account_id=account_id,
            device_id=device_id,
            plan=entitlement_row["plan"],
            issued_at=issued,
            not_before=issued,
            expires_at=_dt_from_ts(float(entitlement_row["expires_at"])),
            grace_until=_dt_from_ts(float(entitlement_row["grace_until"])),
            license_id=_safe_id("lic"),
            nonce=secrets.token_hex(16),
            key_id=self.key_id,
        ).sign(self.private_key)
        self._store_entitlement(
            token,
            now,
            order_no=entitlement_row["order_no"],
        )
        return token

    def _store_entitlement(
        self,
        token: EntitlementToken,
        now: float,
        *,
        order_no: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO entitlements (
                license_id, account_id, device_id, plan,
                issued_at, expires_at, grace_until, token_json, order_no
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token.license_id,
                token.account_id,
                token.device_id,
                token.plan,
                float(token.issued_at.timestamp()),
                float(token.expires_at.timestamp()),
                float(
                    (token.grace_until or token.expires_at).timestamp()
                ),
                token.model_dump_json(),
                order_no,
            ),
        )

    def _trial_remaining(self, account_id: str, now: float) -> int:
        return max(
            0,
            MAX_TRIAL_USES - self._count_trial_uses(account_id, now),
        )

    def _count_trial_uses(self, account_id: str, now: float) -> int:
        self._expire_reservations(now)
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS total FROM trial_usages
            WHERE account_id=? AND status IN ('reserved', 'committed')
            """,
            (account_id,),
        ).fetchone()
        return int(row["total"])

    def _expire_reservations(self, now: float) -> None:
        self.conn.execute(
            """
            UPDATE trial_usages SET status='expired'
            WHERE status='reserved' AND expires_at < ?
            """,
            (now,),
        )

    def _hash_code(self, destination: str, purpose: str, code: str) -> str:
        payload = f"{destination}:{purpose}:{code}".encode("utf-8")
        return hmac.new(self.code_pepper, payload, hashlib.sha256).hexdigest()

    def _verify_code_row(
        self,
        row: sqlite3.Row | None,
        destination: str,
        purpose: str,
        code: str,
        now: float,
    ) -> None:
        if row is None or row["consumed_at"] is not None:
            raise LicenseApiError(400, "CODE_INVALID", "验证码无效或已使用")
        if float(row["expires_at"]) < now:
            raise LicenseApiError(400, "CODE_EXPIRED", "验证码已过期")
        if int(row["attempts"]) >= CODE_MAX_ATTEMPTS:
            raise LicenseApiError(429, "CODE_ATTEMPTS_EXCEEDED", "验证码尝试次数过多")
        expected = self._hash_code(destination, purpose, code)
        if not hmac.compare_digest(expected, row["code_hash"]):
            self.conn.execute(
                "UPDATE verification_codes SET attempts=attempts+1 WHERE code_id=?",
                (row["code_id"],),
            )
            raise LicenseApiError(400, "CODE_INVALID", "验证码错误")

    def _verify_activation_row(
        self,
        row: sqlite3.Row | None,
        account_id: str,
        order_no: str,
        code: str,
        now: float,
    ) -> None:
        if row is None or row["consumed_at"] is not None:
            raise LicenseApiError(400, "ACTIVATION_INVALID", "激活码无效或已使用")
        if float(row["expires_at"]) < now:
            raise LicenseApiError(400, "ACTIVATION_EXPIRED", "激活码已过期")
        if int(row["attempts"]) >= CODE_MAX_ATTEMPTS:
            raise LicenseApiError(429, "ACTIVATION_ATTEMPTS", "激活码尝试次数过多")
        expected = self._hash_code(
            account_id,
            f"activation:{order_no}",
            code,
        )
        if not hmac.compare_digest(expected, row["code_hash"]):
            self.conn.execute(
                "UPDATE activation_codes SET attempts=attempts+1 WHERE activation_id=?",
                (row["activation_id"],),
            )
            raise LicenseApiError(400, "ACTIVATION_INVALID", "激活码错误")

    def _account_destination(self, account: sqlite3.Row) -> tuple[str, str]:
        if account["email"]:
            return "email", account["email"]
        if account["phone"]:
            return "phone", account["phone"]
        raise LicenseApiError(409, "ACCOUNT_IDENTITY_MISSING", "账号缺少联系方式")

    def _audit(self, actor: str, action: str, target: str, detail: dict) -> None:
        import json

        self.conn.execute(
            """
            INSERT INTO admin_audit (
                audit_id, action, target, actor, detail, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _safe_id("audit"),
                action,
                target,
                actor,
                json.dumps(detail, ensure_ascii=False, sort_keys=True),
                _now_ts(),
            ),
        )


def _mask_destination(value: str) -> str:
    if "@" in value:
        local, domain = value.split("@", 1)
        visible = local[:2] if len(local) > 2 else local[:1]
        return f"{visible}***@{domain}"
    if len(value) <= 4:
        return "***"
    return f"{value[:3]}***{value[-2:]}"


class RequestCodeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identity_type: Literal["email", "phone"]
    destination: str = Field(min_length=3, max_length=200)
    purpose: Literal["login"] = "login"


class VerifyCodeBody(RequestCodeBody):
    code: str = Field(min_length=6, max_length=6)
    installation_id: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=100)


class TrialReserveBody(BaseModel):
    job_id: str
    input_sha256: str | None = None


class TrialAuthorizationBody(BaseModel):
    authorization_id: str


class CreateOrderBody(BaseModel):
    account_id: str
    plan: Literal["monthly", "yearly"]
    amount_cents: int = Field(gt=0)
    currency: str = Field(default="CNY", min_length=3, max_length=3)
    payment_reference: str = Field(min_length=1, max_length=200)


class ConfirmOrderBody(BaseModel):
    actor: str = Field(default="admin", min_length=1, max_length=100)


class ActorBody(BaseModel):
    actor: str = Field(default="admin", min_length=1, max_length=100)


class ActivateOrderBody(BaseModel):
    order_no: str
    activation_code: str = Field(min_length=6, max_length=6)


def create_license_app(
    service: LicenseService,
    *,
    admin_token: str,
) -> FastAPI:
    app = FastAPI(title="StoneBot License Service", version="0.1.0")

    @app.exception_handler(LicenseApiError)
    async def handle_license_error(_request, exc: LicenseApiError):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=exc.status_code,
            content={
                "ok": False,
                "error": {"code": exc.code, "message": exc.message},
            },
        )

    def require_admin(authorization: str | None) -> None:
        expected = f"Bearer {admin_token}"
        if authorization is None or not hmac.compare_digest(
            authorization,
            expected,
        ):
            raise LicenseApiError(401, "ADMIN_UNAUTHORIZED", "管理员认证失败")

    def require_session(authorization: str | None) -> sqlite3.Row:
        prefix = "Bearer "
        if authorization is None or not authorization.startswith(prefix):
            raise LicenseApiError(
                401,
                "SESSION_REQUIRED",
                "需要登录会话",
            )
        return service.authenticate_session(authorization[len(prefix):])

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.post("/v1/auth/request-code")
    def request_code(body: RequestCodeBody) -> dict:
        return service.request_code(
            identity_type=body.identity_type,
            destination=body.destination,
            purpose=body.purpose,
        )

    @app.post("/v1/auth/verify-code")
    def verify_code(body: VerifyCodeBody) -> dict:
        return service.verify_code(
            identity_type=body.identity_type,
            destination=body.destination,
            code=body.code,
            installation_id=body.installation_id,
            display_name=body.display_name,
        )

    @app.post("/v1/usage/reserve")
    def reserve_usage(
        body: TrialReserveBody,
        authorization: str | None = Header(default=None),
    ) -> dict:
        session = require_session(authorization)
        return service.reserve_trial_use(
            account_id=session["account_id"],
            device_id=session["installation_id"],
            **body.model_dump(),
        )

    @app.post("/v1/usage/commit")
    def commit_usage(
        body: TrialAuthorizationBody,
        authorization: str | None = Header(default=None),
    ) -> dict:
        session = require_session(authorization)
        return service.commit_trial_use(
            account_id=session["account_id"],
            device_id=session["installation_id"],
            **body.model_dump(),
        )

    @app.post("/v1/usage/release")
    def release_usage(
        body: TrialAuthorizationBody,
        authorization: str | None = Header(default=None),
    ) -> dict:
        session = require_session(authorization)
        return service.release_trial_use(
            account_id=session["account_id"],
            device_id=session["installation_id"],
            **body.model_dump(),
        )

    @app.post("/v1/admin/orders")
    def create_order(
        body: CreateOrderBody,
        authorization: str | None = Header(default=None),
    ) -> dict:
        require_admin(authorization)
        return service.create_order(
            **body.model_dump(),
            actor="admin",
        )

    @app.get("/v1/admin/accounts")
    def list_accounts(
        query: str = "",
        limit: int = 50,
        offset: int = 0,
        authorization: str | None = Header(default=None),
    ) -> dict:
        require_admin(authorization)
        return service.list_accounts(
            query=query,
            limit=limit,
            offset=offset,
        )

    @app.get("/v1/admin/accounts/{account_id}/devices")
    def list_devices(
        account_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict:
        require_admin(authorization)
        return service.list_devices(account_id)

    @app.post(
        "/v1/admin/accounts/{account_id}/devices/{installation_id}/unbind"
    )
    def unbind_device(
        account_id: str,
        installation_id: str,
        body: ActorBody,
        authorization: str | None = Header(default=None),
    ) -> dict:
        require_admin(authorization)
        return service.unbind_device(
            account_id=account_id,
            installation_id=installation_id,
            actor=body.actor,
        )

    @app.get("/v1/admin/orders")
    def list_orders(
        account_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        authorization: str | None = Header(default=None),
    ) -> dict:
        require_admin(authorization)
        return service.list_orders(
            account_id=account_id,
            status=status,
            limit=limit,
            offset=offset,
        )

    @app.post("/v1/admin/orders/{order_no}/confirm")
    def confirm_order(
        order_no: str,
        body: ConfirmOrderBody,
        authorization: str | None = Header(default=None),
    ) -> dict:
        require_admin(authorization)
        return service.confirm_order(
            order_no=order_no,
            actor=body.actor,
        )

    @app.post("/v1/admin/orders/{order_no}/refund")
    def refund_order(
        order_no: str,
        body: ActorBody,
        authorization: str | None = Header(default=None),
    ) -> dict:
        require_admin(authorization)
        return service.refund_order(
            order_no=order_no,
            actor=body.actor,
        )

    @app.get("/v1/admin/accounts/{account_id}/entitlements")
    def list_entitlements(
        account_id: str,
        limit: int = 50,
        offset: int = 0,
        authorization: str | None = Header(default=None),
    ) -> dict:
        require_admin(authorization)
        return service.list_entitlements(
            account_id,
            limit=limit,
            offset=offset,
        )

    @app.post("/v1/admin/entitlements/{license_id}/revoke")
    def revoke_entitlement(
        license_id: str,
        body: ActorBody,
        authorization: str | None = Header(default=None),
    ) -> dict:
        require_admin(authorization)
        return service.revoke_entitlement(
            license_id=license_id,
            actor=body.actor,
        )

    @app.post("/v1/license/activate")
    def activate_order(
        body: ActivateOrderBody,
        authorization: str | None = Header(default=None),
    ) -> dict:
        session = require_session(authorization)
        return service.activate_order(
            account_id=session["account_id"],
            device_id=session["installation_id"],
            **body.model_dump(),
        )

    return app
