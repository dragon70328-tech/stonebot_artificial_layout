"""CLI 运行目录约定。

排板 CLI（``main.py``）和审图 CLI（``src/cli_audit.py``）共用同一个
"时间戳_文件名" 输出目录规则，放在这里避免两个入口互相 import。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def make_output_dir(dxf_path: str, *, root: Path | None = None) -> Path:
    """按 时间戳_文件名 创建输出子目录。"""
    stem = Path(dxf_path).stem
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_dir = root if root is not None else PROJECT_ROOT / "output"
    out_dir = base_dir / f"{ts}_{stem}"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir
