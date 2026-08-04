"""编号管理：识别 DXF 原有编号，或自动生成新编号"""

from src.models import Part


def assign_numbers(parts_data: list[dict],
                   force_renumber: bool = False,
                   skip_unnumbered: bool = False,
                   number_format: str = "P-{index:04d}") -> list[Part]:
    """
    为规格板分配编号。
    优先使用 DXF 原有编号，无编号则自动生成 P-0001 格式。
    force_renumber=True 时全部重新编号。
    skip_unnumbered=True 时跳过无原始编号的零件（只保留能匹配到编号层的台面板）。
    number_format 用于自定义格式，如 "A-{index:03d}" 表示 A-001。
    """
    parts = []
    index = 1

    for data in parts_data:
        if force_renumber:
            number = number_format.format(index=index)
            index += 1
        elif skip_unnumbered and not data.get("original_number"):
            continue
        else:
            number = data.get("original_number")
            if not number:
                number = f"P-{index:04d}"
                index += 1

        part = Part(
            id=data["index"],
            number=number,
            polygon=data["polygon"],
            outer_polygon=data["outer_polygon"],
            holes=data.get("holes", []),
            original_number=data.get("original_number"),
            area=data["area"],
            label_position=data["centroid"],
            outer_handle=data.get("outer_handle"),
            hole_handles=data.get("hole_handles", []),
        )
        parts.append(part)

    return parts