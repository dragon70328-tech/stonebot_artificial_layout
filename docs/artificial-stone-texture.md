# 人造石大板纹理效果

## 范围

本功能只处理人造石：

- 每种材料/花色使用一张标准大板图片；
- 同一图片可重复用于该材料的全部大板；
- 图片范围必须是排板使用的**可利用尺寸**，不包含让尺；
- 不识别天然石每张板的不同尺寸、纹理和瑕疵；
- 不实现瑕疵标记或避瑕排板。

天然石逐板图片、边缘识别、瑕疵规避属于后续独立能力。

## 图案清单

运行时提供 JSON：

```json
{
  "patterns": [
    {
      "pattern_id": "artificial-a",
      "materials": ["ST/1", "ST/2"],
      "image_path": "patterns/artificial-a.png",
      "usable_width_mm": 3200,
      "usable_height_mm": 1800,
      "max_pixels_per_mm": 0.5
    }
  ]
}
```

规则：

- `material_group` 或 `packing_group` 命中 `materials` 即使用该图案；
- 一个材料不能同时映射到多个图案；
- 图片宽高比必须与可利用尺寸一致，误差上限 1%；
- `usable_width_mm/usable_height_mm` 必须与排板结果中每张板的
  `sheet.width/sheet.height` 一致；
- `max_pixels_per_mm` 可选，用于限制逐件纹理分辨率；省略时沿用源图像
  在大板可利用区上的原始像素密度；
- 让尺不参与图案换算。

## CLI

```powershell
.\.venv\Scripts\python.exe -X utf8 main.py input.dxf 3200 1800 20 `
  --profile min_sheets --kerf 5 --sheet-oversize 30,30 --no-confirm `
  --texture-patterns patterns.json --texture-image-reference-mode absolute `
  --texture-archive
```

默认纹理交付目录：

```text
output/<时间戳>_<文件名>/<文件名>_texture_原位/
```

也可显式指定空目录：

```powershell
--texture-output "D:\交付\项目纹理"
```

已有排板 DXF 可补做纹理，默认输出到排板 DXF 同一级的
`*_纹理原位/`：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\generate_slab_texture.py `
  --nested-dxf "排板.dxf" `
  --source-dxf "原位_编号.dxf" `
  --patterns patterns.json --archive
```

移动成果目录后，重定位可移动 DXF 的图片引用：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\relocate_texture_dxf.py `
  "新目录\*_纹理原位_可移动.dxf"
```

## HTTP / 门户

完成后的任务可通过以下接口生成纹理包：

```text
POST /texture-assets/{filename}?tenant_id=...&project_id=...
POST /jobs/{job_id}/textures
```

先上传所有标准图片和图案清单，再把返回的 `patterns.json` 服务器路径传给
第二个接口。纹理生成进入后台队列，完成后通过：

```text
GET /jobs/{job_id}
GET /jobs/{job_id}/artifacts/texture_archive
GET /jobs/{job_id}/artifacts/textured_dxf
GET /jobs/{job_id}/artifacts/texture_manifest
```

门户的“人造石纹理效果”面板已经接入这套接口。

## 生成过程

1. 读取当前有效排板结果；
2. 按零件的原材料或排板池找到标准图案；
3. 在大板可利用坐标中按零件实际位置采样；
4. 撤销排板旋转，把纹理还原到零件原位方向；
5. 用真实外轮廓裁切并用孔洞生成透明区域；
6. 按唯一编号找到原位编号 DXF 中的对应零件；
7. 在原位零件原始包围框内插入 `IMAGE`；
8. 回读 DXF，检查图片数量、图层和裁剪边界。

## 输出

```text
texture_原位/
  <编号>_纹理原位.dxf
  <编号>_纹理原位_可移动.dxf
  texture_manifest.json
  textures/
    artificial-a__<编号>.png
  previews/
    Sheet_001.png
```

`texture_manifest.json` 记录：

- 图案图片路径和 SHA-256；
- 可利用尺寸及“不包含让尺”的坐标口径；
- 每件编号、排板旋转、原位包围框、排板包围框；
- 纹理 PNG 路径与 SHA-256；
- 纹理像素尺寸和 DXF 裁剪路径。

## DXF 说明

纹理位于 `TEXTURE_IMAGE` 图层，不参与切割。DXF 的 `IMAGE` 是外部
栅格引用。AutoCAD 2023 对相对正斜杠引用兼容性不好，因此当前输出使用
绝对图片路径，保证本机直接打开可见。必须同时交付 `textures/` 目录；
移动纹理包后，使用 `scripts/relocate_texture_dxf.py` 将可移动 DXF 的
图片引用改写到新目录。`--texture-archive` / `archive=true` 会同时生成
绝对路径版、可移动版和 ZIP。
