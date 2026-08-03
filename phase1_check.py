import sys, os
sys.path.insert(0, r'C:\Users\drago\stonebot_artificial_layout')
from pathlib import Path

from src.dxf_reader import read_dxf
from src.numbering import assign_numbers
from src.dxf_writer import write_inplace_check_dxf

PROJECT_ROOT = Path(r'C:\Users\drago\stonebot_artificial_layout')
dxf_path = r'C:\Users\drago\Desktop\临时文件\CGR45.dxf'

print('=' * 50)
print('Phase 1: DXF 读取 + 编号匹配 + 原位检查 DXF')
print('=' * 50)

# 参数
panel_layers = ['0']          # 台面板在图层0
exclude_layers = ['大样']     # 排除大样图层
number_layer = '编号'         # 编号图层
# 默认排除ZIGZAG, DASHED, MOBIAN等非实线

print(f'DXF: {dxf_path}')
print(f'Panel layers: {panel_layers}')
print(f'Exclude layers: {exclude_layers}')
print(f'Number layer: {number_layer}')
print()

print('Reading DXF...')
parts_data, doc = read_dxf(dxf_path,
                            panel_layers=panel_layers,
                            exclude_layers=exclude_layers,
                            number_layer=number_layer)
print(f'Found {len(parts_data)} parts')

if not parts_data:
    print('ERROR: No parts found!')
    sys.exit(1)

# Assign numbers
parts = assign_numbers(parts_data)
print(f'Assigned numbers to {len(parts)} parts')

# Show sample
for p in parts[:5]:
    handles_info = ''
    if hasattr(p, 'outer_handle') and p.outer_handle:
        handles_info += f' outer_handle={p.outer_handle}'
    if hasattr(p, 'hole_handles') and p.hole_handles:
        handles_info += f' holes={len(p.hole_handles)}'
    print(f'  {p.number}: area={p.area:.1f}{handles_info}')
if len(parts) > 5:
    print(f'  ... and {len(parts)-5} more')

# Write in-place check DXF
input_path = Path(dxf_path)
output_dir = PROJECT_ROOT / 'output'
output_dir.mkdir(exist_ok=True)
output_path = str(output_dir / f'{input_path.stem}_numbered_原位.dxf')

print(f'\nWriting in-place check DXF to: {output_path}')
write_inplace_check_dxf(parts, dxf_path, output_path, unit_system='metric')
print('DONE!')

# Summary
total_holes = sum(len(p.holes) for p in parts)
parts_with_holes = sum(1 for p in parts if p.holes)
print(f'\nSummary:')
print(f'  Total parts: {len(parts)}')
print(f'  Parts with holes: {parts_with_holes}')
print(f'  Total holes: {total_holes}')
print(f'  Output: {output_path}')
