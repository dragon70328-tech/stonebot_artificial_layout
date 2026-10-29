"""Materialize a DSH plugin using absolute paths for the current machine.

The committed DSH plugin template resolves runtime paths from environment
variables. This helper creates a separate local-only package with concrete
paths, which is easier to install through the DSH plugin UI or CLI.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--tool-profile",
        choices=["full", "compact"],
        default="compact",
    )
    args = parser.parse_args()

    template = Path(__file__).parent / "dsh_plugin"
    output = Path(args.output).resolve()
    if output.exists():
        raise SystemExit(f"output already exists: {output}")
    shutil.copytree(template, output)

    launcher = (
        root
        / "integrations"
        / "codex_plugin"
        / "scripts"
        / "launch_mcp.py"
    ).resolve()
    python = Path(args.python).expanduser().resolve()
    if not launcher.exists():
        raise SystemExit(f"launcher not found: {launcher}")
    if not python.exists():
        raise SystemExit(f"python not found: {python}")

    patch = (
        "# Generated for this machine by make_local_plugin.py.\n"
        "- insert:\n"
        "    - id: mcp-stonebot\n"
        '      name: "@deepseek-ai/dsh-mcp-client"\n'
        "      config:\n"
        "        serverName: stonebot\n"
        "        transport: stdio\n"
        f"        command: {_yaml_string(python.as_posix())}\n"
        "        args:\n"
        '          - "-X"\n'
        '          - "utf8"\n'
        f"          - {_yaml_string(launcher.as_posix())}\n"
        '          - "--tool-profile"\n'
        f"          - {_yaml_string(args.tool_profile)}\n"
        "        failOnStartupError: false\n"
    )
    (output / "cordis.patch.yml").write_text(patch, encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
