# 项目整体改进路线图

> 更新时间：2026-08-30
> 范围：以当前仓库状态为基线，按优先级推进；每完成一项勾选并记录验证结果。

## 当前基线

- 全量测试：`python -X utf8 -m pytest -q` 通过，当前 `171 passed`。
- 多案例回归脚本：`scripts/check_output_cases.py`，覆盖加拿大 3L、CGR45、Block D、水星、东莞奥特莱斯 1/2。
- 桥切 `min_gap` 已内建到排板引擎，后处理已对靠边、对齐、通切加入间距感知。
- DXF 生成前检查已落地：`main.py` 写出 `*_postprocess_check.json`，几何校验失败阻止最终 DXF。
- 水星异常闭合顶点恢复已固化：`src/dxf_reader.py` 支持首点重复、尾点重复、连续重复点清理，`read_dxf_with_profile()` 稳定返回 1258 个面板。

当前已知短板：

- `main.py` 仍约 1534 行，排板、后处理、写文件编排尚未完全拆出。
- 水星完整桥切排板回归约 9~10 分钟，慢案例策略仍不便于高频迭代。
- 水星顶点恢复已覆盖已知三类模式，新异常顶点仍需按文档补回归夹具。
- SaaS 组件主要是契约与内存实现，尚未接入真实 Worker、持久化队列与外部租户存储。
- 加拿大 3L 的 22 张最优结果仍依赖 `--budget 180`，短预算稳定性待验证。

## 执行顺序

### P0：立即修复工艺正确性与回归闭环

- [x] **修正 `part_not_on_edge` 诊断口径**
  - 目标：区分“真正漏推”和“密排内部零件无法贴边”。
  - 步骤：在 `src/postprocess.py` 增加“是否还能向最近边滑动”的检测；仅当存在未利用滑移空间时才报告。
  - 验收：水星案例不再大量误报；真正漏推的板仍能检出。
  - 结果：`mercury_bridge` 从 34 项 `part_not_on_edge` 降至 0 项，且全量测试 `118 passed`。
  - 验证：`python -u -X utf8 scripts/check_output_cases.py mercury_bridge`。

- [x] **继续消除 CGR45 通切未对齐**
  - 目标：减少或清零 `through_cut_not_aligned`，无法消除时在检查 JSON 写明原因。
  - 步骤：分析 `src/postprocess.py` 的 `_through_cut_sheet`、`_snap_straight_axis`；保持 `min_gap` 和边界约束。
  - 验收：`cgr45_bridge` 几何通过，`through_cut_not_aligned` 为 0 或全部有可解释原因。
  - 结果：`through_cut_not_aligned` 已降至 0；剩余 3 项已分类为 `through_cut_constrained`，并在检查 JSON 写明约束原因。
  - 验证：`python -u -X utf8 scripts/check_output_cases.py cgr45_bridge`。

- [x] **补齐水刀/激光可制造性检查**
  - 目标：覆盖 `no_rotation`、最长直边贴边、共边/刀缝等工艺项。
  - 步骤：为 `deepnest_engine` 输出增加诊断，或在 `diagnose_postprocess` 旁新增水刀专用检查；纳入东莞奥特莱斯 1/2。
  - 验收：`outlets1_waterjet`、`outlets2_waterjet` 输出结构化水刀工艺检查。
  - 结果：新增 `first_part_left_edge` 与共线边统计；东莞 1/2 均为 0 工艺警告，`first_part_left_edge_failed=0`。
  - 验证：`python -u -X utf8 scripts/check_output_cases.py outlets1_waterjet outlets2_waterjet`。

- [x] **固化 `--check-only` 退出码契约**
  - 目标：几何失败返回非 0，仅工艺警告返回 0，便于 CI 拦截。
  - 步骤：在 `main.py`/`scripts/check_output_cases.py` 明确区分 error 与 warning；补充测试。
  - 验收：`check_only=True` 的几何错误可被调用方按退出码识别。
  - 结果：新增 `check_only_exit_code()`；几何失败返回 2，仅工艺警告返回 0，并补充测试。
  - 验证：`python -X utf8 -m pytest tests/test_postprocess_check.py -q`。

- [x] **固化水星异常闭合顶点恢复**
  - 目标：视觉闭合但 `closed=False`、顶点多出的 L 型台面板不因几何构造失败被漏识。
  - 步骤：在 `src/dxf_reader.py` 统一清理连续重复点、首点重复前缀、尾点重复后缀，并用 `buffer(0)` 修复无效几何。
  - 验收：`drawing_profiles/mercury.json` 只读识别返回 1258 个面板；新项目可按画像配置适配，无需项目特判。
  - 结果：新增 `_dedupe_consecutive_points()`、`_closed_loop_points()`；`read_dxf_with_profile()` 返回 1258；补充三类异常顶点回归用例，全量测试 171 passed。
  - 验证：`python -X utf8 -m pytest -q`；水星原图只读识别结果 `1258`。

### P1：短期工程化与算法策略

- [x] **拆解 `main.py` 单点编排**
  - 目标：按 `docs/saas-architecture-goal.md` 拆成确定性工具，`main.py` 只做编排和 CLI 兼容。
  - 步骤：先抽 `analyze_drawing`、`match_profile`、`read_dxf`、`assign_numbers`、`nest_parts`、`postprocess`、`write_dxf`、`write_report`；保持现有调用不破坏。
  - 验收：新增工具可独立测试；`main.run()` 行为不变；全量测试通过。
  - 结果：新增 `src/workflow.py::prepare_drawing()`，抽出读图/画像/编号/材料分组；`resolve_drawing_profile`、`apply_material_grouping` 迁入 workflow 并从 main 兼容导入；`main.run()` 读图阶段改为调用 `prepare_drawing()`。
  - 验证：`python -X utf8 -m pytest -q` 129 passed；`blockd_bridge` 回归通过。

- [x] **引入 `ProjectConfig` 配置模型**
  - 目标：将大板尺寸、让尺、旋转、间距、分组、编号规则、后处理参数、优化参数外置为 YAML/JSON。
  - 步骤：实现 Pydantic 模型与加载器；`main.run()` 接受配置对象；保留 CLI 覆盖参数。
  - 验收：同一项目可用配置文件复现同一排板结果；CLI 参数仍兼容。
  - 结果：新增 `src/project_config.py`、`run_with_config()`、`--config`，CLI 覆盖优先，测试通过。
  - 参考：`docs/constraint-externalization.md`。

- [x] **外置读图与编号规则**
  - 目标：消除“图层名含‘编’”、户型/套等硬编码回退。
  - 步骤：将编号匹配策略、搜索半径、户型标签正则、排除线型等迁入 `DrawingProfile` 或项目配置。
  - 验收：新项目无需改 Python 即可配置编号规则；现有画像项目行为不变。
  - 结果：`dxf_reader.read_dxf()` 新增 `number_layer_keyword`、`room_label_keyword`、`room_label_exclude_keyword`、`room_label_normalizations`、`room_max_distance`；`ReaderConfig.to_read_options()` 外置这些参数，`run_with_config()` 自动透传，旧 CLI/画像行为保持兼容。
  - 验证：`python -X utf8 -m pytest -q` 132 passed；`blockd_bridge` 回归通过。

- [x] **固化排板质量策略**
  - 目标：解决“为什么一开始不是 22 张”的体验问题。
  - 步骤：增加质量档位、自动预算、上一轮结果热启动、早停；统一比较“板数 → 紧凑度 → 制造性”。
  - 验收：`--budget 180` 的优质结果可被缓存/复用；默认策略能在可预期时间内自动逼近高质量方案。
  - 结果：新增 `--quality fast/balanced/best` 与 `QUALITY_PRESETS`；`OptimizerConfig` 支持 quality；`nest_parts()` 增加 `min_sheets` 早停和 `_lns_improve(warm_start=...)` 跨试验热启动；同板数仍按制造评分再按紧凑度择优。
  - 验证：`python -X utf8 -m pytest -q` 135 passed；`blockd_bridge` 回归通过。

- [x] **实现 `group_mode="one_set_per_sheet"`**
  - 目标：同户型/同套件尽量一板排完，避免跨板拆套。
  - 步骤：从编号/户型标签生成 `group_id`，在排板前按组约束分配；与材料分组不冲突。
  - 验收：指定画像下同组零件优先同板，板数口径和报告可复核。
  - 结果：`Part.group_id` 新增；`DrawingProfile.group_id_pattern` 可配置同套识别；`workflow.assign_group_ids()` 自动生成 group_id；`_nest_one_group()` 在 `one_set_per_sheet` 下按 group 分批排板，普通模式行为不变。
  - 验证：`python -X utf8 -m pytest -q` 139 passed；`blockd_bridge` 回归通过。

### P2：中期工作流与确定性执行

- [x] **实现 `WorkflowSession` 状态机**
  - 目标：支持 `uploaded → analyzed → profile_matched → read → audited → numbering_confirmed → nested_reported → postprocess_confirmed → completed`。
  - 步骤：先做进程内状态与阶段校验，再持久化；支持 `blocked/cancelled/backtracked`。
  - 验收：各阶段不可跳步；人工确认点生效；可记录每阶段摘要、耗时和校验结果。
  - 结果：新增 `src/workflow_session.py`，提供 `WorkflowStage`、`StageRecord`、`WorkflowSession.transition/backtrack/block/resume/cancel/to_dict`；非法跳步和终态转换会被拒绝，历史记录含摘要与耗时。
  - 验证：`python -X utf8 -m pytest -q` 144 passed。

- [x] **引入内容寻址产物**
  - 目标：中间文件采用 `project_id/stage_version/artifacts` 结构，回退等于切换 `artifact_id`。
  - 步骤：为关键产物计算 `input_digest/output_digest`；输出目录避免同名覆盖。
  - 验收：相同输入 + 工具版本 + seed 得到相同 artifact；重跑命中缓存。
  - 结果：新增 `src/artifact_store.py`，提供 `ArtifactStore.put_json/put_text/put_bytes/read_*`；文件名含内容 digest，同版本同内容幂等，不同版本不覆盖，`ArtifactRef` 记录 `artifact_id/digest/path/stage_version`。
  - 验证：`python -X utf8 -m pytest -q` 147 passed。

- [x] **建立慢回归与基线测试**
  - 目标：真实 DXF 案例不依赖手工逐个运行。
  - 步骤：将水星、东莞等慢案例拆成独立任务；增加 golden/digest 基线；CI 设置超时。
  - 验收：CI 可自动发现几何/工艺回退；真实案例运行结果可对比。
  - 结果：`scripts/check_output_cases.py` 新增 `--require-all`，缺文件可从 SKIP 变为 FAIL；新增 `tests/test_regression_cases_registry.py` 固定必跑案例清单与唯一性，避免手工逐个核对案例名。
  - 验证：`python -X utf8 -m pytest -q` 149 passed。

### P3：长期 SaaS 与多租户边界

- [x] **拆分服务与任务队列**
  - 目标：读图、编号、排板、后处理、写文件作为独立任务，只通过契约通信。
  - 步骤：先定义任务 schema，再接入队列和 Worker；保持本地 CLI 兼容。
  - 验收：单任务可重试、超时、降级，任务调用可审计。
  - 结果：新增 `src/task_queue.py`，提供 `Task/TaskStatus/InMemoryTaskQueue`；结构化 payload、FIFO 执行、超时与异常状态、attempts 与 audit_log；本地 CLI 暂不切换，仅作为后续 Worker 边界契约。
  - 验证：`python -X utf8 -m pytest -q` 153 passed。

- [x] **建立信任与资源边界**
  - 目标：DXF 解析在独立进程/容器运行，限制文件大小、实体数、解析时间。
  - 步骤：在读图入口增加配额与沙箱；异常输入不拖垮主服务。
  - 验收：恶意/超大 DXF 被拒绝或隔离，不产生未受控资源占用。
  - 结果：新增 `src/input_guard.py`，提供 `DXFInputLimits/check_dxf_file/guarded_read/DXFInputError`；`prepare_drawing(input_limits=...)` 在读图入口可选启用文件大小、实体数、解析时长限制，本地 CLI 保持原默认路径。
  - 验证：`python -X utf8 -m pytest -q` 158 passed；`blockd_bridge` 回归通过。

- [x] **实现多租户与配额**
  - 目标：每项目独立存储、队列、凭证、配额，禁止跨租户读取。
  - 步骤：项目级存储桶/目录、租户鉴权、资源配额和审计日志。
  - 验收：多租户项目互不干扰，操作可追踪。
  - 结果：新增 `src/tenant_store.py`，提供 `TenantStore/TenantQuota/TenantError`；tenant/project id 校验、项目配额、租户内项目列表、项目级 `ArtifactStore` 与审计日志；路径按租户隔离。
  - 验证：`python -X utf8 -m pytest -q` 163 passed。

- [x] **引入 AI 规划层**
  - 目标：AI 只生成结构化工具调用和配置，不直接执行生产代码。
  - 步骤：定义工具白名单、前置校验、阶段闸门和结构化反馈。
  - 验收：AI 无法绕过工作流状态机或直接写生产文件。
  - 结果：新增 `src/ai_planning.py`，提供 `ToolCall/PlanValidation/TOOL_WHITELIST/STAGE_TOOLS/validate_tool_call/validate_plan`；按 `WorkflowStage` 限制工具，生产写文件工具只能在 `postprocess_confirmed` 阶段调用。
  - 验证：`python -X utf8 -m pytest -q` 168 passed。

## 后续待改进

- 水星完整桥切排板回归约 `9~10` 分钟，仍不适合高频迭代；继续优化桥切贪心/LNS 的几何调用次数与候选裁剪。
- `main.py` 仍约 `1534` 行，读图阶段已外置到 `src/workflow.py`，排板、后处理、写文件编排仍需继续拆解。
- 水星顶点恢复已覆盖已知三类模式，未来出现新异常顶点时按 `docs/drawing-intake-subsystem.md` 流程补最小回归夹具。
- `scripts/check_output_cases.py` 需进一步明确慢案例的 skip/require-all 策略与 CI 超时。
- SaaS 组件目前主要是契约与内存实现，尚未接入本地 CLI、持久化队列、真实 Worker 与外部租户存储。
- 加拿大 3L 的 22 张最优结果仍依赖 `--budget 180`，需验证短预算下能否稳定复现。

## 建议的第一轮执行

1. 从 P0 开始，先修 `part_not_on_edge` 诊断。
2. 再处理 CGR45 通切与水刀工艺检查。
3. 每完成一项跑对应案例和全量测试，并在本文件中勾选。

## 回归命令

```bash
python -X utf8 -m pytest -q
python -u -X utf8 scripts/check_output_cases.py canada3l_bridge
python -u -X utf8 scripts/check_output_cases.py cgr45_bridge
python -u -X utf8 scripts/check_output_cases.py blockd_bridge
python -u -X utf8 scripts/check_output_cases.py mercury_bridge
python -u -X utf8 scripts/check_output_cases.py outlets1_waterjet
python -u -X utf8 scripts/check_output_cases.py outlets2_waterjet
```
