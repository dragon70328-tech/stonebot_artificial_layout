"""Show local StoneBot license status."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.licensing import build_license_guard  # noqa: E402
from src.local_runtime import LocalRuntimeLayout  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Show local StoneBot entitlement status.",
    )
    parser.add_argument("--root")
    parser.add_argument("--public-key-file", required=True)
    args = parser.parse_args(argv)

    layout = LocalRuntimeLayout.resolve(args.root).ensure()
    guard = build_license_guard(
        layout,
        Path(args.public_key_file).read_text(encoding="utf-8").strip(),
    )
    print(json.dumps(guard.status().to_dict(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
