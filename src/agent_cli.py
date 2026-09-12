"""Local CLI agent loop.

This is the first human-facing consumer of the agent architecture. It keeps
the existing deterministic tool chain, but drives it through AgentLoop and
pauses at the three confirmation gates.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from src.ai_planning import ToolCall
from src.llm_client import LLMError
from src.llm_plan_provider import LLMPlanProvider
from src.openai_llm_client import OpenAICompatibleClient
from src.units import UnitSystem
from src.workflow_session import WorkflowSession, WorkflowStage


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class QueuePlanProvider:
    def __init__(self) -> None:
        self.queue: list[list[ToolCall]] = []

    def enqueue(self, plan: list[ToolCall]) -> None:
        self.queue.append(plan)

    def plan(self, session: WorkflowSession, context: Any) -> list[ToolCall]:
        if not self.queue:
            return []
        return self.queue.pop(0)


def build_agent_plan_provider(provider_name: str):
    if provider_name == "llm":
        client = OpenAICompatibleClient()
        if not client.api_key:
            raise LLMError(
                "OPENAI_API_KEY is not configured for --agent-provider llm"
            )
        return LLMPlanProvider(client)
    if provider_name != "queue":
        raise ValueError(f"unknown agent provider: {provider_name}")
    return QueuePlanProvider()


def _make_output_dir(dxf_path: str) -> Path:
    stem = Path(dxf_path).stem
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = PROJECT_ROOT / "output" / f"{timestamp}_{stem}"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def build_initial_plan(
    dxf_path: str,
    numbered_path: str,
    *,
    skip_unnumbered: bool,
) -> list[ToolCall]:
    return [
        ToolCall("analyze_drawing", {"path": dxf_path}),
        ToolCall("match_profile", {"path": dxf_path}),
        ToolCall("read_dxf", {"path": dxf_path}),
        ToolCall("audit_drawing", {"path": dxf_path}),
        ToolCall(
            "assign_numbers",
            {"path": dxf_path, "skip_unnumbered": skip_unnumbered},
        ),
        ToolCall("write_numbered_dxf", {"path": numbered_path}),
    ]


def plan_after_confirmation(
    stage: str,
    *,
    width_mm: float,
    height_mm: float,
    thickness_mm: float,
    nested_path: str,
    report_path: str,
    trials: int,
    seed: int,
    budget: float,
    quick: bool,
    pairing: bool,
) -> list[ToolCall]:
    if stage == WorkflowStage.AUDITED.value:
        return [
            ToolCall("confirm_numbering", {"decision": "confirmed"}),
            ToolCall(
                "nest_parts",
                {
                    "sheet_width": width_mm,
                    "sheet_height": height_mm,
                    "thickness": thickness_mm,
                    "trials": trials,
                    "seed": seed,
                    "budget": budget,
                    "quick": quick,
                    "pairing": pairing,
                },
            ),
        ]
    if stage == WorkflowStage.NESTED_REPORTED.value:
        return [ToolCall("postprocess", {"result": {}})]
    if stage == WorkflowStage.POSTPROCESS_CONFIRMED.value:
        return [
            ToolCall("write_dxf", {"path": nested_path}),
            ToolCall("write_report", {"path": report_path}),
        ]
    return []


def run_agent_mode(
    dxf_path: str,
    width_mm: float,
    height_mm: float,
    thickness_mm: float,
    unit: UnitSystem,
    profile,
    agent_provider: str = "queue",
    *,
    trials: int,
    seed: int,
    budget: float,
    quick: bool,
    pairing: bool,
    skip_unnumbered: bool,
) -> int:
    from src.agent_runtime import AgentRuntime

    output_dir = _make_output_dir(dxf_path)
    try:
        runtime = AgentRuntime(
            dxf_path,
            width_mm,
            height_mm,
            thickness_mm,
            unit,
            profile,
            provider_name=agent_provider,
            trials=trials,
            seed=seed,
            budget=budget,
            quick=quick,
            pairing=pairing,
            skip_unnumbered=skip_unnumbered,
            output_dir=output_dir,
        )
    except (LLMError, ValueError) as exc:
        print(f"[agent] {exc}")
        return 1

    result = runtime.run_next()
    while True:
        print(f"[agent] stage={result.stage} blocked={result.blocked} "
              f"completed={result.completed}")

        if result.blocked:
            print("Agent loop blocked.")
            return 1
        if result.completed:
            print(f"DXF: {runtime.nested_path}")
            print(f"报告: {runtime.report_path}")
            return 0
        if not result.confirmation_required:
            print("Agent loop stopped without a confirmation gate or terminal state.")
            return 1

        if result.stage == WorkflowStage.AUDITED.value:
            prompt = "编号确认通过后开始排板？[y/N]: "
        elif result.stage == WorkflowStage.NESTED_REPORTED.value:
            prompt = "是否接受当前板数并开始后处理？[y/N]: "
        elif result.stage == WorkflowStage.POSTPROCESS_CONFIRMED.value:
            prompt = "后处理检查通过，是否生成最终 DXF/报告？[y/N]: "
        else:
            prompt = "是否继续？[y/N]: "

        answer = input(prompt).strip().lower()
        if answer not in ("y", "yes"):
            runtime.cancel()
            print("已取消。")
            return 1

        result = runtime.confirm("yes")
