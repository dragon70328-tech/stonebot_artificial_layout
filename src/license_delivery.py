"""Production email/SMS delivery adapters for the License Service."""

from __future__ import annotations

import json
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class DeliveryError(RuntimeError):
    """Raised when an OTP cannot be delivered."""


class SmtpEmailSender:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        from_address: str,
        use_ssl: bool = False,
        starttls: bool = True,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = password
        self.from_address = from_address
        self.use_ssl = use_ssl
        self.starttls = starttls
        self.timeout_seconds = float(timeout_seconds)

    def send_email(self, destination: str, code: str, purpose: str) -> None:
        message = self.build_message(destination, code, purpose)
        try:
            if self.use_ssl:
                client: smtplib.SMTP = smtplib.SMTP_SSL(
                    self.host,
                    self.port,
                    timeout=self.timeout_seconds,
                    context=ssl.create_default_context(),
                )
            else:
                client = smtplib.SMTP(
                    self.host,
                    self.port,
                    timeout=self.timeout_seconds,
                )
            with client:
                client.ehlo()
                if self.starttls and not self.use_ssl:
                    client.starttls(context=ssl.create_default_context())
                    client.ehlo()
                if self.username:
                    client.login(self.username, self.password or "")
                client.send_message(message)
        except Exception as exc:
            raise DeliveryError("SMTP delivery failed") from exc

    def build_message(
        self,
        destination: str,
        code: str,
        purpose: str,
    ) -> EmailMessage:
        label = "登录" if purpose == "login" else "订阅激活"
        message = EmailMessage()
        message["Subject"] = f"StoneBot {label}验证码"
        message["From"] = self.from_address
        message["To"] = destination
        message.set_content(
            f"您的 StoneBot {label}验证码是：{code}\n\n"
            "验证码在 10 分钟内有效，且只能使用一次。\n"
            "如果这不是您的操作，请忽略此邮件。"
        )
        return message

    def send_sms(self, destination: str, code: str, purpose: str) -> None:
        raise DeliveryError("SMTP sender does not support SMS")


class WebhookSmsSender:
    """Generic HTTPS SMS adapter.

    The configured webhook receives:
    ``{"destination": "...", "code": "...", "purpose": "login"}``.
    """

    def __init__(
        self,
        *,
        url: str,
        bearer_token: str,
        timeout_seconds: float = 20.0,
    ) -> None:
        if urlparse(url).scheme != "https":
            raise ValueError("SMS webhook URL must use HTTPS")
        self.url = url
        self.bearer_token = bearer_token
        self.timeout_seconds = float(timeout_seconds)

    def send_sms(self, destination: str, code: str, purpose: str) -> None:
        payload = json.dumps(
            {
                "destination": destination,
                "code": code,
                "purpose": purpose,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            self.url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.bearer_token}",
                "User-Agent": "StoneBot-License-Service",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                if urlparse(response.geturl()).scheme != "https":
                    raise DeliveryError(
                        "SMS webhook redirected to a non-HTTPS URL"
                    )
                if response.status < 200 or response.status >= 300:
                    raise DeliveryError(
                        f"SMS webhook returned HTTP {response.status}"
                    )
        except DeliveryError:
            raise
        except Exception as exc:
            raise DeliveryError("SMS webhook delivery failed") from exc

    def send_email(self, destination: str, code: str, purpose: str) -> None:
        raise DeliveryError("SMS webhook does not support email")


class CompositeCodeSender:
    def __init__(
        self,
        *,
        email_sender: SmtpEmailSender | None,
        sms_sender: WebhookSmsSender | None,
    ) -> None:
        self.email_sender = email_sender
        self.sms_sender = sms_sender

    def send_email(self, destination: str, code: str, purpose: str) -> None:
        if self.email_sender is None:
            raise DeliveryError("email delivery is not configured")
        self.email_sender.send_email(destination, code, purpose)

    def send_sms(self, destination: str, code: str, purpose: str) -> None:
        if self.sms_sender is None:
            raise DeliveryError("SMS delivery is not configured")
        self.sms_sender.send_sms(destination, code, purpose)
