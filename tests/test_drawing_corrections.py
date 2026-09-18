import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from shapely.geometry import Polygon

from src.agent_runtime import AgentRuntime
from src.constraints import PROFILE_QUICK
from src.correction_contracts import CorrectionPlan
from src.correction_planner import CorrectionPlanner, parse_correction_plan
from src.drawing_corrections import apply_correction_plan, build_correction_context
from src.job_context import JobContext
from src.models import Part
from src.tool_handlers import build_tool_handlers
from src.units import UnitSystem
from src.workflow_session import WorkflowStage


class FakeCorrectionClient:
    def __init__(self, response):
        self.response = response

    def complete_json(self, system_prompt, user_prompt):
        return self.response


def _geometry(size: float = 100.0) -> Polygon:
    return Polygon([(0, 0), (size, 0), (size, size), (0, size)])


def _context(tmp_path: Path) -> JobContext:
    source = tmp_path / "drawing.dxf"
    source.write_bytes(b"0\nEOF\n")
    parts = [
        Part(
            id=1,
            number="P-1",
            polygon=_geometry(),
            outer_polygon=_geometry(),
            original_number="P-1",
            material_group="01B",
            outer_handle="H1",
        ),
        Part(
            id=2,
            number="P-2",
            polygon=_geometry(80),
            outer_polygon=_geometry(80),
            original_number="P-2",
            material_group="01B",
            outer_handle="H2",
        ),
    ]
    parts_data = [
        {
            "index": part.id,
            "polygon": part.polygon,
            "outer_polygon": part.outer_polygon,
            "holes": [],
            "hole_handles": [],
            "original_number": part.original_number,
            "material_group": part.material_group,
            "area": part.area,
            "centroid": (50, 50),
            "outer_handle": part.outer_handle,
        }
        for part in parts
    ]
    return JobContext(
        dxf_path=str(source),
        project_id="drawing",
        unit=UnitSystem.METRIC,
        parts=parts,
        parts_data=parts_data,
        output_dir=tmp_path,
    )


def test_parse_correction_plan_validates_operations():
    plan = parse_correction_plan(
        {
            "operations": [
                {
                    "operation": "rename_number",
                    "target": "P-1",
                    "value": "P-1A",
                }
            ]
        },
        instruction="把 P-1 改成 P-1A",
    )

    assert plan.operations[0].value == "P-1A"

    with pytest.raises(ValidationError):
        parse_correction_plan(
            {
                "operations": [
                    {"operation": "rename_number", "target": "P-1"}
                ]
            },
            instruction="错误计划",
        )


def test_correction_planner_builds_context_prompt():
    client = FakeCorrectionClient(
        json.dumps(
            {
                "operations": [
                    {
                        "operation": "set_material",
                        "target": "P-2",
                        "value": "02B",
                    }
                ]
            }
        )
    )
    planner = CorrectionPlanner(client)

    plan = planner.plan(
        "P-2 的材料应为 02B",
        context={"parts": [{"number": "P-2"}]},
        target_ids=["P-2"],
    )

    assert plan.operations[0].operation == "set_material"


def test_build_correction_context_exposes_stable_targets(tmp_path):
    context = _context(tmp_path)

    payload = build_correction_context(context)

    assert payload["part_count"] == 2
    assert payload["parts"][0]["part_id"] == "1"
    assert payload["parts"][0]["number"] == "P-1"
    assert payload["parts"][0]["outer_handle"] == "H1"


def test_apply_correction_plan_regenerates_numbered_dxf(tmp_path):
    context = _context(tmp_path)
    plan = CorrectionPlan(
        instruction="修正编号和材料",
        operations=[
            {"operation": "rename_number", "target": "P-1", "value": "P-1A"},
            {"operation": "set_material", "target": "P-2", "value": "02B"},
        ],
    )

    result = apply_correction_plan(context, plan)

    assert result.revision == 1
    assert result.part_numbers == ["P-1A", "P-2"]
    assert context.correction_revision == 1
    assert context.numbered_dxf_path is not None
    assert context.numbered_dxf_path.exists()
    assert context.parts[0].number == "P-1A"
    assert context.parts[1].material_group == "02B"
    assert context.correction_history[0]["plan_id"] == plan.plan_id


def test_apply_correction_plan_rejects_duplicate_numbers_atomically(tmp_path):
    context = _context(tmp_path)
    plan = CorrectionPlan(
        instruction="错误改成重复编号",
        operations=[
            {"operation": "rename_number", "target": "P-1", "value": "P-2"}
        ],
    )

    with pytest.raises(ValueError, match="duplicate"):
        apply_correction_plan(context, plan)

    assert [part.number for part in context.parts] == ["P-1", "P-2"]
    assert context.correction_revision == 0
    assert context.numbered_dxf_path is None


def test_apply_correction_handler_removes_panel(tmp_path):
    context = _context(tmp_path)
    handlers = build_tool_handlers(context)
    plan = {
        "instruction": "删除误识别的 P-2",
        "operations": [
            {"operation": "remove_panel", "target": "P-2", "reason": "误识别"}
        ],
    }

    result = handlers["apply_drawing_corrections"]({"plan": plan})

    assert result["revision"] == 1
    assert result["removed_part_ids"] == [2]
    assert len(context.parts) == 1
    assert context.parts[0].number == "P-1"


def test_agent_runtime_correct_returns_to_numbering_gate(tmp_path):
    context = _context(tmp_path)
    runtime = AgentRuntime(
        context.dxf_path,
        1000,
        1000,
        20,
        UnitSystem.METRIC,
        PROFILE_QUICK,
        provider_name="queue",
        trials=1,
        seed=0,
        budget=0,
        quick=True,
        pairing=False,
        skip_unnumbered=True,
        output_dir=tmp_path,
    )
    runtime.context.parts = context.parts
    runtime.context.parts_data = context.parts_data
    runtime.session.stage = WorkflowStage.AUDITED
    plan = CorrectionPlan(
        instruction="把 P-1 改成 P-1A",
        operations=[
            {"operation": "rename_number", "target": "P-1", "value": "P-1A"}
        ],
    )

    result = runtime.correct(plan)

    assert result["revision"] == 1
    assert runtime.session.current_stage == "audited"
    assert runtime.last_result.confirmation_required is True
    assert runtime.context.parts[0].number == "P-1A"