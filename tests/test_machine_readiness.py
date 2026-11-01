"""机床就绪检查：单位、零长度、重复实体、0 层面板。"""

from __future__ import annotations

import ezdxf

from src.machine_readiness import findings


def test_missing_units_is_reported():
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 0
    msp = doc.modelspace()
    msp.add_line((0, 0), (100, 0))
    types = {item["type"] for item in findings(doc)}
    assert "units_missing" in types


def test_declared_millimetres_passes():
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    msp.add_line((0, 0), (100, 0))
    assert findings(doc) == []


def test_zero_length_entity_is_reported():
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    msp.add_line((10, 10), (10, 10))
    types = [item["type"] for item in findings(doc)]
    assert "zero_length_entity" in types


def test_duplicate_entity_is_reported():
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    msp.add_line((0, 0), (400, 0))
    msp.add_line((400, 0), (0, 0))  # 反向同样算重复
    types = [item["type"] for item in findings(doc)]
    assert "duplicate_entity" in types


def test_entities_outside_part_regions_are_ignored():
    from shapely.geometry import Polygon

    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    # 零件区域内：重复线
    msp.add_line((0, 0), (100, 0))
    msp.add_line((0, 0), (100, 0))
    # 零件区域外（详图）：重复线不应报
    msp.add_line((5000, 5000), (5100, 5000))
    msp.add_line((5000, 5000), (5100, 5000))

    region = Polygon([(-10, -10), (110, -10), (110, 10), (-10, 10)])
    result = findings(doc, regions=[region])
    duplicates = [item for item in result if item["type"] == "duplicate_entity"]
    assert len(duplicates) == 1
    assert duplicates[0]["coordinates"] == (50.0, 0.0)
