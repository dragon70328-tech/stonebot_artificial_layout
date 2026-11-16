"""Download, verify, stage and activate one signed StoneBot release."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.local_runtime import LocalRuntimeLayout  # noqa: E402
from src.release_manifest import public_key_from_base64  # noqa: E402
from src.update_manager import (  # noqa: E402
    UpdateInstaller,
    candidate_for_update,
    download_release_artifact,
    fetch_release_manifest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install one verified StoneBot update.",
    )
    parser.add_argument("--manifest-url", required=True)
    parser.add_argument("--public-key-file", required=True)
    parser.add_argument("--root")
    parser.add_argument("--current-data-version", type=int, default=1)
    parser.add_argument("--product", default="stonebot")
    parser.add_argument("--channel", default="stable")
    parser.add_argument(
        "--artifact-url",
        help="Defaults to the artifact filename beside the manifest URL.",
    )
    args = parser.parse_args(argv)

    public_key = public_key_from_base64(
        Path(args.public_key_file).read_text(encoding="utf-8").strip()
    )
    layout = LocalRuntimeLayout.resolve(args.root).ensure()
    installer = UpdateInstaller(
        layout,
        product=args.product,
        channel=args.channel,
    )
    manifest = fetch_release_manifest(
        args.manifest_url,
        public_key,
        expected_product=args.product,
        expected_channel=args.channel,
    )
    current = installer.read_current()
    current_version = str(
        (current.get("active") or {}).get("version") or "0.0.0"
    )
    candidate = candidate_for_update(
        manifest,
        current_version=current_version,
        current_data_version=args.current_data_version,
        product=args.product,
        channel=args.channel,
    )
    if not candidate.update_available:
        print(
            json.dumps(
                {
                    "updated": False,
                    "reason": candidate.reason,
                    "current_version": current_version,
                    "available_version": manifest.version,
                },
                ensure_ascii=False,
            )
        )
        return 0

    artifact_url = args.artifact_url or urljoin(
        args.manifest_url,
        manifest.artifact.filename,
    )
    if (
        urlparse(artifact_url).scheme != "https"
        or urlparse(artifact_url).netloc
        != urlparse(args.manifest_url).netloc
    ):
        parser.error(
            "--artifact-url must use HTTPS and the same origin as the manifest"
        )
    artifact = download_release_artifact(manifest, artifact_url, layout)
    version_dir = installer.stage_release(manifest, artifact, public_key)
    pointer = installer.activate(manifest, public_key)
    print(
        json.dumps(
            {
                "updated": True,
                "version": manifest.version,
                "version_directory": str(version_dir),
                "current": pointer,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
