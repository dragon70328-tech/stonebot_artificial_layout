# Agent Instructions - 人造石加工规划系统

## Role
人造石加工规划系统，读取 CAD 导出的 DXF 文件，识别实线封闭图形作为规格件
（台面板、挡水条、异形件等），自动匹配原始编号，在指定尺寸大板内进行二维不规则
排样，输出排板 DXF 和出材率报告。仅接受 DXF 输入，其他格式须先由用户自行转为 DXF。

## 长期目标
SaaS 化人造石加工规划系统围绕五个支柱建设：项目架构约束、本体/数据契约、工具/确定性执行、
调度循环、边界。AI 只生成结构化意图和配置，不直接执行生产代码；真实执行由可信
Python 工具完成，工作流状态机统一调度。详见 `docs/saas-architecture-goal.md`。

当前读图范围仅 DXF 一种格式；DWG 转换与 PDF/手绘草图读图均已放弃
（2026-08-19 范围调整，相关代码归档于 `_archive/legacy/intake_dwg_pdf/`）。

## Project Structure
```
stonebot_artificial_layout/
  AGENTS.md             # 本文件
  main.py               # CLI 入口（命令行排板）
  requirements.txt      # Python 依赖
  _archive/             # 归档遗留脚本、环境文件、备份与散落输出
  src/
    dxf_reader.py       # DXF 读取：提取封闭图形、构建层级、匹配编号
    dxf_writer.py       # DXF 写入：排板结果、编号检查文件
    models.py           # 数据模型：Part / Sheet / NestingResult
    contracts.py        # 读图数据契约（Pydantic，审图/复检状态）
    nesting.py          # 排板引擎：多轮贪心 BFD + LNS 优化
    list_nesting.py     # 清单排板：Excel/PDF 矩形规格件，配对贪心 + skyline
    deepnest_engine.py  # 水刀/激光 DeepNest/BLF 排板引擎
    pairing.py          # 相同形状 180° 共边配对预排（可选增强）
    drawing_profile.py  # 图纸画像匹配、结构化读图与编号识别
    visual_evidence.py  # 审图问题视觉证据（SVG 高亮）
    numbering.py        # 编号管理
    postprocess.py       # 后处理：四边压实、边缘对齐、间距强制；不改角度与板归属
    constraints.py      # 本体约束：工艺规则、材料规格、排板策略
    units.py            # 单位制切换
  drawing_profiles/     # 项目级图纸画像 JSON
  sample/               # 输入样本 DXF
  output/               # 输出目录
  tests/                # 测试
  docs/                 # 文档
  _analysis/            # 工作日志（worklog）
```

## 工作步骤

### Step 1: 读取 DXF，筛选实线封闭图形

1. 遍历 modelspace 所有实体，按 dxftype() 分类
2. 线型过滤：只保留 linetype 为 CONTINUOUS 或 BYLAYER（所在层为实线）的实体
   排除 ZIGZAG（大样图标记）、DASHED（虚线）等非规格板图形
3. 封闭判断：LWPolyline.closed=True 或首尾点距离 < 0.01
4. 实体转 Polygon：LWPolyline -> 顶点坐标；Circle -> 64段近似；Spline -> flattening(0.1)
5. 若自动匹配到 `drawing_profiles/*.json` 图纸画像，则 `panel_layer`、
   `hatch_layer`、`use_hatch`、`exclude_entity_types`、`closed_tolerance`
   由画像提供，覆盖上述通用默认值。

### Step 2: 构建面板层级（外轮廓 vs 孔洞）

核心：质心包容检测 + 交集面积比验证（不使用 shapely.contains，容错性差）

1. 所有实线封闭图形按面积降序排列
2. LWPOLYLINE 逐一处理：
   - 质心在已有外轮廓内 AND 交集面积 > 自身面积 * 90% -> 归类为孔洞
   - 否则 -> 作为新外轮廓
3. CIRCLE 永远是孔洞，分配给包含它的外轮廓（同上双条件）
4. 结果：203 外轮廓 + 306 孔洞（CGR45），100 外轮廓（countertop1 无孔洞）

### Step 3: 匹配原始编号

自动匹配 `drawing_profiles/*.json` 图纸画像时，使用画像中的编号识别规则：

1. `number_layers`：从指定的多个 TEXT/MTEXT 图层收集候选编号
2. `label_pattern`：按正则识别并清洗编号，具体格式由画像定义
3. `assignment_mode=point_then_bbox`：先用编号点落入面板判断归属，
   再用文本包围盒与面板重叠比例补配
4. 贪心 1:1 匹配保证唯一性，冲突按 `conflict_resolution` 处理
5. 无匹配时保持原编号为空，由 `assign_numbers` 决定跳过或自动编号 `P-XXXX`

未匹配到图纸画像时，保留旧回退逻辑：

- 从含“编”字的图层收集短文本，或从所有 TEXT/MTEXT 中按常见编号形态过滤
- 每个外轮廓取最近未用编号，默认搜索半径可随图面坐标偏移调整（如 5000）
- 无匹配的自动编号 `P-XXXX`

### Step 4: 生成编号检查 DXF（原位）

输出文件：`output/{name}_numbered_原位.dxf`

- 单位制与原文件一致
- 面板外轮廓 + 孔洞（独立图层）+ 编号 TEXT（质心居中）
- 坐标保持原位，不做平移旋转
- 用途：用户检查面板识别和编号准确性

### Step 4.5: 材料分组（仅启用图纸画像的项目）

启用 `material_group_enabled` 的图纸画像，按材料前缀分别排板：

- `material_prefix_pattern` 提取材料前缀
- `allowed_material_prefixes` 限定允许排板的材料前缀集合
- 无允许材料前缀的零件不排板，直接跳过
- 每种材料单独进入排板引擎，不同材料绝不共板
- 报告中按 `material_group` 输出每组的件数、板数、出材率

### Step 5: 排板

后处理由独立的 PostProcessor 在校验前执行（可选），与排板算法解耦。

加工方式由 `--process` 或 `NestingProfile.processing_class` 决定：

- `bridge`：走 `src/nesting.py`，多轮贪心 BFD + 大邻域搜索（LNS）
- `waterjet` / `laser`：走 `src/deepnest_engine.py`，Bottom-Left Fill + 滑动压实
- `--pairing`：水刀 DeepNest 分支先做相同形状 180° 共边配对预排
- `--no-rotation`：强制 `rotation=[0]`、`arbitrary_rotation=False`
- `first_part_left_edge=True` 时，每张板首件最长直边贴大板左边缘；
  禁止旋转时若原图最长边不是竖直方向，该规则无法保证，会回退为普通左下放置

桥切机原算法：

1. 创建 Part 对象：outer_polygon 用于碰撞，holes 跟随变换
2. 多轮配置：sort（short/area/long/jitter）× mode（skyline/column/contact）× seed
3. improve_budget：120~180 秒大邻域搜索
4. 每轮按排列键贪心放置，旋转角度由 NestingProfile.rotation 控制
5. 取最优（先比板数，再比紧凑度）
6. 精确校验：边界 + DE-9IM 重叠检测
7. 排板结束后先报告使用大板数量，用户确认后再执行后处理推板；`--no-confirm` 可跳过确认

### Step 6: 生成排板 DXF

输出文件：`output/{name}_nested_{W}x{H}.dxf`

图层结构：
- SHEET_BORDER：大板外框（红色）+ Sheet 编号
- OUTER：台面板外轮廓（黑色）
- HOLES：盆孔/水龙头孔（蓝色），不参与排板但跟随面板
- NUMBERS：板件编号（居中）

每个面板 + 孔洞 + 编号组成 GROUP（`doc.groups.new(name).extend(entities)`），选中即全选。

## 约束系统（src/constraints.py）

### NestingProfile 参数

| 参数 | 类型 | 说明 |
|------|------|------|
| rotation | list[int] | 允许的旋转角度，如 [0] 或 [0,90,180,270] |
| arbitrary_rotation | bool | 是否允许任意角度旋转（水刀/激光 DeepNest） |
| min_gap | float | 最小切割间距 mm，0=不要求 |
| group_mode | str/None | "one_set_per_sheet" 或 None |
| slide_to_edge | bool | 排板后推边压实，空出内部切割通道 |
| align_edges | bool | 推边时尽量对齐相邻面板边缘 |
| sheet_thickness | float | 大板厚度 mm（记录用） |
| edge_margin | float | 面板距板边最小距离，通常 0 |
| processing_class | str | `bridge` 或 `waterjet_laser` |
| uses_deepnest | bool | 由 processing_class 推导，水刀/激光为 True |

### 预置模板

| 模板 | rotation | gap | 分组 | 旋转 | 用途 |
|------|----------|-----|------|------|------|
| textured | [0] | 100mm | 一套一板 | 禁止 | 纹路大板 |
| min_sheets | [0,90,180,270] | 0 | 无 | 允许 | 最少板数 |
| balanced | [0,90,180,270] | 60mm | 无 | 允许 | 折中 |
| quick | [0,90,180,270] | 0 | 无 | 允许 | 快速预览 |
| waterjet | [0,90,180,270] | 0 | 无 | 允许 | 水刀 DeepNest |
| laser | [0,90,180,270] | 0 | 无 | 允许 | 激光 DeepNest |

### 使用方式

```bash
# 预置模板
python main.py input.dxf 3200 1800 20 --profile textured
python main.py input.dxf 3200 1800 20 --profile min_sheets

# 覆盖参数
python main.py input.dxf 3200 1800 20 --profile textured --rotation "0,90" --min-gap 50

# 自由组合（不选模板，直接指定参数）
python main.py input.dxf 3200 1800 20 --rotation "0" --min-gap 100 --no-slide

# 排板后自动确认板数，直接进入后处理
python main.py input.dxf 3200 1800 20 --no-confirm

# 混合板尺寸：普通板 3200x1600，特殊板 3225x1625，先只报板数
python main.py input.dxf 3200 1600 20 --quick --special-size 3225x1625 --report-only

# 特殊板会自动尝试两张头尾相接共用一张；确认板数后继续后处理
python main.py input.dxf 3200 1600 20 --profile min_sheets --special-size 3225x1625 --quick --budget 30 --no-confirm

# 水刀 DeepNest（自动旋转）
python main.py input.dxf 3200 1600 20 --process waterjet --quick --budget 0 --no-confirm

# 水刀 DeepNest + 相同形状共边配对（可选增强）
python main.py input.dxf 3200 1600 20 --process waterjet --pairing --quick --budget 0 --no-confirm

# 水刀 DeepNest，禁止旋转但启用共边配对
python main.py input.dxf 3200 1600 20 --process waterjet --no-rotation --pairing --quick --budget 0 --no-confirm

# 水刀 DeepNest，禁止旋转且不配对
python main.py input.dxf 3200 1600 20 --process waterjet --no-rotation --quick --budget 0 --no-confirm
```

## 关键经验

### 线型过滤是第一步
ZIGZAG 线型的大样图标记必须排除，否则会被误识别为规格板。
实线 = CONTINUOUS 或 BYLAYER（且所在层线型为实线）。

### 孔洞检测不能用 shapely.contains()
`contains()` 对精度敏感，改用 centroid 包容 + 交集面积比（>90%）更可靠。

### 编号匹配需考虑坐标偏移
原图坐标可能很大（几十万单位），search_radius 默认 50 不够。按实际情况调整（如 5000）。

### 多编号图层不能只靠“编”字图层
同一图纸可能同时存在多个编号相关图层，必须用
`DrawingProfile.number_layers` 显式指定，不能只依赖图层名含“编”的旧逻辑。

### 材料前缀决定是否排板
启用 `material_group_enabled` 后，允许材料前缀集合由
`DrawingProfile.allowed_material_prefixes` 决定；没有允许材料前缀的零件
即使识别为封闭图形也不排板。

### 禁止旋转时首件左贴边可能不满足
`first_part_left_edge` 要求首件最长直边贴大板左边缘；若 `--no-rotation`
且原图最长边不是竖直方向，系统会回退为普通左下放置，此时不能保证该规则。

### CIRCLE 实体单独处理
CIRCLE 永远是孔洞（水龙头/盆孔），不能作为外轮廓，但必须包含在层级检测中。

### GROUP 组织面板
排板 DXF 中每个面板外框+孔洞+编号用 GROUP 绑定，方便后续 CNC 操作时选中。

### 出材率解读
出材率 = 面板净面积 / 大板总面积。面板净面积已扣除孔洞，所以出材率会比看起来低一些。
实际材料消耗看大板张数，出材率主要用于不同方案间比较。

## 后处理（src/postprocess.py）

排板引擎只负责放置和优化，后处理作为独立的几何变换管道在校验前执行。

### PostProcessor 操作

| 操作 | 说明 |
|------|------|
| maximize_edge_clearance | 在不改变角度和大板归属的前提下，将面板重排到四边，最大化中央空区；使用 50mm 周缘候选点，失败时退回逐板滑动 |
| align_edges | 将相邻面板边缘对齐到同一直线（snap_tol=2mm），方便圆盘锯直线切割 |
| enforce_min_gap | 强制面板间最小间距，逐对推开过近的面板 |

### 执行顺序

排板完成 → 报告板数 → 用户确认 → maximize_edge_clearance → align_edges → enforce_min_gap

由 NestingProfile 参数控制是否启用。

## 特殊板头尾相接

普通板与特殊板分开排板：普通板用名义大板尺寸，特殊板用 `--special-size`。
特殊板排板不修改 `src/nesting.py`，而是在 `main.py` 中先尝试两张一组：

1. 搜索两块特殊板各自旋转后能否在同一张大板内无重叠放置。
2. 可配对则两张一张；不可配对则单独一张。
3. 该逻辑只处理特殊板，普通板仍走原有排板引擎。

## 清单排板（Excel/PDF 规格尺寸与数量）

仅从 `.xlsx` / `.pdf` 清单提取矩形规格件尺寸和数量，按材料自动分组排板，
输出文字结论（不输出 DXF）：

```bash
python main.py input.xlsx 3200 1800 20 --list-nest
python main.py input.pdf 2500 1400 20 --list-nest --no-rotation

# 同时指定两种可用大板，优先最小总板面积（最高出材率）；
# 总板面积相同时优先多用小尺寸板，再按板数最少选择
python main.py input.xlsx --sizes 2400x1200,2500x1400 --list-nest
python main.py input.xlsx 2400 1200 --special-size 2500x1400 --list-nest

# 生产口径：锯缝 5mm（零件为净尺寸），大板让尺 30mm/边
# 可用尺寸 2430x1230 与 2530x1430，报告同时给出标称计价口径
python main.py input.xlsx --sizes 2400x1200,2500x1400 --list-nest --kerf 5 --oversize 30
```

- Excel 列名支持 材料编号/长度(mm)/宽度(mm)/数量(件) 等自动识别。
- PDF 使用 PyMuPDF 表格检测。
- 材料分组：材料列存在重复值时按材料分板；编号列值唯一时合并为同一组。
- Excel 工作表可能横向重复多组“成品尺寸/数量”列；解析时必须先按数量列
  拆成多个列组，再逐组识别，不能只读第一组。
- 矩形清单先按 PDF 参考排板逻辑配对：规格件旋转后横向并排，不设固定件数，
  只要总宽不超板宽、总高不超板高；先贪心选择利用率最高的配对模式。
- 大批量重复模式先固定；剩余小批量零件再用带记忆的回溯搜索精确优化，
  目标仍是总板面积最小、其次板数最少。
- 横向组合数量过大时停止全量枚举并回退 skyline，避免窄条/多规格清单卡死。
- skyline 回退路径按“大件先排 → 剩余空间先填短窄条 → 剩余长窄条短边竖排、
  优先开小尺寸新板”的顺序执行；这适用于 ST-104 这类大面积板 + 大量 95mm 窄条清单。
- 清单排板新增“大面积规格板先配对 → 窄条填入已排板剩余矩形 → 剩余窄条另排”
  候选（v3，含 `src/strip_packing.py` 精确剩余窄条 packer），
  `ST-104` 名义口径当前结果 121 张/98.4%（2400x1200 60 张、2500x1400 61 张，
  人工参考 121 张/98.9%）；生产口径 `--kerf 5 --oversize 30`
  结果 122 张（2430x1230 60 张、2530x1430 62 张，标称计价 389.8 m²），
  几何校验：越界 0、重叠 0、件间净间距恰为 5mm。
- kerf 化通过放大空间实现：件 +kerf、板 +kerf、零间隙相邻 = 件间 kerf 缝；
  `_fill_free_rect_renba` 的窄条节距必须用调用方传入的 `lane_width`，
  不能硬编码 95，否则放大空间会多放一排/一列导致越界和压叠。
- 窄条填入剩余矩形前，`_subtract_occupied_rect` 使用上下左右四块互不重叠的分区；
  旧的最大矩形表示会重叠，导致个别板件利用率超过 100% 或板件互相压叠。
- 清单排板默认自动生成 DXF；可用 `--output-dxf` 指定路径，或用 `--no-dxf` 关闭。
  `write_list_nesting_dxf` 会把布局相同的大板归为一组，只画一张代表图，
  并在图旁标注“数量 x N”。
- 配对结果与 skyline/混合尺寸候选一起比较，仍按总板面积最小、板数最少选择。
- 当前结论以完整清单为准：`ST-101规格板清单.xlsx` 解析为 14 行 562 件，
  大板 `2400x1200 + 2500x1400` 排板结果为 200 + 81 = 281 张，出材率 96.5%。

## 依赖

## 依赖
- Python 3.10+
- ezdxf（DXF 读写）
- shapely（几何计算）
- numpy
