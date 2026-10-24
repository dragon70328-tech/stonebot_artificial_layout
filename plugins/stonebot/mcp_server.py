"""Stonebot local MCP server.

This process is the model-independent integration boundary. Agent harnesses
speak MCP over STDIO; deterministic DXF, geometry and nesting work stays in the
existing Python modules.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import platform
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.constraints import PROFILES
from src.drawing_profile import load_profiles
from src.dxf_writer import write_numbered_parts_dxf
from src.units import UnitSystem
from src.workflow import prepare_drawing


SERVER_NAME = "stonebot-artificial-layout"
SERVER_VERSION = "0.1.0"
SUPPORTED_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")
DEFAULT_PROTOCOL = SUPPORTED_PROTOCOLS[-1]

SERVER_INSTRUCTIONS = (
    "Use Stonebot tools for DXF intake, material-aware part inspection, numbered "
    "in-place DXF export, and read-only nesting planning. Never treat an LLM "
    "response as confirmed geometry: inspect and confirm the structured tool "
    "result first. Keep different materials on separate boards. Do not claim "
    "nesting output exists unless a write tool returns its path. User-facing "
    "reports should be DOCX; do not create JSON deliverables."
)


class ToolInputError(ValueError):
    """Invalid arguments supplied to a Stonebot MCP tool."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_file(value: Any, name: str = "dxf_path") -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError(f"{name} must be a non-empty path string")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ToolInputError(f"{name} does not exist or is not a file: {path}")
    if path.suffix.lower() != ".dxf":
        raise ToolInputError(f"{name} must point to a .dxf file")
    return path


def _profile_by_name(name: str | None):
    if not name:
        return None
    profiles = load_profiles(PROJECT_ROOT / "drawing_profiles")
    for profile in profiles:
        if profile.name == name:
            return profile
    names = ", ".join(sorted(profile.name for profile in profiles))
    raise ToolInputError(
        f"Unknown drawing_profile {name!r}. Available profiles: {names}"
    )


def _prepare(
    dxf_path: Path,
    *,
    drawing_profile: str | None = None,
    layers: list[str] | None = None,
    exclude_layers: list[str] | None = None,
):
    output = io.StringIO()
    error_output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error_output):
        prepared = prepare_drawing(
            str(dxf_path),
            drawing_profile=_profile_by_name(drawing_profile),
            skip_unnumbered=False,
            layers=layers,
            exclude_layers=exclude_layers,
            reader_options=None,
            use_case_library=False,
        )
    log = (output.getvalue() + error_output.getvalue()).strip()
    if prepared.error:
        raise ToolInputError(prepared.error)
    return prepared, log


def _part_summary(prepared) -> dict[str, Any]:
    parts = prepared.parts
    sizes: Counter[tuple[float, float]] = Counter()
    for part in parts:
        min_x, min_y, max_x, max_y = part.outer_polygon.bounds
        long_side = round(max(max_x - min_x, max_y - min_y), 1)
        short_side = round(min(max_x - min_x, max_y - min_y), 1)
        sizes[(long_side, short_side)] += 1
    numbers = [part.number for part in parts]
    return {
        "parts": len(parts),
        "holes": sum(len(part.holes) for part in parts),
        "unique_numbers": len(set(numbers)),
        "net_area_m2": round(sum(part.area for part in parts) / 1_000_000, 4),
        "material_groups": dict(sorted(Counter(
            part.material_group or "UNASSIGNED"
            for part in parts
        ).items())),
        "size_classes": [
            {
                "length_mm": size[0],
                "width_mm": size[1],
                "quantity": quantity,
            }
            for size, quantity in sorted(
                sizes.items(),
                key=lambda item: (-item[1], -item[0][0], -item[0][1]),
            )[:20]
        ],
    }


def tool_health(_arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "ok",
        "server": SERVER_NAME,
        "version": SERVER_VERSION,
        "project_root": str(PROJECT_ROOT),
        "python": platform.python_version(),
        "tools": [item["name"] for item in TOOL_DEFINITIONS],
    }


def tool_list_profiles(_arguments: dict[str, Any]) -> dict[str, Any]:
    profiles = load_profiles(PROJECT_ROOT / "drawing_profiles")
    return {
        "profiles": [
            {
                "name": profile.name,
                "panel_layers": (
                    profile.panel_layers
                    or ([profile.panel_layer] if profile.panel_layer else [])
                ),
                "number_layers": profile.number_layers,
                "material_group_enabled": profile.material_group_enabled,
            }
            for profile in profiles
        ]
    }


def tool_inspect_dxf(arguments: dict[str, Any]) -> dict[str, Any]:
    dxf_path = _require_file(arguments.get("dxf_path"))
    layers = arguments.get("layers")
    exclude_layers = arguments.get("exclude_layers")
    if layers is not None and not isinstance(layers, list):
        raise ToolInputError("layers must be an array of layer names")
    if exclude_layers is not None and not isinstance(exclude_layers, list):
        raise ToolInputError("exclude_layers must be an array of layer names")
    prepared, log = _prepare(
        dxf_path,
        drawing_profile=arguments.get("drawing_profile"),
        layers=layers,
        exclude_layers=exclude_layers,
    )
    return {
        "dxf_path": str(dxf_path),
        "sha256": _sha256(dxf_path),
        "bytes": dxf_path.stat().st_size,
        "drawing_profile": (
            prepared.drawing_profile.name
            if prepared.drawing_profile is not None else None
        ),
        "summary": _part_summary(prepared),
        "diagnostics": log.splitlines()[-20:],
    }


def tool_export_numbered(arguments: dict[str, Any]) -> dict[str, Any]:
    dxf_path = _require_file(arguments.get("dxf_path"))
    prepared, log = _prepare(
        dxf_path,
        drawing_profile=arguments.get("drawing_profile"),
        layers=arguments.get("layers"),
        exclude_layers=arguments.get("exclude_layers"),
    )
    output_value = arguments.get("output_path")
    if output_value:
        output_path = Path(output_value).expanduser().resolve()
    else:
        output_path = dxf_path.with_name(f"{dxf_path.stem}_numbered_in_place.dxf")
    if output_path.suffix.lower() != ".dxf":
        raise ToolInputError("output_path must end with .dxf")
    if output_path.exists() and not bool(arguments.get("overwrite", False)):
        raise ToolInputError(
            f"Refusing to overwrite existing file without overwrite=true: {output_path}"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_numbered_parts_dxf(prepared.parts, str(output_path))
    return {
        "output_path": str(output_path),
        "sha256": _sha256(output_path),
        "summary": _part_summary(prepared),
        "diagnostics": log.splitlines()[-20:],
    }


def tool_plan_nesting(arguments: dict[str, Any]) -> dict[str, Any]:
    dxf_path = _require_file(arguments.get("dxf_path"))
    width = float(arguments.get("width_mm", 3200.0))
    height = float(arguments.get("height_mm", 1600.0))
    thickness = float(arguments.get("thickness_mm", 20.0))
    if width <= 0 or height <= 0 or thickness <= 0:
        raise ToolInputError("Board dimensions and thickness must be positive")

    profile_name = str(arguments.get("profile", "min_sheets"))
    profile = PROFILES.get(profile_name)
    if profile is None:
        raise ToolInputError(
            f"Unknown nesting profile {profile_name!r}. "
            f"Available: {', '.join(sorted(PROFILES))}"
        )
    process = str(arguments.get("process", "bridge"))
    if process not in {"bridge", "waterjet_laser"}:
        raise ToolInputError("process must be bridge or waterjet_laser")
    profile = profile.with_overrides(processing_class=process)

    rotation = arguments.get("rotation")
    if rotation is not None:
        if not isinstance(rotation, list) or any(
            float(value) not in {0.0, 90.0, 180.0, 270.0}
            for value in rotation
        ):
            raise ToolInputError("rotation must be an array of 0/90/180/270")
        profile = profile.with_overrides(rotation=[int(value) for value in rotation])

    from main import run as run_main

    output = io.StringIO()
    error_output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error_output):
            result = run_main(
                str(dxf_path),
                width,
                height,
                thickness,
                UnitSystem.METRIC,
                trials=int(arguments.get("trials", 1)),
                seed=int(arguments.get("seed", 0)),
                budget=float(arguments.get("budget_s", 60.0)),
                skip_unnumbered=False,
                layers=arguments.get("layers"),
                exclude_layers=arguments.get("exclude_layers"),
                drawing_profile=_profile_by_name(arguments.get("drawing_profile")),
                profile=profile,
                confirm_sheet_count=False,
                report_only=True,
                special_size=None,
                check_only=False,
                quick=bool(arguments.get("quick", True)),
                kerf_mm=float(arguments.get("kerf_mm", 0.0)),
                sheet_oversize=(
                    float(arguments.get("oversize_x_mm", 0.0)),
                    float(arguments.get("oversize_y_mm", 0.0)),
                ),
                output_root=None,
                auto_review=bool(arguments.get("auto_review", False)),
                use_template=False,
                cold_start=True,
                beam=True,
                beam_width=1000,
                beam_budget=float(arguments.get("beam_budget_s", 60.0)),
                pattern_reuse=4,
                parallel_workers=1,
                compress=True,
                rect_fast=bool(arguments.get("rect_fast", True)),
                material_filter=arguments.get("material_filter"),
            )
    except SystemExit as exc:
        details = output.getvalue() + error_output.getvalue()
        raise RuntimeError(
            f"Nesting plan stopped with exit code {exc.code}: "
            f"{details.strip()[-2000:]}"
        ) from exc
    return {
        "plan": result,
        "dxf_path": str(dxf_path),
        "profile": profile_name,
        "process": process,
        "diagnostics": (
            output.getvalue() + error_output.getvalue()
        ).strip().splitlines()[-30:],
        "wrote_files": False,
    }


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "stonebot_health",
        "title": "Stonebot plugin health",
        "description": "Report plugin version, project root and available tools.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "stonebot_list_profiles",
        "title": "List drawing profiles",
        "description": "List available local DXF drawing profiles.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "stonebot_inspect_dxf",
        "title": "Inspect DXF",
        "description": (
            "Read a DXF and return deterministic panel, hole, numbering, material "
            "and size summaries. This does not write files."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["dxf_path"],
            "properties": {
                "dxf_path": {"type": "string"},
                "drawing_profile": {"type": "string"},
                "layers": {"type": "array", "items": {"type": "string"}},
                "exclude_layers": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "stonebot_export_numbered",
        "title": "Export numbered in-place DXF",
        "description": (
            "Read a DXF, assign stable numbers, and write one numbered in-place "
            "DXF for user confirmation. No JSON deliverable is created."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["dxf_path"],
            "properties": {
                "dxf_path": {"type": "string"},
                "output_path": {"type": "string"},
                "overwrite": {"type": "boolean", "default": False},
                "drawing_profile": {"type": "string"},
                "layers": {"type": "array", "items": {"type": "string"}},
                "exclude_layers": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "stonebot_plan_nesting",
        "title": "Plan nesting",
        "description": (
            "Run a read-only nesting calculation and return board count, yield "
            "and material summary. No DXF or JSON files are written."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["dxf_path"],
            "properties": {
                "dxf_path": {"type": "string"},
                "drawing_profile": {"type": "string"},
                "layers": {"type": "array", "items": {"type": "string"}},
                "exclude_layers": {"type": "array", "items": {"type": "string"}},
                "width_mm": {"type": "number", "default": 3200},
                "height_mm": {"type": "number", "default": 1600},
                "thickness_mm": {"type": "number", "default": 20},
                "process": {"enum": ["bridge", "waterjet_laser"], "default": "bridge"},
                "profile": {"type": "string", "default": "min_sheets"},
                "rotation": {"type": "array", "items": {"type": "number"}},
                "kerf_mm": {"type": "number", "default": 0},
                "oversize_x_mm": {"type": "number", "default": 0},
                "oversize_y_mm": {"type": "number", "default": 0},
                "budget_s": {"type": "number", "default": 60},
                "beam_budget_s": {"type": "number", "default": 60},
                "trials": {"type": "integer", "default": 1},
                "seed": {"type": "integer", "default": 0},
                "quick": {"type": "boolean", "default": True},
                "rect_fast": {"type": "boolean", "default": True},
                "auto_review": {"type": "boolean", "default": False},
                "material_filter": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
]

TOOL_HANDLERS = {
    "stonebot_health": tool_health,
    "stonebot_list_profiles": tool_list_profiles,
    "stonebot_inspect_dxf": tool_inspect_dxf,
    "stonebot_export_numbered": tool_export_numbered,
    "stonebot_plan_nesting": tool_plan_nesting,
}


def _protocol_version(requested: Any) -> str:
    return requested if requested in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL


def _write_message(payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(data + b"\n")
    sys.stdout.buffer.flush()


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

    if request_id is None:
        return None
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": _protocol_version(
                    params.get("protocolVersion")
                ),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
                "instructions": SERVER_INSTRUCTIONS,
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": TOOL_DEFINITIONS},
        }
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32602,
                    "message": f"Unknown tool: {name}",
                },
            }
        try:
            result = handler(arguments)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{
                        "type": "text",
                        "text": json.dumps(result, ensure_ascii=False, indent=2),
                    }],
                    "structuredContent": result,
                    "isError": False,
                },
            }
        except Exception as exc:  # Return tool failures to the agent.
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{
                        "type": "text",
                        "text": f"{type(exc).__name__}: {exc}",
                    }],
                    "isError": True,
                },
            }
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _read_message():
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    if line.lower().startswith(b"content-length:"):
        length = int(line.split(b":", 1)[1].strip())
        while True:
            header = sys.stdin.buffer.readline()
            if header in (b"\n", b"\r\n", b""):
                break
        body = sys.stdin.buffer.read(length)
        return json.loads(body.decode("utf-8"))
    return json.loads(line.decode("utf-8"))


def serve() -> None:
    while True:
        try:
            message = _read_message()
            if message is None:
                return
            response = handle_request(message)
            if response is not None:
                _write_message(response)
        except json.JSONDecodeError as exc:
            _write_message({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            })
        except Exception as exc:
            _write_message({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": str(exc)},
            })


if __name__ == "__main__":
    serve()
