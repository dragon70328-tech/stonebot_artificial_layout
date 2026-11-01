"""认证确定性模式（certified）的验收门禁。

承诺：同一确认图纸 + 同一请求参数 + 同一算法/依赖环境下，
认证模式的排板结果逐位一致（版本号、布局、交付 DXF 字节）。
探索性模式（默认）不承诺——墙钟预算仍在，被截断时会在
stats / 输出里留下 determinism_hazards 标志。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from src.constraints import PROFILE_MIN_SHEETS, PROFILE_WATERJET
from src.job_context import JobContext
from src.models import Part
from src.tool_handlers import build_tool_handlers
from src.units import UnitSystem


def _rect_part(part_id: int, width: float, height: float) -> Part:
    polygon = Polygon([
        (0, 0), (width, 0), (width, height), (0, height),
    ])
    return Part(
        id=part_id,
        number=f"P-{part_id:04d}",
        polygon=polygon,
        outer_polygon=polygon,
    )


def _mixed_parts() -> list[Part]:
    # 多尺寸混合，逼出贪心 + 束搜索 + LNS + 压缩全链路
    sizes = [(400, 300), (350, 250), (500, 200), (300, 300), (200, 180)]
    return [_rect_part(i + 1, w, h) for i, (w, h) in enumerate(sizes)]


def _run_pipeline(parts, tmp_path: Path, name: str, *, certified: bool):
    context = JobContext(
        nesting_profile=PROFILE_MIN_SHEETS,
        parts=parts,
        unit=UnitSystem.METRIC,
        output_dir=tmp_path / name,
    )
    handlers = build_tool_handlers(context)
    handlers["confirm_numbering"]({"decision": "confirmed"})
    nested = handlers["nest_parts"]({
        "sheet_width": 1000,
        "sheet_height": 800,
        "budget": 0,
        "quick": True,
        "certified": certified,
    })
    handlers["postprocess"]({"result": {}})
    dxf_path = tmp_path / f"{name}.dxf"
    handlers["write_dxf"]({"path": str(dxf_path)})
    return context, nested, dxf_path


def test_certified_mode_twice_same_revision_and_bytes(tmp_path):
    """验收口径：两次独立认证运行 → 同一版本号 + 交付 DXF 逐字节一致。"""
    context1, nested1, dxf1 = _run_pipeline(
        _mixed_parts(), tmp_path, "run1", certified=True
    )
    context2, nested2, dxf2 = _run_pipeline(
        _mixed_parts(), tmp_path, "run2", certified=True
    )

    assert nested1["certified"] is True
    assert nested2["certified"] is True
    # 版本号是内容寻址的：同输入同环境 ⇒ 同一版本
    assert nested1["revision"]["revision_id"] == (
        nested2["revision"]["revision_id"]
    )
    assert nested1["revision"]["request_id"] == (
        nested2["revision"]["request_id"]
    )
    assert context1.active_revision_id == context2.active_revision_id
    assert dxf1.read_bytes() == dxf2.read_bytes()


def test_certified_flag_distinguishes_modes(tmp_path):
    """certified 标志必须在工具输出里可见：认证 True，探索 False。"""
    _context, nested_certified, _dxf = _run_pipeline(
        _mixed_parts(), tmp_path, "cert", certified=True
    )
    _context2, nested_exploratory, _dxf2 = _run_pipeline(
        _mixed_parts(), tmp_path, "explore", certified=False
    )

    assert nested_certified["certified"] is True
    assert nested_exploratory["certified"] is False
    assert "determinism_hazards" not in nested_certified


def test_certified_mode_rejects_deepnest(tmp_path):
    """认证范围当前只覆盖桥切：水刀/激光请求认证必须 fail closed。"""
    context = JobContext(
        nesting_profile=PROFILE_WATERJET,
        parts=_mixed_parts(),
        unit=UnitSystem.METRIC,
        output_dir=tmp_path / "out",
    )
    handlers = build_tool_handlers(context)
    handlers["confirm_numbering"]({"decision": "confirmed"})

    with pytest.raises(ValueError, match="certified mode"):
        handlers["nest_parts"]({
            "sheet_width": 1000,
            "sheet_height": 800,
            "certified": True,
        })


def test_certified_mode_stable_across_hash_seeds(tmp_path):
    """跨进程 + 不同 PYTHONHASHSEED：布局签名必须一致。

    这是"可复现"承诺里最难守的一类隐患（dict/set 迭代序依赖哈希种子），
    必须用两个独立进程验证，进程内重复运行测不出来。
    """
    script = (
        "import hashlib, json\n"
        "from shapely.geometry import Polygon\n"
        "from src.models import Part\n"
        "from src.nesting import nest_parts\n"
        "def rp(i, w, h):\n"
        "    p = Polygon([(0, 0), (w, 0), (w, h), (0, h)])\n"
        "    return Part(id=i, number=f'P-{i:04d}', polygon=p,"
        " outer_polygon=p)\n"
        "sizes = [(400, 300), (350, 250), (500, 200), (300, 300),"
        " (200, 180), (450, 320), (280, 260)]\n"
        "parts = [rp(i + 1, w, h) for i, (w, h) in enumerate(sizes)]\n"
        "stats = {}\n"
        "result = nest_parts(parts, 1000, 800, 20, improve_budget=0,"
        " trials=1, seed=0, certified=True, stats=stats)\n"
        "assert stats.get('certified') is True, stats\n"
        "sig = [[(p.number, p.polygon.wkt) for p in s.parts]"
        " for s in result.sheets]\n"
        "canonical = json.dumps(sig, ensure_ascii=False, sort_keys=True)\n"
        "print(hashlib.sha256(canonical.encode('utf-8')).hexdigest())\n"
    )
    repo_root = Path(__file__).resolve().parents[1]
    digests = []
    for hash_seed in ("0", "1"):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = hash_seed
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert completed.returncode == 0, completed.stderr
        digests.append(completed.stdout.strip())
    assert digests[0] == digests[1]
