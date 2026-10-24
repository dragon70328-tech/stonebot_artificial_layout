from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "plugins" / "stonebot" / "mcp_server.py"


def _load_server():
    spec = importlib.util.spec_from_file_location("stonebot_mcp_server", SERVER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_initialize_and_list_tools():
    server = _load_server()
    response = server.handle_request({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18"},
    })
    assert response["result"]["serverInfo"]["name"] == "stonebot-artificial-layout"
    tools = server.handle_request({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    })["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert {
        "stonebot_health",
        "stonebot_inspect_dxf",
        "stonebot_export_numbered",
        "stonebot_plan_nesting",
    } <= names


def test_inspect_validated_dxf():
    server = _load_server()
    result = server.tool_inspect_dxf({
        "dxf_path": str(ROOT / "tests" / "fixtures" / "dxf" / "CGR45final.dxf"),
        "drawing_profile": "cgr45",
    })
    assert result["summary"]["parts"] == 203
    assert result["summary"]["holes"] == 306
    assert result["summary"]["unique_numbers"] == 203


def test_export_numbered_is_dxf_only(tmp_path):
    server = _load_server()
    output = tmp_path / "numbered.dxf"
    result = server.tool_export_numbered({
        "dxf_path": str(
            ROOT / "tests" / "fixtures" / "dxf" / "雪花白洗手台（小利）1.8.dxf"
        ),
        "drawing_profile": "snow_white_basin",
        "output_path": str(output),
    })
    assert Path(result["output_path"]).is_file()
    assert output.suffix == ".dxf"
    assert not list(tmp_path.glob("*.json"))


def test_nesting_plan_is_read_only(tmp_path):
    server = _load_server()
    result = server.tool_plan_nesting({
        "dxf_path": str(
            ROOT / "tests" / "fixtures" / "dxf" / "雪花白洗手台（小利）1.8.dxf"
        ),
        "drawing_profile": "snow_white_basin",
        "width_mm": 3200,
        "height_mm": 1600,
        "thickness_mm": 20,
        "process": "bridge",
        "profile": "min_sheets",
        "kerf_mm": 5,
        "oversize_x_mm": 30,
        "oversize_y_mm": 30,
        "budget_s": 0,
        "beam_budget_s": 0,
        "quick": True,
    })
    assert result["wrote_files"] is False
    assert result["plan"]["report_only"] is True
    assert result["plan"]["total_sheets"] >= 1
    assert not list(tmp_path.iterdir())


def test_stdio_initialize_round_trip():
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1"},
        },
    }
    process = subprocess.Popen(
        [sys.executable, "-u", "-X", "utf8", str(SERVER)],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        stdout, stderr = process.communicate(
            json.dumps(request, ensure_ascii=False) + "\n",
            timeout=20,
        )
    finally:
        if process.poll() is None:
            process.kill()
    response = json.loads(stdout.splitlines()[0])
    assert response["result"]["protocolVersion"] == "2025-06-18"
    assert response["result"]["capabilities"]["tools"]["listChanged"] is False
