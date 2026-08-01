"""编号管理：识别 DXF 原有编号，或自动生成新编号"""

from src.models import Part


def assign_numbers(parts_data: list[dict]) -> list[Part]:
    """
    为规格板分配编号。
    优先使用 DXF 原有编号，无编号则自动生成 P-0001 格式。
    """
    parts = []
    auto_index = 1

    for data in parts_data:
        number = data.get("original_number")
        if not number:
            number = f"P-{auto_index:04d}"
            auto_index += 1

        part = Part(
            id=data["index"],
            number=number,
            polygon=data["polygon"],
            outer_polygon=data["outer_polygon"],
            holes=data.get("holes", []),
            original_number=data.get("original_number"),
            area=data["area"],
            label_position=data["centroid"],
        )
        parts.append(part)

    return parts
