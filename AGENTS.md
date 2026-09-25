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
  AGENTS.md             # 本文件（业务与工艺约束）
  main.py               # CLI 入口：排板 / --audit 审图 / --list-nest 清单排板 / --agent
  requirements.txt      # Python 依赖
  Dockerfile            # api 镜像（uvicorn src.http_api:app）
  docker-compose.yml    # api + worker 两个服务，共享 /data
  .github/workflows/    # CI：pytest -q -m "not slow"
  portal/               # 静态前端：modules.json 注册、nesting/ 识图排板页、admin/ 模型运维页
  deploy/               # nginx 反代示例、识别模型定时运维模板
  scripts/              # 开发启动、识别模型训练/监控/归档、备份、复核脚本
  src/
    # ── 适配层 adapter ──
    dxf_reader.py       # DXF 读取：封闭图形候选、层级（孔洞）、编号文本
    dxf_writer.py       # DXF 写入：排板结果、编号检查、GROUP、清单排板图
    drawing_preview.py  # 原位 DXF 在线预览（可点击实体 + target_id）
    visual_evidence.py  # 审图问题视觉证据（SVG 高亮）
    visual_renderer.py  # 整图 SVG 渲染
    # ── 领域/契约 domain ──
    models.py           # Part / Sheet / NestingResult
    constraints.py      # NestingProfile、预置模板、标准板规格
    nesting_contracts.py # SheetSpec / ProcessPolicy / NestingRequest / NestingRevision
    contracts.py        # 读图数据契约（Pydantic，审图/复检状态），SCHEMA_VERSION
    correction_contracts.py / recognized_contract.py / recognition_contracts.py
    project_config.py   # ProjectConfig（sheet/profile/optimizer/reader）
    drawing_profile.py  # 图纸画像匹配、结构化读图、编号归属、审图
    entity_classifier.py # 图层弱化的多证据实体分类（panel/hole/unknown）
    # ── 应用/排板 application ──
    nesting.py          # 桥切引擎：多轮贪心 BFD + LNS
    beam_nesting.py     # 桥切第二布局路径：逐板最佳填充束搜索（尺寸档/缺口回收/多板尾解）
    deepnest_engine.py  # 水刀/激光引擎：BLF + 滑动压实
    pairing.py          # 相同形状 180° 共边配对
    list_parsing.py     # 清单解析适配层（.xlsx/.pdf -> ListItem）
    list_nesting.py     # 清单排板：配对贪心 + skyline + 窄条填充 + 多尺寸选板
    strip_packing.py    # 剩余窄条精确 packer（清单排板 v3）
    template_nesting.py # 已验证模板复用（哈希匹配）
    postprocess.py      # 后处理：推边/对齐/间距、刀缝材料校验
    nesting_reviewer.py # 排板回看减板
    workflow.py         # 读图->分组->排板->后处理准备（确定性执行主体）
    # ── 调度/Agent ──
    workflow_session.py # WorkflowStage 状态机（含 backtrack）
    agent_runtime.py    # 非阻塞运行时（CLI 与 HTTP 共用）
    agent_loop.py / agent_cli.py / orchestrator.py / worker.py
    tool_schemas.py     # 工具白名单注册表（阶段、schema、是否生产写入）
    tool_handlers.py    # 确定性工具实现（只改 JobContext）
    ai_planning.py / llm_plan_provider.py / openai_llm_client.py / llm_client.py
    # ── SaaS 基础设施 infrastructure ──
    http_api.py         # FastAPI：/conversations /jobs /admin 路由 + 门户挂载
    job_context.py      # 单任务内存状态（含契约与产物引用）
    job_repository.py / session_store.py / tenant_store.py / state_db.py
    task_queue.py / task_events.py / object_store.py / artifact_store.py
    conversation_planner.py / conversation_store.py
    job 相关：admin_ops.py / admin_audit.py / audit_cache.py / case_library.py
    # ── 识别学习闭环 ──
    recognition_memory.py / recognition_feedback.py / recognition_model.py
    recognition_operations.py / recognition_training_policy.py / recognition_retention.py
    # ── CLI 支撑 ──
    cli_audit.py        # 审图 CLI（从 main.py 抽出）
    cli_paths.py        # 输出目录约定（时间戳_文件名）
    numbering.py / units.py / input_guard.py / active_learning.py
  drawing_profiles/     # 项目级图纸画像 JSON（validated_cases/、validated_templates/）
  output/               # 运行产物（时间戳目录 + api_jobs；.gitignore 只保留三类文件）
  tests/                # 测试（pytest，CI 排除 slow 标记）
  docs/                 # 设计与改进计划文档，见下表
  _analysis/            # 工作日志（worklog_YYYY-MM-DD.md）与一次性分析脚本
  _archive/             # 归档遗留脚本与散落输出（含 legacy/intake_dwg_pdf/）
```

> 注意：不存在 `sample/` 输入样本目录；回归样本以 `drawing_profiles/validated_cases/`
> 与 `output/` 中已生成的 DXF 为准。

## 文档索引

| 文档 | 内容 |
|---|---|
| `docs/saas-architecture-goal.md` | SaaS 长期目标与五个支柱（判断代码改动的标准） |
| `docs/saas-setup.md` | 当前搭建/运行/环境变量/租户认证 |
| `docs/drawing-intake-subsystem.md` | 读图子系统设计（DXF 唯一输入） |
| `docs/process-aware-nesting-improvement-plan.md` | 统一工艺排板改进计划（契约、刀缝、版本重排） |
| `docs/ai-workflow-improvement-plan.md` | 对话式排板工作流 |
| `docs/constraint-externalization.md` | 约束外置到 JSON/YAML |
| `docs/agentization-improvement-plan.md` | Agent 化路线（本体 -> Harness -> Skill -> Loop -> 生产边界） |
| `docs/irregular-nesting-improvement-plan.md` | 不规则图形排板质量改进（当前排板重点） |
| `docs/recognition-model-operations.md` | 识别模型训练/监控/回滚/保留 |
| `docs/modularization-plan.md` | 大文件拆分计划与安全规则 |
| `docs/drawing-profile-*.md` | 具体项目画像说明 |

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
6. 若多个面板匹配到同一原编号，`assign_numbers` 保留第一个原编号，其余自动追加
   `-2`、`-3` 等后缀，确保最终排板/编号 DXF 中的 `number` 唯一；审图仍会
   提示 `duplicate_label` 供用户核对。

未匹配到图纸画像时，保留旧回退逻辑：

- 从含“编”字的图层或 `NUMBERS` 图层（本系统编号检查文件约定）收集短文本，
  或从所有 TEXT/MTEXT 中按常见编号形态过滤
- 每个外轮廓取最近未用编号，默认搜索半径可随图面坐标偏移调整（如 5000）
- 无匹配的自动编号 `P-XXXX`

注意：编号检查 DXF（`OUTER/HOLES/NUMBERS` 图层）回读依赖 `NUMBERS` 图层约定；
缺失该约定时会静默丢掉编号匹配不到的零件（2026-08-30 水星 950→1258 修复）。

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

布局来源必须是显式的（2026-09-23 起）：

- 模板布局复用（`--use-template`）默认**关闭**，默认只用算法排板；
  模板匹配键是「源 DXF sha256 + 大板尺寸」，命中即复刻历史布局并跳过算法，
  因此绝不能作为默认路径（`docs/process-aware-nesting-improvement-plan.md`）。
- `--cold-start` 进一步忽略 `drawing_profiles/validated_cases` 案例库与
  `output/.cache/audit` 审图缓存，只依赖原始 DXF + 画像配置。
- 报告、CLI 输出与工作流会话都要标注 `layout_source`
  （`algorithm` 或 `template:<名称>`），避免把历史复刻当成算法能力。

加工方式由 `--process` 或 `NestingProfile.processing_class` 决定：

- `bridge`：走 `src/nesting.py`，多轮贪心 BFD + 大邻域搜索（LNS）
- `waterjet` / `laser`：走 `src/deepnest_engine.py`，Bottom-Left Fill + 滑动压实
- `--pairing`：水刀 DeepNest 分支先做相同形状 180° 共边配对预排
- `--no-rotation`：强制 `rotation=[0]`、`arbitrary_rotation=False`
- `--kerf`：桥切刀路材料损耗，不计为零件间隙；零件净尺寸，排板仍按零间隙共边。
- `--sheet-oversize`：桥切大板长/宽让尺，如 `30` 或 `30,30`；让尺只用于容纳刀路，
  不放大零件可用的标称尺寸；默认 `0` 时不让尺。
- `first_part_left_edge=True` 时，每张板首件最长直边贴大板左边缘；
  禁止旋转时若原图最长边不是竖直方向，该规则无法保证，会回退为普通左下放置

桥切机原算法：

1. 创建 Part 对象：outer_polygon 用于碰撞，holes 跟随变换
2. 两条布局路径同台竞争（取板数最少，其次制造评分/紧凑度）：
   - 多轮贪心配置：sort（short/area/long/jitter）× mode（skyline/column/contact）× seed
   - 逐板束搜索（`beam_nesting.py`，默认开启）：每开一张新板用束搜索找
     "当前库存能摆出的最满布局"，强项是跨尺寸档的分组配方；
     `--no-beam` 关闭，`--beam-width` 调宽度（默认 full=1000 / quick=400）；
     `first_part_left_edge` 约束下自动跳过
3. improve_budget：120~180 秒大邻域搜索（LNS 子问题求解器已换为束搜索，
   子集 ≤40 件时启用，修复了旧版对 CGR45 零收益的问题）
4. 每轮按排列键贪心放置，旋转角度由 NestingProfile.rotation 控制
5. 精确校验：边界 + DE-9IM 重叠检测
6. 排板结束后先报告使用大板数量，用户确认后再执行后处理推板；`--no-confirm` 可跳过确认
7. 若指定 `--kerf`，排板后按每张板的贯穿通切刀数校验实际材料长度，
   材料长度 = 零件外包络跨度 + 通切刀数 × kerf，必须 ≤ 标称尺寸 + 让尺

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

# 冷启动：忽略历史模板/已验证案例/审图缓存，只依赖原始 DXF + 画像配置
python main.py input.dxf 3200 1800 20 --cold-start --profile quick --report-only

# 显式复用已验证模板布局（默认关闭，默认只用算法排板）
python main.py input.dxf 3200 1800 20 --use-template --no-confirm
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

### 水刀/激光引擎的角度策略：边对齐，不要等步长铺满 360°
`src/deepnest_engine.py` 默认 `angle_mode="edge"`：候选旋转角来自"让零件最长
直边与板边平行"（并始终包含 0/90/180/270），不再是等步长枚举。CGR45final
（203 件，3200x1800）实测：

| 角度集 | 板数 | 出材率 | 耗时 |
|---|---:|---:|---:|
| 4 角 (0/90/180/270) | 54 | 67.55% | 8.7s |
| 24 角 (15° 网格，旧 quick) | 61 | 59.80% | 35.4s |
| 72 角 (5° 网格，旧 full) | 61 | 59.80% | 104.3s |
| **边对齐，最长 8 边（现默认）** | **54** | **67.55%** | **10.0s** |

结论：**细化角度不但没有更密，反而更差**（贪心近视——更多旋转让它先把零件
放歪、堵住后续空间），5° 相对 15° 更是纯浪费（板数相同、多 69s）。
多轮排序也不是杠杆：4 角 1 轮 54 张 / 4 轮同样 54 张。

实现要点：

- 参与边对齐的直边按长度降序取前 `_MAX_EDGE_ANGLES=8` 条——上限 16 会退化到
  55 张且更慢，过短边带来的方向是噪声；
- 曲线兜底用「最长 8 条边占外轮廓周长的比例」判定（`_EDGE_COVERAGE_MIN=0.6`），
  **不能**用「边方向数」判定：CAD 导出的矩形常带几十个冗余顶点/微段，方向数
  很多但长边占比接近 1.0，属于多边形而非曲线件（CGR45 有 37 件如此，误判会
  多花 1 张板）；
- `angle_mode="grid"` 保留旧的等步长枚举（需要全角度扫描时显式使用），
  `"hybrid"` 为边对齐 ∪ 网格；`rotation_step` 只在 grid/hybrid 下生效；
- 未传 `part` 时 `angle_candidates()` 保持旧的网格行为，兼容既有调用方。

### 口径必须写清：大板标称尺寸 ≠ 让尺 ≠ 刀缝
登记/对比排板结果时（benchmark、worklog、报告）必须区分三者：

- `sheet`：大板**标称**尺寸，如 3200x1800；
- `sheet_oversize`：让尺（如 30,30；让尺只用于容纳刀路，不放大零件可用尺寸）；
- `kerf_mm`：刀缝（桥切常为 5mm）。

不要把"3200x1800 + 让尺 30"写成 3230x1830 当成板尺寸。历史数字必须带口径才
可比（例：东莞奥特莱斯2 的 53 张 / 68.86% 是 **3200x1600** 口径，与 3200x1800
不可直接比较）。

### 桥切排板质量的杠杆在"分组"，不在放置准则（2026-09-25 束搜索改造）

CGR45 的诊断结论：算法 52 张的填充分布**均匀**（53~79%，无一张差板），
模板 50 张分布形状相同、只是整体高 ~3pp——差距在"哪些零件共享一张板"
（跨尺寸档互锁配方，如 `1892x978 横 + 978x1791 竖 + 1819x648 横`），
所以拆板重排（LNS/回看）的小邻域在结构上补不上，此前所有 2→1 / 3→2
穷举失败都是这个原因。

解法（`src/beam_nesting.py`，桥切第二布局路径，默认开启）：

- 每开一张新板用束搜索找"当前库存能摆出的最满布局"：状态 = 各尺寸档
  剩余 + 空闲矩形 + 已放，扩展 = 空闲矩形 × 尺寸档 × 取向，按 bbox 已用
  面积保留前 beam_width 个布局；
- **尺寸档必须带形状签名**：同 bbox 但缺口方向不同的镜像 L 件必须分档，
  否则按代表件算出的缺口空区套到镜像件上会造成重叠（已有回归测试）；
- L 形件 bbox 内、多边形外的矩形缺口**回收为空闲矩形**，小件可直接
  放进缺口；合法性由"空闲矩形减法"构造保证，精校验在主流程兜底；
- 难排件两遍法（尾板件加权先排，两遍取较优）+ 多板耦合尾解
  （库存 ≤24 件时剩余件同时排进 K 张板，消灭尾部卡死）；
- LNS 子问题求解器同步换为束搜索（子集 ≤40 件，签名缓存），修复旧版
  "贪心子求解器找不到跨板重排机会"导致的零收益。

CGR45 @ 3200x1800 冷启动：贪心 52 → **束搜索 51 → 回看 50 / 72.95%
（纯算法追平已验证模板）**。已验证无效的方向：按剩余面积摊匀的
自适应 cap、"可用空闲面积"评分（56 张，更差）、最差板子集压缩穷举
（最差 3→2 到 6→5 需 95%+ 填充，几何不可行；混合选择 0/12）。
压缩靠"干净尾板 + 多边形精确回看"，不是靠子集重排。

### 加工方式与场景：台面走桥切，异形才走水刀/激光
**只有不规则的规格板（异形件）才用水刀/激光；台面板以圆盘锯（桥切）为主。**

- 桥切（bridge）是主路径，CGR45（203 件台面板）属于这一类；
- 水刀/激光只用于异形件（弧边、造型边）；拿台面图纸去比水刀引擎能力是错误对比；
- 当前 `Part` 模型**没有加工方式字段**，`NestingProfile.processing_class` 是
  整单唯一开关 → 同一张图里台面 + 异形混排时只能人工拆图，或整单用一种工艺
  （按件分派是待确认的架构改动）。

### 水刀引擎的时间预算目前不生效（死参数）
`nest_parts_deepnest(improve_budget=..., configs=...)` 两个参数**从未被使用**，
而 `workflow.py` 的水刀路径会传 `improve_budget=budget`（CLI 默认 180s）——
即用户要求的长时搜索被静默丢弃。改动水刀质量时必须先看这一点，不要以为
拉大 `--budget` 会有收益。

已隔离验证过、对 CGR45 板数**无效**的开关（保留为显式实验入口，默认 legacy）：
`score_mode`（bottom_left/skyline/column/contact）、`candidate_order`、
`use_reflex`、`low_fill_rework`。该案例的板数由贪心"换板后不回头"的结构决定；
要再降板数需要批量候选构建 + 统一滑动，或真正的 LNS 跨板重排。

### 画像排序用指纹分层，不能只看图层名
`rank_profiles()` 只按"图层名是否命中"打分时，同一类图纸（0 层 + 编号层 +
建层级）会得到完全相同的分数，并列后按 `load_profiles` 的字典序取第一个，
结果 CGR45 被误配成 canada3l 画像。现在的规则：

- 画像带 `fingerprint` 且与图纸指纹相似度 ≥ `FINGERPRINT_MATCH_THRESHOLD`（60）
  时进入优先层；低于阈值**完全不加分**（否则该画像会对所有结构相近的图纸
  产生虚假优势）；
- 结构性布尔特征不一致必须**扣分**：`has_circle` 不一致 −15（有无盆孔/
  龙头孔是工艺上区分度最大的特征），`has_lwpolyline` / `has_line` 不一致 −5。
  只做集合 Jaccard 时 CGR45 与 countertop2 的相似度仍有 64.8，会互相抢占；
- 排名出现同分时**必须打印并列告警**，不能静默按文件名选；
- 画像 `fingerprint` 与图纸指纹同源（`drawing_profiles/validated_cases/*.json`），
  回填前要用 sha256 核对该案例对应的就是这份图纸（mercury 的案例指纹对应原始
  图纸，与编号后样本不一致，不回填）。

### 层级归并的 children 索引必须用 hierarchy 取值
`_build_part_hierarchy()` 返回的 `children` 是**按面积降序排列后的序号**，
取子级必须 `hierarchy[child_index]["poly"]`（对照 `dxf_reader.read_dxf` 的正确
写法）；并且被父轮廓直接包含的子轮廓在几何上就是孔洞，**不能再用分类器的
`role == "hole"` 二次过滤**。两者叠加会让孔洞被静默丢弃：CGR45 曾把 153 个
画成闭合多段线的盆孔整体丢掉（识别结果 holes 153/306，出材率被高估、
排板 DXF 漏孔）。画像 `expected_counts` 必须进入自动化断言。

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
