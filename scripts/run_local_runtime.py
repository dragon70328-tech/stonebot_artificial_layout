"""Launch the per-user local StoneBot MCP Runtime."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.local_runtime import (  # noqa: E402
    LocalRuntimeLayout,
    RuntimeAlreadyRunning,
    acquire_runtime_instance,
)
from src.licensing import build_license_guard  # noqa: E402
from src.mcp_server import serve_stdio  # noqa: E402
from src.plugin_service import PluginJobService  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run StoneBot with per-user local data and instance locking.",
    )
    parser.add_argument(
        "--root",
        help="Application root; defaults to STONEBOT_LOCAL_ROOT or LOCALAPPDATA.",
    )
    parser.add_argument(
        "--tool-profile",
        choices=("full", "compact"),
        default="full",
    )
    parser.add_argument(
        "--license-public-key-file",
        help="Base64 Ed25519 public key used to verify local entitlements.",
    )
    parser.add_argument(
        "--device-name",
        default=None,
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    layout = LocalRuntimeLayout.resolve(args.root).ensure()
    try:
        with acquire_runtime_instance(layout):
            license_guard = None
            public_key_text = args.license_public_key_file
            if public_key_text:
                license_guard = build_license_guard(
                    layout,
                    Path(public_key_text).read_text(encoding="utf-8").strip(),
                    enforcement=True,
                    display_name=args.device_name,
                )
            serve_stdio(
                service=PluginJobService(
                    layout.data_dir,
                    license_guard=license_guard,
                ),
                tool_profile=args.tool_profile,
            )
    except RuntimeAlreadyRunning:
        print(
            f"StoneBot Runtime is already running for {layout.root}",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
