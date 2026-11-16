"""Login to StoneBot License Service and store the local entitlement."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.license_client import LicenseServiceClient, LocalSessionStore  # noqa: E402
from src.licensing import (  # noqa: E402
    DeviceIdentityStore,
    EntitlementToken,
    LocalEntitlementStore,
)
from src.local_runtime import LocalRuntimeLayout  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Login to StoneBot and store the signed entitlement.",
    )
    parser.add_argument("--root")
    parser.add_argument("--service-url", required=True)
    parser.add_argument(
        "--identity-type",
        choices=("email", "phone"),
        required=True,
    )
    parser.add_argument("--destination", required=True)
    parser.add_argument("--device-name")
    args = parser.parse_args(argv)

    layout = LocalRuntimeLayout.resolve(args.root).ensure()
    identity = DeviceIdentityStore(
        layout.device_identity_path
    ).load_or_create(display_name=args.device_name)
    client = LicenseServiceClient(
        args.service_url,
        LocalSessionStore(layout.session_path),
    )
    client.request_code(
        identity_type=args.identity_type,
        destination=args.destination,
    )
    code = input("验证码: ").strip()
    payload = client.verify_code(
        identity_type=args.identity_type,
        destination=args.destination,
        code=code,
        installation_id=identity.installation_id,
        display_name=args.device_name,
    )
    token = EntitlementToken.model_validate(payload["entitlement"])
    LocalEntitlementStore(layout.entitlement_path).save(token)
    print(
        json.dumps(
            {
                "ok": True,
                "account_id": payload["account_id"],
                "device_id": payload["device_id"],
                "trial_remaining": payload.get("trial_remaining"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
