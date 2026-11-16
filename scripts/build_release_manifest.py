"""Build and sign a StoneBot release manifest."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.release_manifest import (  # noqa: E402
    build_release_manifest,
    private_key_from_base64,
    write_release_manifest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build and sign a StoneBot release manifest.",
    )
    parser.add_argument("--release-dir", required=True)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--product", default="stonebot")
    parser.add_argument("--version", required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument(
        "--channel",
        choices=("stable", "beta", "offline"),
        default="stable",
    )
    parser.add_argument("--minimum-version")
    parser.add_argument("--minimum-data-version", type=int, default=1)
    parser.add_argument("--metadata-json")
    parser.add_argument(
        "--private-key-file",
        help="File containing a base64 raw Ed25519 private key.",
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    private_key_value = os.environ.get("STONEBOT_RELEASE_PRIVATE_KEY")
    if args.private_key_file:
        private_key_value = Path(
            args.private_key_file
        ).read_text(encoding="utf-8").strip()
    if not private_key_value:
        parser.error(
            "--private-key-file or STONEBOT_RELEASE_PRIVATE_KEY is required"
        )
    metadata = (
        json.loads(args.metadata_json)
        if args.metadata_json
        else {}
    )
    manifest = build_release_manifest(
        args.release_dir,
        args.artifact,
        product=args.product,
        version=args.version,
        key_id=args.key_id,
        channel=args.channel,
        minimum_version=args.minimum_version,
        minimum_data_version=args.minimum_data_version,
        metadata=metadata,
    ).sign(private_key_from_base64(private_key_value))
    path = write_release_manifest(manifest, args.output)
    print(
        json.dumps(
            {
                "manifest": str(path),
                "version": manifest.version,
                "digest": manifest.digest(),
                "artifact_sha256": manifest.artifact.sha256,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
