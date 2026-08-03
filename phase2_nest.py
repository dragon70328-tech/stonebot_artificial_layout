import sys, time, json
sys.path.insert(0, r'C:\Users\drago\stonebot_artificial_layout')
from pathlib import Path
from src.dxf_reader import read_dxf
from src.numbering import assign_numbers
from src.nesting import nest_parts, validate_nesting
from src.dxf_writer import write_nested_dxf

PROJECT_ROOT = Path(r'C:\Users\drago\stonebot_artificial_layout')
dxf_path = r'C:\Users\drago\Desktop\临时文件\CGR45.dxf'
sheet_w, sheet_h, sheet_t = 3200.0, 1800.0, 20.0

print("=" * 50)
print("Phase 2: Nesting")
print("=" * 50)
print("Sheet: {} x {} mm".format(sheet_w, sheet_h))
print()

# Read DXF
parts_data, doc = read_dxf(dxf_path,
                            panel_layers=["0"],
                            exclude_layers=["\u5927\u6837"],
                            number_layer="\u7f16\u53f7")
parts = assign_numbers(parts_data)
print("Parts: {}".format(len(parts)))

# Nest
print("Nesting (this may take 2-3 minutes)...")
t0 = time.time()
result = nest_parts(parts, sheet_w, sheet_h, sheet_t, unit="metric",
                     improve_budget=180.0)
elapsed = time.time() - t0
print("Nesting done in {:.1f}s".format(elapsed))

# Validate
errors = validate_nesting(result, sheet_w, sheet_h)
if errors:
    print("Validation errors:")
    for e in errors:
        print("  - {}".format(e))
else:
    print("Validation: OK (no overlaps, no out-of-bounds)")

# Report
print()
print("=" * 50)
print("Report")
print("=" * 50)
print("Sheets used: {}".format(result.total_sheets))
print("Total parts: {}".format(result.total_parts))
print("Total part area: {:.1f} mm^2".format(result.total_part_area))
print("Total sheet area: {:.1f} mm^2".format(result.total_sheet_area))
print("Yield rate: {:.1f}%".format(result.yield_rate))
print()
for sheet in result.sheets:
    part_nums = [p.number for p in sheet.parts]
    preview = ", ".join(part_nums[:5])
    if len(part_nums) > 5:
        preview += "..."
    print("  Sheet {}: {} parts - {}".format(sheet.index, len(sheet.parts), preview))

# Write DXF
input_path = Path(dxf_path)
output_dir = PROJECT_ROOT / "output"
output_dir.mkdir(exist_ok=True)
sheet_suffix = "{}x{}".format(int(sheet_w), int(sheet_h))
output_dxf = str(output_dir / "{}_nested_{}_v2.dxf".format(input_path.stem, sheet_suffix))
print("\nWriting: {}".format(output_dxf))
write_nested_dxf(result, output_dxf, unit_system="metric")

# JSON report
output_json = str(output_dir / "{}_report_{}_v2.json".format(input_path.stem, sheet_suffix))
data = {
    "sheet_dimensions": {"width": sheet_w, "height": sheet_h, "thickness": sheet_t, "unit": "mm"},
    "unit_system": result.unit,
    "total_sheets": result.total_sheets,
    "total_parts": result.total_parts,
    "total_part_area": result.total_part_area,
    "total_sheet_area": result.total_sheet_area,
    "yield_rate": round(result.yield_rate, 2),
    "nesting_time_s": round(elapsed, 1),
    "sheets": [{"index": s.index, "parts": [p.number for p in s.parts],
                "part_count": len(s.parts), "used_area": s.used_area}
               for s in result.sheets]
}
with open(output_json, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print("Report: {}".format(output_json))
print("\nDONE!")
