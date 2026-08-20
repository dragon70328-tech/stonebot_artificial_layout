"""按日期时间生成项目备份 zip。"""

from __future__ import annotations

import argparse
import zipfile
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_EXCLUDE_DIRS = {
    ".git",
    ".pytest_cache",
    ".venv",
    "venv",
    "__pycache__",
    "output",
    "_archive",
}

DEFAULT_EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".zip"}


def _excluded(relative_path: Path) -> bool:
    parts = relative_path.parts
    if not parts:
        return True
    if any(part in DEFAULT_EXCLUDE_DIRS for part in parts):
        return True
    if relative_path.name.startswith("_backup_") and relative_path.suffix == ".zip":
        return True
    if relative_path.suffix in DEFAULT_EXCLUDE_SUFFIXES:
        return True
    return False


def create_backup(destination: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = destination / f"_backup_{timestamp}.zip"
    destination.mkdir(parents=True, exist_ok=True)

    included_files: list[Path] = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(PROJECT_ROOT)
        if _excluded(relative):
            continue
        included_files.append(path)

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(included_files):
            archive.write(path, path.relative_to(PROJECT_ROOT).as_posix())

    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="按日期时间备份当前项目。")
    parser.add_argument(
        "--destination",
        type=Path,
        default=PROJECT_ROOT,
        help="备份 zip 输出目录，默认为项目根目录。",
    )
    args = parser.parse_args()

    output_path = create_backup(args.destination)
    size_kb = output_path.stat().st_size / 1024
    print(f"Backup created: {output_path}")
    print(f"Size: {size_kb:.1f} KB")


if __name__ == "__main__":
    main()
