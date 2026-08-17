# 约束外置清单

目标：把当前散落在脚本中的“工艺/材料/排板规则”逐步外置为可配置项，让 SaaS 用户通过界面或 AI 对话修改，而不需要改 Python 脚本。

## 一级：应优先外置为用户/项目配置

| 规则 | 当前位置 | 建议配置方式 |
|------|----------|--------------|
| 大板标准尺寸 | `src/constraints.py` `STANDARD_SHEET_SIZES` | 数据库/JSON，支持企业自定义 |
| 标准厚度 | `src/constraints.py` `STANDARD_THICKNESSES` | 数据库/JSON |
| 允许旋转角度 | `NestingProfile.rotation` | 项目配置 |
| 最小切割间距 | `NestingProfile.min_gap` | 项目配置 |
| 分组模式 | `NestingProfile.group_mode` | 项目配置，需补实装 |
| 是否推边 | `NestingProfile.slide_to_edge` | 项目配置 |
| 是否边缘对齐 | `NestingProfile.align_edges` | 项目配置 |
| 边缘余量 | `NestingProfile.edge_margin` | 项目配置 |
| 预置模板 | `PROFILE_*`、`PROFILES` | 模板库，支持用户新建 |
| 特殊板尺寸 | `main.py --special-size` | 项目配置 |
| 特殊板配对策略 | `main.py nest_special_parts` | 规则参数，例如是否两两配对、是否允许镜像 |
| 排板试验次数/种子/LNS 时长 | `main.py --trials/--seed/--budget` | 任务参数 |
| 快速模式配置 | `main.py QUICK_CONFIGS` | 任务参数 |
| 是否跳过无编号零件 | `main.py --include-unnumbered` | 读取配置 |
| 指定读取/排除图层 | `main.py --layers/--exclude-layers` | 读取配置 |
| 排除线型 | `src/dxf_reader.py DEFAULT_EXCLUDE_LINETYPES` | 读取配置 |
| 曲线采样精度 | `src/dxf_reader.py _bulge_arc_points`、`num_segments` | 读取配置 |

## 二级：应抽象为规则引擎

| 规则 | 当前实现 | 建议 |
|------|----------|------|
| 编号匹配优先级 | `_assign_numbers_by_containment` / `_assign_numbers_by_nearest_room` | 配置匹配策略：先包含后距离、仅距离、仅户型标签 |
| 编号搜索半径 | `search_radius=5000`、`20000` | 按 DXF 坐标量级自动推荐或用户配置 |
| 户型标签识别 | `_collect_room_texts` 中“户型/套/B7a”等硬编码 | 正则/字典配置 |
| 同户型同板 | `group_mode="one_set_per_sheet"` 尚未实装 | 从户型标签生成 `group_id`，再由引擎执行 |
| 特殊板判定 | `part_fits` 不满足普通板则视为特殊板 | 规则可配置：尺寸阈值、形状类型、指定编号 |
| 推边候选步长 | `postprocess.py step=50.0` | 后处理参数 |
| 边缘对齐容差 | `postprocess.py snap_tol=2.0` | 后处理参数 |
| 重叠/间距容差 | `nesting.py tol=0.01`、`postprocess.py gap` | 统一精度配置 |
| 贪心候选点上限 | `_MAX_VALID_PER_ROT=8` | 性能/质量参数 |
| LNS 拆除板数范围 | `rng.choice((2,3,3,4,4,5,6))` | 优化策略配置 |

## 三级：暂时保留为代码版本管理

这些属于算法实现细节，不适合普通用户修改，应由 Codex 作为开发工具维护：

- 候选点生成与评分模式：`skyline`、`column`、`contact`
- 滑动压实步长策略：`1024 -> 0.25`
- LNS 的排序/模式随机选择
- Shapely 几何转换与 bulge 弧线采样
- DXF 图层结构、颜色、GROUP 组织
- 特殊板两两配对搜索算法

## 建议的配置形态

### 大板“让尺”配置

让尺不是简单的大板尺寸，而是“名义尺寸 + 允许超出量”。类似水星项目：

```yaml
sheet_spec:
  nominal:
    width: 3200
    height: 1600
  allowance:
    width_extra: 25
    height_extra: 25
    # 可选：left/right/bottom/top 分别配置
  effective_max:
    width: 3225
    height: 1625
allowance_usage: auto
```

`allowance_usage` 建议支持：
- `none`：所有板都按名义尺寸排，不使用让尺。
- `all`：所有板都按最大让尺尺寸排。
- `auto`：系统自动判断哪些件需要让尺，并计算最少需要几张让尺大板。

自动判断流程：
1. 按允许旋转角计算每块板的包围盒。
2. 能放入名义尺寸的零件进入普通板池。
3. 不能放入名义尺寸、但能放入让尺尺寸的零件进入让尺池。
4. 让尺池继续沿用特殊板配对逻辑，优先两张共用一张让尺板。
5. 报告普通板数量和让尺板数量，例如：`普通板 121 张 + 让尺板 5 张`。

自然语言示例：
- “大板 3200×1600，允许让尺 25×25。”
- “超尺寸的 F 厨房板用让尺，其他不用。”
- “先自动判断需要几张让尺板，再决定是否接受。”

使用 YAML/JSON 作为中间格式，数据库只存引用和覆盖值：

```yaml
project_id: 20260815-001
sheet:
  width: 3200
  height: 1800
  thickness: 20
  special:
    width: 3225
    height: 1625
profile:
  rotation: [0, 90, 180, 270]
  min_gap: 0
  group_mode: null
  slide_to_edge: true
  align_edges: true
  edge_margin: 0
reader:
  include_unnumbered: false
  layers: null
  exclude_layers: []
  exclude_linetypes: [ZIGZAG, DASHED, HIDDEN]
  number_match:
    strategy: containment_then_distance
    search_radius: 20000
optimizer:
  trials: 1
  seed: 0
  budget_seconds: 60
  quick: false
postprocess:
  edge_candidate_step: 50
  align_tolerance: 2
```

## SaaS 使用流程

1. 用户上传 DXF，AI 自动执行读图预检。
2. AI 将用户自然语言要求翻译成配置草稿。
3. 用户确认后保存为项目配置。
4. 排板引擎读取配置执行，不修改脚本。
5. 同类型项目可复用模板，持续沉淀规则。

## 下一步建议

1. 先实现 `ProjectConfig` 模型和 YAML/JSON 读取。
2. 让 `main.run()` 接受 `ProjectConfig` 或兼容的配置对象。
3. 把一级配置全部接入，保留 CLI 参数兼容。
4. 再实现二级规则引擎中的“户型分组”和“编号匹配策略”。
