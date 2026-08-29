# Block D - 6F 大堂地台石图纸画像

## 目标

记录 `Block D - 6F大堂地台石 廠方切石圖.dxf` 的读图方法、图纸指纹和已验证结果。
后续遇到同类 PDF 导入矩形网格图纸时，优先匹配 `blockd_6f_lobby` 画像复用同一套规则。

## 图纸特征

- DXF 版本：`AC1032`
- 单位：`INSUNITS=4`（毫米）
- 输入来源：PDF 导入 DXF，几何为大量开放 LWPOLYLINE，而不是标准封闭面板
- 面板形态：4 列 × 12 行矩形网格，共 48 个编号单元格
- 切分面板：`2`、`3`、`34`、`35` 每个单元格内部有闭合曲线，将矩形拆成两个非矩形闭合件

## 指纹

`analyze_drawing()` 对该文件生成的指纹如下：

```json
{
  "dxf_version": "AC1032",
  "insunits": 4,
  "entity_counts": {
    "HATCH": 1549,
    "SOLID": 52,
    "LWPOLYLINE": 3625,
    "MTEXT": 114,
    "TEXT": 18
  },
  "layer_counts": {
    "PDF_实体填充": 1601,
    "PDF_几何图形": 3621,
    "PDF_文字": 114,
    "0": 4,
    "MJ": 18
  },
  "linetype_counts": {
    "BYLAYER": 5358
  },
  "has_hatch": true,
  "has_lwpolyline": true,
  "has_line": false,
  "has_circle": false
}
```

已保存到：`drawing_profiles/validated_cases/blockd_6f_lobby.json`

## 分析方法

1. 自动匹配 `drawing_profiles/blockd_6f_lobby.json`。
2. 在 `PDF_几何图形` 层收集长水平/垂直 LWPOLYLINE。
3. 按 80% 全跨过滤，聚类得到 4 个 X 边界和 13 个 Y 边界，重建 4×12 矩形网格。
4. 从 `PDF_文字` 层收集 `1~48` 编号；通过 `exclude_entity_handles` 排除标题层重复的 `9`、`1`。
5. 对每个单元格查找 `PDF_几何图形` 和 `0` 层内的闭合 LWPOLYLINE：
   - 若找到两个互补闭合件且联合覆盖单元格 ≥ 90%，则替换原矩形，生成 `2a/2b`、`3a/3b`、`34a/34b`、`35a/35b`。
   - 否则保持原矩形单元格。
6. 网格模式下审图跳过开放网格线的未闭合/无效几何误报，只校验数量、编号、面积等结果。

## 已配置字段

核心字段位于 `drawing_profiles/blockd_6f_lobby.json`：

- `grid_mode: true`
- `grid_layer: "PDF_几何图形"`
- `grid_split_layers: ["PDF_几何图形", "0"]`
- `number_layers: ["PDF_文字"]`
- `label_pattern: "^(?P<part>\\d+)$"`
- `exclude_entity_handles: ["182F", "1830"]`
- `expected_counts: {"panels": 52, "holes": 0, "numbers": 52}`

## 已验证结果

- 面板数：`52`
- 唯一编号数：`52`
- 孔洞数：`0`
- 审图问题数：`0`
- 切分编号：`2a/2b`、`3a/3b`、`34a/34b`、`35a/35b`
- 回归测试：`109 passed`

## 复用命令

```powershell
python -X utf8 main.py "图纸路径.dxf" --audit
```

匹配成功后 CLI 会输出：

```text
matched drawing profile: blockd_6f_lobby
```
