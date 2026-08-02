# -*- coding: utf-8 -*-
"""排板工作流引擎 - 管理对话状态，调度 LLM 和后端操作"""

import json
import os
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
sys.path.insert(0, str(PROJECT_ROOT))


@dataclass
class SessionState:
    sheet_width: float = 0.0
    sheet_height: float = 0.0
    sheet_thickness: float = 18.0
    unit: str = "metric"
    dxf_path: Optional[str] = None
    numbered_dxf_path: Optional[str] = None
    numbering_mode: str = "keep_original"
    number_format: str = "P-{index:04d}"
    step: str = "idle"
    messages: list = field(default_factory=list)


def _generate_numbered_dxf(source_path, numbering_mode, number_format):
    from src.dxf_reader import read_dxf
    from src.numbering import assign_numbers
    from src.dxf_writer import write_numbered_parts_dxf

    parts_data, _doc = read_dxf(source_path)
    if numbering_mode == "new":
        parts = assign_numbers(parts_data, force_renumber=True,
                              number_format=number_format)
    else:
        parts = assign_numbers(parts_data)

    out_path = str(Path(source_path).parent /
                   f"{Path(source_path).stem}_numbered.dxf")
    write_numbered_parts_dxf(parts, out_path)
    return out_path


def _run_nesting(dxf_path, sheet_w, sheet_h, thickness=18.0,
                unit="metric", improve_budget=40.0):
    from src.dxf_reader import read_dxf
    from src.numbering import assign_numbers
    from src.nesting import nest_parts, validate_nesting
    from src.dxf_writer import write_nested_dxf

    parts_data, _doc = read_dxf(dxf_path)
    parts = assign_numbers(parts_data)
    result = nest_parts(parts, sheet_w, sheet_h, thickness,
                       unit=unit, improve_budget=improve_budget)
    errors = validate_nesting(result, sheet_w, sheet_h)

    basename = Path(dxf_path).stem
    out_dxf = str(OUTPUT_DIR / f"{basename}_nested_{sheet_w:.0f}x{sheet_h:.0f}.dxf")
    out_json = str(OUTPUT_DIR / f"{basename}_report_{sheet_w:.0f}x{sheet_h:.0f}.json")
    write_nested_dxf(result, out_dxf, unit_system=unit)

    report = {
        "sheets": result.total_sheets,
        "parts": result.total_parts,
        "yield": round(result.yield_rate, 2),
        "sheet_area": result.total_sheet_area,
        "part_area": result.total_part_area,
        "errors": len(errors),
        "dxf_output": out_dxf,
        "json_output": out_json,
    }
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return report


SYSTEM_PROMPT = """\
You are an AI assistant for a stone countertop nesting system.
Guide the user through the workflow:
1. Ask for slab dimensions (width x height x thickness in mm)
2. Ask user to upload the DXF file with all parts
3. Ask whether to generate a numbered-parts DXF for inspection
4. If yes: ask whether to keep original numbers or renumber. If renumber, ask for format
5. Generate the numbered DXF, tell user the path, wait for their check
6. If user requests modifications, explain what can be changed
7. When user is satisfied, they say to start nesting
8. Run nesting, report: sheet count, yield rate, output file paths

Rules: one question at a time. Short, professional Chinese replies.
"""


def _get_llm_client():
    api_key = os.environ.get("OPENAI_API_KEY", "")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com")
    if not api_key:
        return None
    try:
        import openai
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        return openai.OpenAI(**kwargs)
    except Exception:
        return None


def llm_chat(state, user_message):
    client = _get_llm_client()
    if client is None:
        return _rule_based_response(state, user_message)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in state.messages:
        messages.append(m)
    messages.append({"role": "user", "content": user_message})

    tools = [{
        "type": "function",
        "function": {
            "name": "generate_numbered_dxf",
            "description": "Generate a numbered parts DXF for inspection",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_path": {"type": "string"},
                    "numbering_mode": {"type": "string", "enum": ["keep_original", "new"]},
                    "number_format": {"type": "string"},
                },
                "required": ["source_path"],
            },
        },
    }, {
        "type": "function",
        "function": {
            "name": "run_nesting",
            "description": "Execute the nesting algorithm",
            "parameters": {
                "type": "object",
                "properties": {
                    "dxf_path": {"type": "string"},
                    "sheet_width": {"type": "number"},
                    "sheet_height": {"type": "number"},
                    "sheet_thickness": {"type": "number"},
                },
                "required": ["dxf_path", "sheet_width", "sheet_height", "sheet_thickness"],
            },
        },
    }]

    try:
        model = os.environ.get("OPENAI_MODEL", "deepseek-chat")
        response = client.chat.completions.create(
            model=model, messages=messages, tools=tools,
            tool_choice="auto", temperature=0.3,
        )
        msg = response.choices[0].message

        if msg.tool_calls:
            for tc in msg.tool_calls:
                fn = tc.function
                args = json.loads(fn.arguments)

                if fn.name == "generate_numbered_dxf":
                    out = _generate_numbered_dxf(
                        args.get("source_path", state.dxf_path or ""),
                        args.get("numbering_mode", state.numbering_mode),
                        args.get("number_format", state.number_format),
                    )
                    state.numbered_dxf_path = out
                    state.numbering_mode = args.get("numbering_mode", state.numbering_mode)
                    state.number_format = args.get("number_format", state.number_format)
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                    "content": f"DXF generated: {out}"})

                elif fn.name == "run_nesting":
                    report = _run_nesting(
                        args.get("dxf_path", state.dxf_path or ""),
                        args.get("sheet_width", state.sheet_width),
                        args.get("sheet_height", state.sheet_height),
                        args.get("sheet_thickness", state.sheet_thickness),
                        unit=state.unit,
                    )
                    state.step = "done"
                    result_text = (
                        f"排板完成!\n"
                        f"- 使用 {report['sheets']} 张大板\n"
                        f"- 出材率 {report['yield']}%\n"
                        f"- 结果文件: {report['dxf_output']}\n"
                        f"- 报告文件: {report['json_output']}"
                    )
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                    "content": result_text})

            followup = client.chat.completions.create(
                model=model, messages=messages, temperature=0.3,
            )
            text = followup.choices[0].message.content or ""
        else:
            text = msg.content or ""

        state.messages.append({"role": "user", "content": user_message})
        state.messages.append({"role": "assistant", "content": text})
        return text

    except Exception as e:
        traceback.print_exc()
        return _rule_based_response(state, user_message)


# ---------------------------------------------------------------------------
# Rule-based fallback
# ---------------------------------------------------------------------------

def _rule_based_response(state, user_message):
    msg = user_message.strip().lower()

    if state.step == "idle":
        if "排板" in msg or "nest" in msg:
            state.step = "dimensions"
            return "请输入大板尺寸（宽 高 厚，单位mm），例如：3200 1800 18"
        return "欢迎使用人造石排板系统！输入“排板”或点击排板按钮开始。"

    if state.step == "dimensions":
        parts = msg.split()
        if len(parts) >= 2:
            try:
                state.sheet_width = float(parts[0])
                state.sheet_height = float(parts[1])
                state.sheet_thickness = float(parts[2]) if len(parts) >= 3 else 18.0
                state.step = "dxf"
                return f"大板尺寸已确认：{state.sheet_width:.0f} x {state.sheet_height:.0f} x {state.sheet_thickness:.0f} mm。请上传规格板DXF文件。"
            except ValueError:
                pass
        return "格式不正确。请输入三个数字（宽 高 厚），例如：3200 1800 18"

    if state.step == "dxf":
        state.step = "numbered"
        return "DXF文件已接收。是否需要生成带编号的纯规格板DXF文件以便检查？（是/否）"

    if state.step == "numbered":
        if msg in ("yes", "y", "是", "要", "需要"):
            state.step = "numbering_mode"
            return "请选择编号规则：\n1 - 保留原编号\n2 - 重新编号（默认 P-0001）"
        else:
            state.step = "check"
            return "请检查规格板文件。满意后输入“开始排板”，如需修改请说明。"

    if state.step == "numbering_mode":
        if msg in ("1", "keep", "original", "保留", "原编号"):
            state.numbering_mode = "keep_original"
            state.step = "check"
            if state.dxf_path:
                try:
                    out = _generate_numbered_dxf(state.dxf_path, "keep_original", "")
                    state.numbered_dxf_path = out
                    return f"带编号的规格板DXF已生成：{out}\n请检查。满意后输入“开始排板”，如需修改请说明。"
                except Exception as e:
                    return f"生成失败：{e}"
        elif msg in ("2", "renumber", "new", "重新", "新编号"):
            state.numbering_mode = "new"
            state.step = "number_format"
            return "请输入编号格式（默认 P-{index:04d}），例如：P-{index:04d} 或 A-{index:03d}"
        return "请选择 1（保留原编号）或 2（重新编号）"

    if state.step == "number_format":
        fmt = msg.strip() or "P-{index:04d}"
        state.number_format = fmt
        if state.dxf_path:
            try:
                out = _generate_numbered_dxf(state.dxf_path, "new", fmt)
                state.numbered_dxf_path = out
                state.step = "check"
                return f"带编号的规格板DXF已生成（格式：{fmt}）：{out}\n请检查。满意后输入“开始排板”，如需修改请说明。"
            except Exception as e:
                return f"生成失败：{e}"
        state.step = "check"
        return f"编号格式已设为 {fmt}。规格板DXF已生成，请检查。满意后输入“开始排板”，如需修改请说明。"

    if state.step == "check":
        if "nesting" in msg or "start" in msg or "排板" in msg or "满意" in msg or msg == "ok":
            state.step = "nesting"
            dxf = state.numbered_dxf_path or state.dxf_path
            if not dxf:
                return "错误：未找到规格板DXF文件。"
            try:
                report = _run_nesting(dxf, state.sheet_width, state.sheet_height,
                                     state.sheet_thickness, state.unit)
                state.step = "done"
                return (f"排板完成！\n"
                        f"- 使用了 {report['sheets']} 张大板（{state.sheet_width:.0f}x{state.sheet_height:.0f}mm）\n"
                        f"- 共 {report['parts']} 块规格板\n"
                        f"- 出材率：{report['yield']}%\n"
                        f"- 排板DXF：{report['dxf_output']}\n"
                        f"- 报告JSON：{report['json_output']}")
            except Exception as e:
                return f"排板失败：{e}"
        return "规格板DXF如有问题请说明具体修改要求。满意后输入“开始排板”。"

    return "请确认当前状态，或输入“排板”重新开始。"
