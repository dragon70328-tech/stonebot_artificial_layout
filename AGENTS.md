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
    spec_list.py        # 规格板尺寸清单（材料 x 区域 分组 + 图纸要求提取）
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
    profile_proposal.py # 画像提议/登记：候选画像自动产出、expected_counts 复算、入库
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
    cli_profile.py      # 画像提议/登记 CLI（--propose-profile / --register-profile）
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
- 画像启用列头材料（`material_column_enabled`）时文件名带 `_房号材料`，
  并多写两个图层：`MATERIALS`（零件材料号，绿色，居编号下方，与零件同 GROUP）
  和 `MATERIAL_HEADER`（原图「材料：ST-x」列头标签按原坐标复写）

### Step 4.1: 规格板尺寸清单（审图参考，2026-10-16 起）

主流程与 `--audit` 在读取 DXF 后都会产出 `{name}_规格清单.csv/.json`
（`src/spec_list.py`），供人工审图参考：

- 按 **材料 × 区域** 分组列出规格板：长 x 宽、数量、单件净面积、面积小计、
  孔洞数、编号清单；末行合计。材料 = `Part.material_group`，
  区域 = `Part.material_column`（楼层列），都没有 = 空。
- **表面处理 / 磨边 / 纹理走向**：关键词扫描图纸 TEXT/MTEXT 提取
  （抛光/圆边/顺纹等），只提取原文不做推断，上下文窗口保留"不磨边"
  否定语境；**图纸未注明一律留空**；证据原文存 JSON `annotations_evidence`。
- 尺寸口径：外轮廓外包络跨度，长边在前（长>=宽），0.1mm 精度；
  异形件（外轮廓面积/外包络 < 0.98）备注列标"尺寸为外包络"。
- CSV 为 utf-8-sig（Excel/WPS 直接打开）；`--audit` 同批还产出原位 DXF
  （此前只有主流程有）；清单生成失败降级为警告，不阻断审图/排板主产物。

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
- `--kerf`：桥切刀路材料损耗（圆盘锯常 3~5mm），不计为零件间隙；零件净尺寸，
  排板仍按零间隙共边。水刀/激光按口径"刀缝可不计"，不做刀缝材料校验。
- `--sheet-oversize`：大板让尺，如 `30` 或 `30,30`，**每维度总量 +30（不是每边
  +30）**。让尺是标称之外的实物余量，只为消化刀缝：默认既不进零件可利用尺寸，
  也不进 DXF 外框，只把刀缝材料校验的允许尺寸放宽到实物尺寸（可利用 + 让尺）。
- `--usable-includes-oversize`：显式声明本次排板"大板可利用尺寸包括让尺"
  （零件可用标称+让尺，此时实物不再有额外余量留给刀缝）。
- `--kerf-reserve`：`--sheet-oversize` 的兼容别名（默认 0）；两者都给时以
  `--sheet-oversize` 为准，不做叠加。
- `first_part_left_edge=True` 时，每张板首件最长直边贴大板左边缘；
  禁止旋转时若原图最长边不是竖直方向，该规则无法保证，会回退为普通左下放置

桥切机原算法：

1. 创建 Part 对象：outer_polygon 用于碰撞，holes 跟随变换
2. 两条布局路径同台竞争（取板数最少，其次制造评分/紧凑度）：
   - 多轮贪心配置：sort（short/area/long/jitter）× mode（skyline/column/contact）× seed
   - 逐板束搜索（`beam_nesting.py`，默认开启）：每开一张新板用束搜索找
     "当前库存能摆出的最满布局"，强项是跨尺寸档的分组配方；
     `--no-beam` 关闭，`--beam-width` 调宽度（默认恒为 1000；
     `--quick` 只减少配置数与预算，**不再**把 beam 静默降到 400——
     2026-10-14 实测：同一张 CGR45 加 `--quick` 会从 50 张掉到 51 张）；
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

# 标准口径说明：3200x1800 大板、让尺各 30mm、刀缝 5mm、可利用尺寸不含让尺
# （零件排 3200x1800，刀缝校验允许 3230x1830，外框画 3200x1800）
python main.py input.dxf 3200 1800 20 --cold-start --drawing-profile canada3l \
    --profile min_sheets --budget 60 --kerf 5 --sheet-oversize 30,30 --no-confirm

# 同上，但本次"可利用尺寸包括让尺"：零件可用 3230x1830，刀缝没有额外余量
python main.py input.dxf 3200 1800 20 --drawing-profile canada3l \
    --profile min_sheets --kerf 5 --sheet-oversize 30,30 \
    --usable-includes-oversize --no-confirm

# 显式复用已验证模板布局（默认关闭，默认只用算法排板）
python main.py input.dxf 3200 1800 20 --use-template --no-confirm
```

## 关键经验

### 画像库积累：提议 → 核对 → 登记（2026-10-16 起）

读图是项目的核心竞争力：只有分析图纸准确才能排板。画像库是数据资产，
新画像**不再手写**，走三步流水线：

```bash
python main.py new.dxf --propose-profile            # 1. 自动提议候选画像
# 人工核对问题清单（件数/编号格式/排除项），必要时编辑草稿
python main.py new.dxf --register-profile <草稿路径> # 3. 复算后登记入库
```

- `--propose-profile` 自动产出：panel_layer(s)、number_layers、label_pattern
  （字符类游程归纳最紧正则，数字段+大写段自动命名 material/shape）、
  closed_tolerance（缝隙 p95）、small_area_threshold（面积断层 ≥8 倍）、
  build_hierarchy（有 CIRCLE 即 True）、exclude_linetypes（全部非实线）、
  expected_counts（候选参数实读结果，标"待确认"）。
- `--register-profile` 的硬门槛：登记前复算 expected_counts，**与草稿不一致
  默认拒绝**（`--force` 例外并写"[登记警告]"进 notes）；同名画像默认拒绝
  覆盖。写画像 JSON（含 fingerprint）+ `validated_cases/{name}.json`
  （含 source_sha256）。
- 入库标准：画像 + fingerprint + expected_counts + notes（写清取值依据，
  citizen.json 是范本）+ 已验证案例，五件齐才算积累完成。
- 库的健康度看两个收敛指标：新图纸**命中率**（已有画像直接读准的比例）
  应上升，**新增画像率**应下降。
- 冒烟验证：CGR45final 提议结果与手写画像逐项一致（203/306/203，
  正则功能等价）。

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

### 列头材料：按质心 x 归列，边界取楼层标签左边缘
多层平面图（如 citizen）x 方向每列 = 一个楼层，列头同时写 `L-XX LEVEL` 与
`材料：ST-x`。画像开 `material_column_enabled` 后，零件按**质心 x** 归属到
所在列，材料号写进 `Part.material_group`，原位 DXF 也据此标注。

- 列边界取楼层标签的**左边缘**（这些标签是左对齐 TEXT，`insert.x` 就是左边缘），
  与 `_analysis/material_usage_report.py` 同口径；用包围盒中心当边界会让每列
  右移半个字宽，边界附近的零件会被错分到相邻列、甚至落到第一列之外变成"无材料"
  （2026-10-14 踩过：3524/3525，`1002-01` 掉出列外）。
- `material_group_enabled`（按编号前缀取材料）与 `material_column_enabled`
  （按列取材料）是两条来源；`apply_material_grouping` 优先保留读图阶段已写入的
  `Part.material_group`，否则 citizen 这种"编号是房号"的图纸会被判无材料而整体跳过。
- `ST-7/ST-8` 表示该列两种料都用，图面没有拆分依据，只能整体记作一组，
  要落地"不同材料不共板"必须先由用户给出拆分方式。
- 分单排板用 `--material ST-6`（多个用逗号分隔）过滤零件：材料组找不到时
  直接报错并列出可用材料组，不静默排空；过滤后输出目录里的原位 DXF /
  排板 DXF / 报告只含该材料的零件。

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

### 口径必须写清：标称尺寸 / 让尺 / 刀缝 / 可利用尺寸（2026-10-13 用户锁定）

用户今后按这个格式说明大板口径，四件事必须齐：

> 大板尺寸 3200x1600，长宽让尺各 30mm，刀缝 3mm，
> 本次排板大板可利用尺寸不包括让尺（或包括让尺）

规则：

- **大板尺寸 = 标称**（3200x1600），也是登记/对比时写的板尺寸；
- **让尺 = 标称之外的实物余量**，`30` 表示该维度总量 +30（实物 3230x1630），
  **不是每边 +30**（那是 3260x1660）；让尺存在的意义只有一条——消化切割刀缝；
- **刀缝**：圆盘锯（桥切）常 3~5mm；**水刀刀缝可不计**，不做刀缝材料校验；
- **可利用尺寸 = 零件实际能占的范围**，**默认 = 标称（不含让尺）**；
  只有用户明确说"可利用尺寸包括让尺"时才 = 标称 + 让尺。

代码落点（`main.resolve_sheet_caliber` 是唯一口径入口）：

- 排板空间 = 可利用尺寸；
- 刀缝校验允许 = 实物尺寸 = 可利用尺寸 + 让尺（让尺只计一次）；
- DXF 外框 = 可利用尺寸（默认即标称 3200x1600）；
- 报告 `sheet_dimensions` 必须能还原口径：`width/height`（标称）、
  `sheet_oversize_x/y`（让尺）、`usable_width/height`（可利用）、
  `usable_includes_oversize`、`physical_width/height`（实物 = 标称 + 让尺）、
  `kerf_allowance_x/y_mm`（= 实物 − 可利用）。
- API/agent 路径（`src/tool_handlers.py`）：排板空间取契约的 `SheetSpec.usable_*`
  （工具参数 `sheet_width/sheet_height` 只在没有契约时回退）；桥切刀缝材料校验
  在 `postprocess` 阶段执行，失败写进 `ctx.validation_errors` → `write_dxf`
  拒绝出图；水刀/激光跳过（刀缝可不计）。

**清单排板（`--list-nest`）是已确认的例外：可利用尺寸包括让尺**（可用尺寸 =
标称 + 让尺，`--oversize` 就是这个让尺）。结论文本会打印
`大板口径: 标称 …，让尺 …（每维度总量），可利用尺寸 …（含让尺），刀缝 …`，
并另外给出标称计价口径。两条路径的默认口径不同，**不要互相"修"成一致**。

历史数字必须带口径才可比，且注意口径变更会作废旧数字：

- 东莞奥特莱斯2 的 53 张 / 68.86% 是 **3200x1600** 口径，与 3200x1800 不可比；
- 加拿大3L 的 **22 张 / 76.68%** 是在旧行为（让尺当"更大的料"、零件排进
  3230x1830）下测的，与现在的口径不可比；同口径（3200x1800）的实测是
  **24 张 / 72.13%**。要复现 22 张必须显式声明"可利用尺寸包括让尺"。

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

### 布局择优要带"尾板项"；预算要确定而非墙钟（2026-10-14 P0）

最少大板数是**离散目标、有平台期**：板数相同的布局总浪费完全一样，真正的分界在
"最弱的那张板能不能被并掉"。因此：

- `nesting._layout_key` = **（板数 → 最弱板已用面积升序 → 制造评分 → 紧凑度）**，
  贪心配置之间、束搜索与贪心之间、以及多次 trial 之间都用同一个键择优；
  此前尾板完全不参与择优，50 张是"撞"出来的。
- `nesting._lns_improve` 的预算改为 **迭代数 + 连续无改进轮数**（默认 40 / 12）
  为主控且确定；`--budget`（墙钟）降级为护栏，触发时在报告
  `parameters.optimizer.lns_budget_exhausted` 标记"本次不可复现"。
- 回看（`nesting_reviewer.find_reductions`）默认**不再设墙钟上限**：
  `--review-budget` 默认 `0`，只受 `max_reductions=5` / `max_donor_attempts=8` /
  `max_donor_parts=6` / `max_nodes_per_donor=3000` 这些确定性上限约束。
  旧的 120s 截断会让同一张图的回看结果随机器负载漂移（实测 quick 那次正是跑满
  120s 被切断）。上限必须同时限"试几个 donor"和"单个 donor 的节点数"：donor 拆除
  的代价 ≈ 零件数 × 目标板数 × 每件候选数，50+ 张板的单子上单 donor 就能吃掉几十秒
  （实测东莞奥特莱斯2：不设上限时回看 >10 分钟未完成，收紧后 ~10s）。
- `--quick` 只影响配置数与预算，不改变搜索空间（见 Step 5 的 beam 宽度说明）。

验收（CGR45final @3200x1800 冷启动）：`--profile min_sheets --budget 60` 连跑两次
**逐件完全一致**（51 → 回看减 sheet[40] → **50 张 / 72.95%**，
`lns_iterations=25`、`budget_exhausted=False`），`--quick` 也同样是 50 张。

### 水刀路径的零件外轮廓可能是 MultiPolygon（2026-10-14 修复）

`deepnest_engine._edge_segments` 直接取 `poly.exterior`，遇到被强制闭合或被 CAD
合成一个实体的**复合轮廓**（MultiPolygon）会抛
`AttributeError: 'MultiPolygon' object has no attribute 'exterior'`，
`--process waterjet --pairing` 在东莞奥特莱斯2 上必崩。现已按几何类型展开子多边形。

同单水刀基线（1109 件 / 3200x1600 / `--pairing --kerf 5 --sheet-oversize 30,30 --quick`）：
**53 张 / 68.86% / 123.1s**（理论最少 39 张），与 9-19 同结果但快约 3 倍；
每材料 01B 1 / 02B 6 / 03B 9 / 04B 13 / 05B 24 张。异形路径目前**没有搜索层**
（单趟 BLF + 滑动压实，`improve_budget`/`configs` 是死参数），是下一步的主要杠杆。

### 多板联合重排压缩：通道已建，但当前两单不触发（2026-10-14 P1）

`nesting._compress_sheets`：把 k 张填充最低的板**联合重排进 k-1 张**（`--no-compress` 关闭）。
它补上了 LNS 的一个机制缺口——LNS 里那次压缩的判定是 `len(got) <= k`，**并不要求少一张板**，
所以满板布局上几乎不会发生。新通道用 `_nest_single(max_sheets=k-1)` 硬性要求塞进 k-1 张，
并用子集束搜索兜底（贪心塞不下不代表塞不下）；`first_part_left_edge` 约束下跳过。

**口径陷阱（必读）**：可行性要用**外包络**面积（`_sheet_outer_area` = `pl.poly.area`），
不能用 `part.area`——后者是扣孔洞后的净面积（报告出材率用它）。CGR45 上两者差 12%
（外包络 235.9 m² vs 净 210.1 m²），用净面积会把可行性高估。

实测结果（离线探测已存布局，`_analysis/rea_youshi/probe_compress_offline.py`，
所有输入的"压缩前几何自检"均通过）：

| 布局 | 结果 | 尝试 | 耗时 |
|---|---|---:|---:|
| CGR45 50 张（桥切） | 50 → 50 未触发 | 28 | 7.9s |
| citizen ST-6 148 张（桥切） | 148 → 148 未触发 | 28 | 15.9s |
| 加拿大IFC 68 张（桥切） | 68 → 68 未触发 | 27 | 10.0s |
| 加拿大3L 24 张（桥切） | 24 → 24 未触发 | 42 | 56.3s |
| **东莞奥特莱斯2 53 张（水刀）** | **53 → 51 触发，几何校验通过** | 19 | 61.4s |

桥切四单不触发的根因是算术，不是记账：

| | 现状平均外包络填充 | 少一张板所需 | 子集压缩最低门槛 |
|---|---:|---:|---:|
| CGR45（50→49） | 81.9% | **83.57%** | k=4 需 92.1% / k=6 需 83.9% |
| citizen（148→147） | 87.6% | **88.21%** | k=3 需 83.5% |

回看也不是没搜尽：把上限放大到 120 donors × 20 万节点 × 400 候选仍是"未找到"
（`probe_review_budget.py`，26.9s 穷尽）。所以"少一张板"缺的是**打包效率**（平均填充 +1.2~1.7pp），
不是板数记账；CGR45 的人工验证模板同样落在 50 张，是同一结论的旁证。

**水刀单能触发**：该布局填充极不均（最低 **3.5%** —— 几乎空的一张尾板，中位 72.9%），
BLF 留下了可观的合并余地。注意：管线目前**只在桥切路径接入了 compress**
（`nest_parts_deepnest*` 还没有这个参数），所以这 −2 张目前只能离线复现；
把它接进异形路径是下一步的小改动。压缩重排会打散"共边配对"的摆法，
但零件本身是单独切割的，几何校验通过即合法。

**两个必须记住的坑**：

1. `beam_part_cap=60`：子集零件数超过它就不做束搜索兜底。东莞那张每板约 21 件，
   k=8 的子集接近 170 件——不做这个上限，这个默认开启的通道会在大单上跑 8 分钟以上。
2. 校验探测结果必须复用生产代码的 `_materialize` + `validate_nesting`：
   自己手写"平移 + 两两相交"时把内部约定的 `(x, y)` 当成 0 处理，会把整张板的零件
   挪到原点，凭空造出 1.1 万处"重叠"（本次真实踩过，差点误报一个假发现）。

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

### 会话接口取 job_id 的地方必须校验任务归属
`/conversations/{id}/respond` 的 job 动作允许 planner 在 intent 里显式给 `job_id`，
所以取 job_id 的 `_resolve_conversation_job()` 必须调用 `_require_job_tenant()`；
角色按 `_CONVERSATION_ACTION_ROLES` 与同名 REST 端点对齐（变更类动作 = operator，
`get_job` 与 `GET /jobs/{job_id}` 一样不要求角色）。漏掉这一步时，只要知道别的
租户的 job_id（形如 `agent-<文件名>-<时间戳>`）就能查询、确认、继续、回看、纠正
或取消其任务（2026-09-29 全系统核对发现，2026-10-13 修复，回归测试
`tests/test_conversation_api.py::test_respond_rejects_job_of_another_tenant`）。

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
- 口径：**可利用尺寸包括让尺**（可用尺寸 = 标称 + 让尺，`--oversize` 即让尺，
  用户 2026-10-13 确认）；与 DXF 排板默认口径相反，别照搬家口径去"统一"。
  结论里会打印标称/让尺/可利用尺寸/刀缝四项，并另给标称计价口径。
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

- Python 3.10+（部署镜像 `python:3.10-slim`；CI 跑 3.10 + 3.11 矩阵）
- ezdxf（DXF 读写）、shapely（几何计算）、numpy
- pymupdf（清单 PDF 解析）、fastapi + uvicorn（SaaS API）、pydantic（数据契约）

本地环境 2026-10-13 起固定（此前一直裸用系统 Python，没有虚拟环境）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

三份依赖文件的分工：`requirements.txt` 是**允许的版本区间**（人工维护的唯一来源）；
`requirements.lock.txt` 是**实测版本快照**（CI 与 Docker 构建用它锁版本）；
`requirements-dev.txt` = lock + `pytest` + `httpx`。
**测试依赖也是依赖**：`pytest` / `httpx` 长期漏声明，本地靠机器上恰好装着才绿，
CI 从第一次跑就是红的；改依赖时三份文件要一起看。
