"""Run the StoneBot License Service with environment-based configuration."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.license_delivery import (  # noqa: E402
    CompositeCodeSender,
    SmtpEmailSender,
    WebhookSmsSender,
)
from src.license_service import (  # noqa: E402
    CapturedCodeSender,
    LicenseService,
    create_license_app,
)
from src.release_manifest import private_key_from_base64  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the StoneBot License Service.",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get(
            "STONEBOT_LICENSE_DB",
            str(PROJECT_ROOT / "data" / "license_service.sqlite3"),
        ),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8120)
    args = parser.parse_args(argv)

    private_key_text = os.environ.get("STONEBOT_LICENSE_PRIVATE_KEY")
    key_id = os.environ.get("STONEBOT_LICENSE_KEY_ID")
    admin_token = os.environ.get("STONEBOT_LICENSE_ADMIN_TOKEN")
    code_pepper = os.environ.get("STONEBOT_LICENSE_CODE_PEPPER")
    if not all((private_key_text, key_id, admin_token, code_pepper)):
        parser.error(
            "STONEBOT_LICENSE_PRIVATE_KEY, STONEBOT_LICENSE_KEY_ID, "
            "STONEBOT_LICENSE_ADMIN_TOKEN and STONEBOT_LICENSE_CODE_PEPPER "
            "are required"
        )

    dev_capture = os.environ.get(
        "STONEBOT_LICENSE_DEV_CAPTURE",
        "0",
    ).strip() == "1"
    if dev_capture:
        sender = CapturedCodeSender()
    else:
        email_sender = None
        if os.environ.get("STONEBOT_SMTP_HOST"):
            email_sender = SmtpEmailSender(
                host=os.environ["STONEBOT_SMTP_HOST"],
                port=int(os.environ.get("STONEBOT_SMTP_PORT", "587")),
                username=os.environ.get("STONEBOT_SMTP_USERNAME"),
                password=os.environ.get("STONEBOT_SMTP_PASSWORD"),
                from_address=os.environ["STONEBOT_SMTP_FROM"],
                use_ssl=os.environ.get("STONEBOT_SMTP_SSL", "0") == "1",
                starttls=os.environ.get("STONEBOT_SMTP_STARTTLS", "1") == "1",
            )
        sms_sender = None
        if os.environ.get("STONEBOT_SMS_WEBHOOK_URL"):
            sms_sender = WebhookSmsSender(
                url=os.environ["STONEBOT_SMS_WEBHOOK_URL"],
                bearer_token=os.environ["STONEBOT_SMS_WEBHOOK_TOKEN"],
            )
        sender = CompositeCodeSender(
            email_sender=email_sender,
            sms_sender=sms_sender,
        )

    service = LicenseService(
        args.db,
        private_key=private_key_from_base64(private_key_text),
        key_id=key_id,
        sender=sender,
        code_pepper=code_pepper,
    )
    app = create_license_app(service, admin_token=admin_token)

    import uvicorn

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
