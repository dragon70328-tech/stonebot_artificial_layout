"""Dependency-free launcher which re-execs the configured Python runtime.

MCP stdio is UTF-8.  Passing JSON through Windows PowerShell can transcode
Chinese paths using a legacy code page, so the plugin launches Python directly
and lets the repository virtual environment own the actual server process.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Launch the Stonebot MCP server with a selected runtime",
    )
    parser.add_argument(
        "--tool-profile",
        choices=["full", "compact"],
        default=os.environ.get("STONEBOT_TOOL_PROFILE", "full"),
    )
    args = parser.parse_args()
    plugin_root = Path(__file__).resolve().parents[1]
    repo_root = plugin_root.parents[1]
    python = (
        os.environ.get("STONEBOT_PYTHON")
        or str(repo_root / ".venv" / "Scripts" / "python.exe")
    )
    if not Path(python).exists():
        python = sys.executable

    arguments = [
        python,
        "-X",
        "utf8",
        "-m",
        "src.mcp_server",
        "--tool-profile",
        args.tool_profile,
    ]
    data_root = os.environ.get("STONEBOT_MCP_DATA_ROOT")
    if data_root:
        arguments.extend(["--data-root", data_root])
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    return subprocess.call(arguments, cwd=repo_root, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
