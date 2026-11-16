"""HTTP client for the StoneBot License Service."""

from __future__ import annotations

import json
import os
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field

from src.secure_storage import (
    dpapi_available,
    protect_secret,
    unprotect_secret,
)


class LicenseClientError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


class ClientSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_token: str = Field(min_length=16)
    account_id: str
    device_id: str
    expires_at: datetime


class LocalSessionStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def save(
        self,
        *,
        session_token: str,
        account_id: str,
        device_id: str,
        expires_in: int,
    ) -> ClientSession:
        session = ClientSession(
            session_token=session_token,
            account_id=account_id,
            device_id=device_id,
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=max(1, int(expires_in))),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        serialized = session.model_dump_json().encode("utf-8")
        if dpapi_available():
            envelope = {
                "schema_version": 1,
                "protection": "windows-dpapi",
                "payload": base64.b64encode(
                    protect_secret(serialized)
                ).decode("ascii"),
            }
        else:
            envelope = {
                "schema_version": 1,
                "protection": "file-permissions",
                "payload": base64.b64encode(serialized).decode("ascii"),
            }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                envelope,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
        if os.name != "nt":
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        return session

    def load(self) -> ClientSession:
        if not self.path.exists():
            raise LicenseClientError(
                "SESSION_MISSING",
                "本机尚未登录 StoneBot 账号",
            )
        try:
            text = self.path.read_text(encoding="utf-8")
            payload = json.loads(text)
            if isinstance(payload, dict) and "protection" in payload:
                raw = base64.b64decode(
                    str(payload["payload"]),
                    validate=True,
                )
                if payload["protection"] == "windows-dpapi":
                    raw = unprotect_secret(raw)
                elif payload["protection"] != "file-permissions":
                    raise ValueError("unsupported session protection")
                return ClientSession.model_validate_json(raw)
            return ClientSession.model_validate(payload)
        except Exception as exc:
            raise LicenseClientError(
                "SESSION_INVALID",
                "本地登录会话损坏",
            ) from exc

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def _validate_service_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme == "https":
        return value.rstrip("/")
    if parsed.scheme == "http" and parsed.hostname in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        return value.rstrip("/")
    raise ValueError(
        "license service URL must use HTTPS or loopback HTTP"
    )


class LicenseServiceClient:
    def __init__(
        self,
        base_url: str,
        session_store: LocalSessionStore,
        *,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.base_url = _validate_service_url(base_url)
        self.session_store = session_store
        self.timeout_seconds = float(timeout_seconds)

    def request_code(
        self,
        *,
        identity_type: str,
        destination: str,
    ) -> dict:
        return self._request(
            "POST",
            "/v1/auth/request-code",
            {
                "identity_type": identity_type,
                "destination": destination,
            },
        )

    def verify_code(
        self,
        *,
        identity_type: str,
        destination: str,
        code: str,
        installation_id: str,
        display_name: str | None = None,
    ) -> dict:
        payload = self._request(
            "POST",
            "/v1/auth/verify-code",
            {
                "identity_type": identity_type,
                "destination": destination,
                "code": code,
                "installation_id": installation_id,
                "display_name": display_name,
            },
        )
        self.session_store.save(
            session_token=str(payload["session_token"]),
            account_id=str(payload["account_id"]),
            device_id=str(payload["device_id"]),
            expires_in=int(payload.get("session_expires_in", 2592000)),
        )
        return payload

    def reserve_use(
        self,
        *,
        job_id: str,
        input_sha256: str | None,
    ) -> dict:
        return self._request(
            "POST",
            "/v1/usage/reserve",
            {
                "job_id": job_id,
                "input_sha256": input_sha256,
            },
            session=self._session_token(),
        )

    def commit_use(self, authorization_id: str) -> dict:
        return self._request(
            "POST",
            "/v1/usage/commit",
            {"authorization_id": authorization_id},
            session=self._session_token(),
        )

    def release_use(self, authorization_id: str) -> dict:
        return self._request(
            "POST",
            "/v1/usage/release",
            {"authorization_id": authorization_id},
            session=self._session_token(),
        )

    def activate_order(
        self,
        *,
        order_no: str,
        activation_code: str,
    ) -> dict:
        return self._request(
            "POST",
            "/v1/license/activate",
            {
                "order_no": order_no,
                "activation_code": activation_code,
            },
            session=self._session_token(),
        )

    def current_session(self) -> ClientSession:
        session = self.session_store.load()
        if session.expires_at <= datetime.now(timezone.utc):
            raise LicenseClientError(
                "SESSION_EXPIRED",
                "本地登录会话已过期，请重新登录",
            )
        return session

    def _session_token(self) -> str:
        return self.current_session().session_token

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any],
        *,
        session: str | None = None,
    ) -> dict:
        url = f"{self.base_url}{path}"
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "StoneBot-Local-Runtime",
        }
        if session:
            headers["Authorization"] = f"Bearer {session}"
        request = Request(url, data=body, method=method, headers=headers)
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                data = response.read(2 * 1024 * 1024)
        except HTTPError as exc:
            raw = exc.read(1024 * 1024)
            try:
                parsed = json.loads(raw.decode("utf-8"))
                error = parsed.get("error") or {}
            except Exception:
                error = {}
            if exc.code == 401 and session:
                self.session_store.clear()
            raise LicenseClientError(
                str(error.get("code") or "LICENSE_HTTP_ERROR"),
                str(error.get("message") or f"License Service HTTP {exc.code}"),
                status_code=exc.code,
                details=error,
            ) from exc
        except URLError as exc:
            raise LicenseClientError(
                "LICENSE_SERVICE_UNAVAILABLE",
                "无法连接 StoneBot License Service",
            ) from exc
        try:
            parsed = json.loads(data.decode("utf-8"))
        except Exception as exc:
            raise LicenseClientError(
                "LICENSE_RESPONSE_INVALID",
                "License Service 返回了无效响应",
            ) from exc
        if not isinstance(parsed, dict):
            raise LicenseClientError(
                "LICENSE_RESPONSE_INVALID",
                "License Service 返回了非对象响应",
            )
        return parsed
