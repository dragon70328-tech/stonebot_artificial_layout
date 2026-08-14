# Agent Instructions - 人造石台面 DXF 排板系统

## Role
人造石台面排版系统，读取 CAD 导出的 DXF 文件，识别实线封闭图形作为规格台面板，
自动匹配原始编号，在指定尺寸大板内进行二维不规则排样，输出排板 DXF 和出材率报告。

## Project Structure
```
stonebot_artificial_layout/
  AGENTS.md             # 本文件
  main.py               # CLI 入口（命令行排板）
  template_v3.py        # 模板排板：以历史排板结果为模板对号入座（老项目改版用）
  requirements.txt      # Python 依赖
  src/
    dxf_reader.py       # DXF 读取：提取封闭图形、构建层级、匹配编号
    dxf_writer.py       # DXF 写入：排板结果、编号检查文件
    models.py           # 数据模型：Part / Sheet / NestingResult
    nesting.py          # 排板引擎：多轮贪心 BFD + LNS 优化
    numbering.py        # 编号管理
    postprocess.py       # 后处理：四边压实、边缘对齐、间距强制；不改角度与板归属
    constraints.py      # 本体约束：工艺规则、材料规格、排板策略
    units.py            # 单位制切换
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

### Step 2: 构建面板层级（外轮廓 vs 孔洞）

核心：质心包容检测 + 交集面积比验证（不使用 shapely.contains，容错性差）

1. 所有实线封闭图形按面积降序排列
2. LWPOLYLINE 逐一处理：
   - 质心在已有外轮廓内 AND 交集面积 > 自身面积 * 90% -> 归类为孔洞
   - 否则 -> 作为新外轮廓
3. CIRCLE 永远是孔洞，分配给包含它的外轮廓（同上双条件）
4. 结果：203 外轮廓 + 306 孔洞（CGR45），100 外轮廓（countertop1 无孔洞）

### Step 3: 匹配原始编号

1. 从"编号"层收集短文本（<15 字符）作为候选池
2. 每个外轮廓取最近未用编号（距离 < 5000 单位）
3. 贪心 1:1 匹配保证唯一性
4. 无匹配的自动编号 P-XXXX

### Step 4: 生成编号检查 DXF（原位）

输出文件：`output/{name}_numbered_原位.dxf`

- 单位制与原文件一致
- 面板外轮廓 + 孔洞（独立图层）+ 编号 TEXT（质心居中）
- 坐标保持原位，不做平移旋转
- 用途：用户检查面板识别和编号准确性

### Step 5: 排板

后处理由独立的 PostProcessor 在校验前执行（可选），与排板算法解耦。

算法：多轮贪心 BFD + 大邻域搜索（LNS）

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
| min_gap | float | 最小切割间距 mm，0=不要求 |
| group_mode | str/None | "one_set_per_sheet" 或 None |
| slide_to_edge | bool | 排板后推边压实，空出内部切割通道 |
| align_edges | bool | 推边时尽量对齐相邻面板边缘 |
| sheet_thickness | float | 大板厚度 mm（记录用） |
| edge_margin | float | 面板距板边最小距离，通常 0 |

### 预置模板

| 模板 | rotation | gap | 分组 | 旋转 | 用途 |
|------|----------|-----|------|------|------|
| textured | [0] | 100mm | 一套一板 | 禁止 | 纹路大板 |
| min_sheets | [0,90,180,270] | 0 | 无 | 允许 | 最少板数 |
| balanced | [0,90,180,270] | 60mm | 无 | 允许 | 折中 |
| quick | [0,90,180,270] | 0 | 无 | 允许 | 快速预览 |

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
```

## 关键经验

### 线型过滤是第一步
ZIGZAG 线型的大样图标记必须排除，否则会被误识别为规格板。
实线 = CONTINUOUS 或 BYLAYER（且所在层线型为实线）。

### 孔洞检测不能用 shapely.contains()
`contains()` 对精度敏感，改用 centroid 包容 + 交集面积比（>90%）更可靠。

### 编号匹配需考虑坐标偏移
原图坐标可能很大（几十万单位），search_radius 默认 50 不够。按实际情况调整（如 5000）。

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

## 依赖

## 依赖
- Python 3.10+
- ezdxf（DXF 读写）
- shapely（几何计算）
- numpy
