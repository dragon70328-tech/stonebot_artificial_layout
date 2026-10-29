"""User-controlled delivery and optional Word reporting for plugin jobs."""

from __future__ import annotations

import html
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile


DELIVERY_ARTIFACTS = ("numbered_dxf", "nested_dxf")


def choose_directory_dialog(title: str) -> str | None:
    """Open a local native folder picker and return one absolute path."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise RuntimeError("当前 Python 环境没有可用的目录选择器") from exc

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
        root.update_idletasks()
        selected = filedialog.askdirectory(
            parent=root,
            title=title,
            mustexist=True,
        )
    finally:
        root.destroy()
    if not selected:
        return None
    return str(Path(selected).resolve())


def resolve_output_directory(value: str | Path) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("必须选择保存文件夹")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("保存文件夹必须是绝对路径")
    path = path.resolve()
    if not path.is_dir():
        raise ValueError(f"保存文件夹不存在: {path}")
    return path


def _atomic_copy(
    source: Path,
    target: Path,
    *,
    overwrite: bool,
) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"待交付文件不存在: {source}")
    if target.exists() and not overwrite:
        raise FileExistsError(f"目标文件已存在: {target}")
    temporary = target.with_name(
        f".{target.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def deliver_dxf_artifacts(
    *,
    numbered_source: str | Path,
    nested_source: str | Path,
    output_directory: str | Path,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Copy only the in-place and nested DXFs to a user-selected folder."""
    target_dir = resolve_output_directory(output_directory)
    sources = {
        "numbered_dxf": Path(numbered_source).resolve(),
        "nested_dxf": Path(nested_source).resolve(),
    }
    targets = {
        key: target_dir / source.name
        for key, source in sources.items()
    }
    collisions = [
        str(path)
        for path in targets.values()
        if path.exists() and not overwrite
    ]
    if collisions:
        raise FileExistsError(
            "保存目录已有同名文件，请更换目录或明确允许覆盖: "
            + ", ".join(collisions)
        )
    for key in DELIVERY_ARTIFACTS:
        _atomic_copy(
            sources[key],
            targets[key],
            overwrite=overwrite,
        )
    return {
        "output_directory": str(target_dir),
        "artifacts": {
            key: str(targets[key])
            for key in DELIVERY_ARTIFACTS
        },
        "file_names": [
            targets[key].name
            for key in DELIVERY_ARTIFACTS
        ],
    }


def _xml_text(value: Any) -> str:
    return html.escape(str(value), quote=False)


def _run(text: Any, *, bold: bool = False) -> str:
    rpr = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return (
        "<w:r>"
        f"{rpr}<w:t xml:space=\"preserve\">{_xml_text(text)}</w:t>"
        "</w:r>"
    )


def _paragraph(
    text: Any = "",
    *,
    style: str | None = None,
    bold: bool = False,
) -> str:
    ppr = (
        f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
        if style
        else ""
    )
    return f"<w:p>{ppr}{_run(text, bold=bold)}</w:p>"


def _cell(text: Any, *, bold: bool = False) -> str:
    return (
        "<w:tc><w:tcPr><w:tcW w:w=\"0\" w:type=\"auto\"/>"
        "</w:tcPr>"
        f"{_paragraph(text, bold=bold)}"
        "</w:tc>"
    )


def _table(rows: list[list[Any]]) -> str:
    if not rows:
        return ""
    body = []
    for row_index, row in enumerate(rows):
        cells = "".join(
            _cell(value, bold=row_index == 0)
            for value in row
        )
        body.append(f"<w:tr>{cells}</w:tr>")
    return (
        "<w:tbl>"
        "<w:tblPr><w:tblStyle w:val=\"TableGrid\"/>"
        "<w:tblW w:w=\"0\" w:type=\"auto\"/></w:tblPr>"
        "<w:tblGrid/>"
        + "".join(body)
        + "</w:tbl>"
    )


def _styles_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults>
    <w:rPrDefault>
      <w:rPr>
        <w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:eastAsia="等线"/>
        <w:sz w:val="21"/>
        <w:lang w:val="zh-CN" w:eastAsia="zh-CN"/>
      </w:rPr>
    </w:rPrDefault>
  </w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Title">
    <w:name w:val="Title"/>
    <w:basedOn w:val="Normal"/>
    <w:rPr><w:b/><w:sz w:val="36"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:basedOn w:val="Normal"/>
    <w:next w:val="Normal"/>
    <w:rPr><w:b/><w:sz w:val="28"/></w:rPr>
  </w:style>
  <w:style w:type="table" w:styleId="TableGrid">
    <w:name w:val="Table Grid"/>
    <w:tblPr>
      <w:tblBorders>
        <w:top w:val="single" w:sz="4" w:color="B7B7B7"/>
        <w:left w:val="single" w:sz="4" w:color="B7B7B7"/>
        <w:bottom w:val="single" w:sz="4" w:color="B7B7B7"/>
        <w:right w:val="single" w:sz="4" w:color="B7B7B7"/>
        <w:insideH w:val="single" w:sz="4" w:color="D9D9D9"/>
        <w:insideV w:val="single" w:sz="4" w:color="D9D9D9"/>
      </w:tblBorders>
    </w:tblPr>
  </w:style>
</w:styles>
"""


def _report_document(context: Any, job_id: str) -> str:
    result = context.result
    if result is None:
        raise ValueError("任务没有可报告的排板结果")
    outcome = context.nesting_outcome
    process = getattr(
        getattr(context.process_policy, "process", None),
        "value",
        getattr(context.process_policy, "process", None),
    )
    generated_at = datetime.now(timezone.utc).astimezone().strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    body = [
        _paragraph("人造石排板报告", style="Title"),
        _paragraph(f"生成时间：{generated_at}"),
        _paragraph(f"任务编号：{job_id}"),
        _paragraph(f"源图纸：{context.dxf_path}"),
        _paragraph("一、排板摘要", style="Heading1"),
        _table(
            [
                ["项目", "结果"],
                ["规格件数量", result.total_parts],
                ["大板数量", result.total_sheets],
                ["出材率", f"{result.yield_rate:.2f}%"],
                [
                    "大板尺寸",
                    (
                        f"{context.width_mm:g} × "
                        f"{context.height_mm:g} mm"
                    ),
                ],
                ["厚度", f"{context.thickness_mm:g} mm"],
                ["加工方式", process or ""],
                ["验证错误", len(context.validation_errors)],
            ]
        ),
        _paragraph("二、每张板明细", style="Heading1"),
        _table(
            [
                ["板号", "尺寸(mm)", "件数", "零件编号"],
                *[
                    [
                        sheet.index,
                        f"{sheet.width:g} × {sheet.height:g}",
                        len(sheet.parts),
                        "、".join(part.number for part in sheet.parts),
                    ]
                    for sheet in result.sheets
                ],
            ]
        ),
    ]
    material_summary = getattr(outcome, "material_summary", None)
    if material_summary:
        rows = [["材料", "板数", "件数", "数据"]]
        if isinstance(material_summary, dict):
            entries = sorted(material_summary.items())
        else:
            entries = [
                (
                    str(item.get("material") or item.get("name") or ""),
                    item,
                )
                for item in material_summary
                if isinstance(item, dict)
            ]
        for material, payload in entries:
            if isinstance(payload, dict):
                rows.append(
                    [
                        material,
                        payload.get("sheets", ""),
                        payload.get("parts", ""),
                        ", ".join(
                            f"{key}={value}"
                            for key, value in sorted(payload.items())
                            if key not in {"sheets", "parts"}
                        ),
                    ]
                )
            else:
                rows.append([material, "", "", payload])
        body.extend(
            [
                _paragraph("三、材料汇总", style="Heading1"),
                _table(rows),
            ]
        )
    if context.validation_errors:
        body.extend(
            [
                _paragraph("四、需要处理的问题", style="Heading1"),
                *[
                    _paragraph(f"• {message}")
                    for message in context.validation_errors
                ],
            ]
        )
    body.append(
        _paragraph(
            "说明：加工以本次交付的原位 DXF 和排板 DXF 为准；"
            "本报告用于审阅与归档。"
        )
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document '
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        + "".join(body)
        + (
            "<w:sectPr><w:pgSz w:w=\"11906\" w:h=\"16838\"/>"
            "<w:pgMar w:top=\"1134\" w:right=\"1134\" "
            "w:bottom=\"1134\" w:left=\"1134\"/>"
            "</w:sectPr>"
        )
        + "</w:body></w:document>"
    )


def export_word_report(
    context: Any,
    *,
    job_id: str,
    output_directory: str | Path,
    overwrite: bool = False,
) -> str:
    """Create a no-dependency DOCX report only when explicitly requested."""
    target_dir = resolve_output_directory(output_directory)
    stem = Path(context.dxf_path).stem
    suffix = f"{int(context.width_mm)}x{int(context.height_mm)}"
    target = target_dir / f"{stem}_排板报告_{suffix}.docx"
    if target.exists() and not overwrite:
        raise FileExistsError(f"Word 报告已存在: {target}")
    temporary = target.with_name(
        f".{target.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr(
                "[Content_Types].xml",
                """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>""",
            )
            archive.writestr(
                "_rels/.rels",
                """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>""",
            )
            archive.writestr(
                "word/_rels/document.xml.rels",
                """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>""",
            )
            archive.writestr("word/styles.xml", _styles_xml())
            archive.writestr(
                "word/document.xml",
                _report_document(context, job_id),
            )
            archive.writestr(
                "docProps/core.xml",
                """<?xml version="1.0" encoding="UTF-8"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:title>Stonebot 排板报告</dc:title>
  <dc:creator>Stonebot</dc:creator>
</cp:coreProperties>""",
            )
            archive.writestr(
                "docProps/app.xml",
                """<?xml version="1.0" encoding="UTF-8"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">
  <Application>Stonebot</Application>
</Properties>""",
            )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return str(target)
