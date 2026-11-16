"""Activate a paid StoneBot order and store the entitlement."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.license_client import LicenseServiceClient, LocalSessionStore  # noqa: E402
from src.licensing import EntitlementToken, LocalEntitlementStore  # noqa: E402
from src.local_runtime import LocalRuntimeLayout  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Activate a paid StoneBot subscription.",
    )
    parser.add_argument("--root")
    parser.add_argument("--service-url", required=True)
    parser.add_argument("--order-no", required=True)
    args = parser.parse_args(argv)

    layout = LocalRuntimeLayout.resolve(args.root).ensure()
    client = LicenseServiceClient(
        args.service_url,
        LocalSessionStore(layout.session_path),
    )
    code = input("邮件或短信激活码: ").strip()
    payload = client.activate_order(
        order_no=args.order_no,
        activation_code=code,
    )
    token = EntitlementToken.model_validate(payload["entitlement"])
    LocalEntitlementStore(layout.entitlement_path).save(token)
    print(
        json.dumps(
            {
                "ok": True,
                "account_id": payload["account_id"],
                "device_id": payload["device_id"],
                "plan": payload["plan"],
                "expires_at": token.expires_at.isoformat(),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
