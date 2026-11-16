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
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    layout = LocalRuntimeLayout.resolve(args.root).ensure()
    try:
        with acquire_runtime_instance(layout):
            serve_stdio(
                service=PluginJobService(layout.data_dir),
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
