"""Check a signed StoneBot release feed without installing it."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.release_manifest import public_key_from_base64  # noqa: E402
from src.update_manager import (  # noqa: E402
    candidate_for_update,
    fetch_release_manifest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify a remote StoneBot release manifest.",
    )
    parser.add_argument("--manifest-url", required=True)
    parser.add_argument("--public-key-file", required=True)
    parser.add_argument("--current-version", required=True)
    parser.add_argument("--current-data-version", type=int, default=1)
    parser.add_argument("--product", default="stonebot")
    parser.add_argument("--channel", default="stable")
    args = parser.parse_args(argv)

    public_key = public_key_from_base64(
        Path(args.public_key_file).read_text(encoding="utf-8").strip()
    )
    manifest = fetch_release_manifest(
        args.manifest_url,
        public_key,
        expected_product=args.product,
        expected_channel=args.channel,
    )
    candidate = candidate_for_update(
        manifest,
        current_version=args.current_version,
        current_data_version=args.current_data_version,
        product=args.product,
        channel=args.channel,
    )
    print(
        json.dumps(
            {
                "update_available": candidate.update_available,
                "reason": candidate.reason,
                "current_version": candidate.current_version,
                "available_version": manifest.version,
                "artifact": manifest.artifact.model_dump(mode="json"),
                "manifest_digest": manifest.digest(),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
