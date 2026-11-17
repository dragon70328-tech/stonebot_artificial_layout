# 全矢量 PDF 原位 DXF 试点

## 目标

把一个全矢量的多页 CAD PDF 转换为**一个**可直接检查的原位 DXF。默认交付仍是
一张原位 DXF 和一张排板 DXF；PDF 适配器只负责把前者做出来，不改变排板算法。

启德医院 CO-001 试点要求：

- PDF 共 9 页，对应 D6F、D5F、D4F、D7F、D8F、D9F、D10F、DB1F、DB2F。
- 单一 DXF 内包含 9 个非重叠楼层块和 9 个 A4 竖向布局。
- 图框内外的文字说明、填充纹理、尺寸标注和板号必须保留。
- 394 个原图板号必须可追溯；R450 区拆成两个不同材料的异形件。
- R450 用真 ARC 或带 bulge 的多段线，不得静默折线化。

## 输入门禁

- 只接受 `.pdf`，且必须是 CAD 导出的全矢量 PDF。
- 扫描件、整页图片、加密 PDF 拒绝进入生产转换。
- 页面映射由明确配置提供：`PAGE:FLOOR:PREFIX`，不根据中文标题猜测楼层。
- 板号必须与尺寸文本成对识别；原图板号总数不符时 fail closed。

## 输出层

| 图层 | 用途 |
|---|---|
| `PDF_VECTOR` | PDF 主矢量路径与填充轮廓 |
| `PDF_TEXT_VECTOR` | PDF 文字轮廓，保证无 ToUnicode 的中文仍可见 |
| `HATCH` | PDF 填充/纹理路径 |
| `OUTER` | 可加工零件外轮廓 |
| `R450_BOUNDARY` | R450 真圆弧 |
| `R450_ENVELOPE` | 辅助包络，不作为切割轮廓 |
| `NUMBERS` | 加工件板号 |
| `MATERIALS` | 材料号与厚度 |
| `DIMENSIONS` | 可提取的尺寸文字 |
| `TEXT` | 可提取的文字说明 |
| `CHECK_ISSUES` | 不确定项，禁止直接下料 |

每个 `OUTER`、板号和材料文字都在同一 GROUP；零件轮廓带 `STONEBOT` XDATA，
记录楼层、原板号、材料、PDF 页、行列和是否 R450。

## 材料规格板 Excel

`scripts/export_pdf_material_schedule.py` 从已确认的原位 DXF 生成三表工作簿：

- `规格板汇总`：按材料名称 + 规格 + 矩形/异形汇总数量与板号。
- `材料总计`：按材料名称给出总数量和异形数量。
- `逐件明细`：逐板号列出实际包围尺寸和采用的生产规格。

异形件按真实 ARC/bulge 计算最大包围矩形，并把长、宽分别向上取整到 1mm。

## R450 规则

- R450 区域的最终包络是 `1197x1197` 或 `1197x1472`。
- 每个受弧线分割的单元格生成两块不同材料件：`ST-101` 弧形件和纹理件。
- 弧形分块的包围高度严格为 `725mm`；一旦与 `1197/1472` 包络冲突必须报错。
- 弧形件沿用原板号并追加 `-ST101`；纹理件保留原板号，确保原图编号可追溯。
- 拟合半径必须为 `450mm ± 3mm`，否则拒绝生成该区域并报错。
- 包络矩形不是成品件；不得把两块异形件合并成一块矩形下料。
- `R450_BOUNDARY` 是校验层，默认关闭并冻结；加工轮廓只在 `OUTER` 多段线中。

## 当前限制

- 文字优先以 PDF 字形轮廓保留；可可靠提取的数字/文字再叠加为 DXF TEXT。
- DXF 使用毫米单位，并固定 `STONEBOT_MM` 尺寸样式（`DIMLFAC=1`、`DIMSCALE=1`）。
- PDF 中的任意填充先按矢量路径保留；材料分类只认 ST-101～ST-104 的已知纹理。
- D6F 的少量纹理与其余楼层采用相同模板，若单页纹理无法确定，可用同行列、
  同规格楼层的一致结果补齐，并在审计中标记
  `material_inferred_from_floor_template`。
- PDF 适配器尚不替代 `--audit` 的正式确认流程；转换后仍需用户确认原位 DXF。

## 命令

```powershell
python scripts/pdf_to_inplace_dxf.py input.pdf output.dxf `
  --page 1:D6F:D6F --page 2:D5F:D5F `
  --expected-pages 9 --expected-boards 394 --expected-r450 28 `
  --audit-json output/report.json
```

客户 PDF、转换 DXF 和审计 JSON 默认不进入公开 GitHub 仓库。
