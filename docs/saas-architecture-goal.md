# StoneBot 平台长期架构目标

## 目标定位

StoneBot 不只是一次性加工规划脚本、Web 服务或某个 Agent 的插件，而是一个平台无关、
可持续演进、可审计、可租户隔离的 DXF 审图与排板 Runtime。Codex、DeepSeek Harness、
Claude Code、Kimi Code、CLI 和 Web 都只是交互入口。

核心原则：**AI/Agent 只负责规划、语义理解和生成结构化意图，不直接执行生产代码、不直接
操作文件系统或数据库；真实识别、校验、修正、排板和交付由可信的 Python Runtime 完成，
并由工作流状态机统一调度。**

2026-10-30 起，核心判断标准调整为：

1. 一个平台无关的 StoneBot Core；
2. 一套版本化 MCP/JSON Schema 契约；
3. 多种只负责连接的薄适配器；
4. 关键闭环在 Core 内完成，不依赖 Harness 的模型循环强度；
5. 用户纠正经结构化和回归验证后形成版本化画像包；
6. 本地 Runtime、公司私有服务和云端服务共用同一核心。

完整方向见 `docs/product-direction-2026-10-30.md`。

## 当前阶段里程碑：读图优先

当前第一里程碑是建设强读图能力，现阶段只接受 **DXF** 一种输入格式
（DWG/PDF/手绘草图均已放弃，详见 `docs/drawing-intake-subsystem.md` 文首范围调整），并完成
“解析 -> 语义识别 -> 审图 -> 问题反馈 -> 修正 -> 复检 -> 提取加工数据 -> 深化图纸
-> 原位 DXF 确认”。详细设计见 `docs/drawing-intake-subsystem.md`。

## 五个支柱

### 1. 项目架构约束

- 代码按 `adapters / domain / application / infrastructure` 分层：
  - `dxf_reader`、`dxf_writer`：adapter。
  - `models`、`constraints`、`drawing_profile`：domain。
  - `nesting`、`deepnest_engine`、`pairing`、`postprocess`：application。
  - 文件存储、任务队列、SaaS 配置：infrastructure。
- 禁止反向依赖，例如 adapter 不得 import application 规则。
- 每个 SaaS 项目对应一个 `ProjectConfig`，由 JSON Schema/Pydantic 校验。
- 中间产物不可覆盖，采用 `project_id/stage_version/artifacts` 的内容寻址结构。
- AI 工具按当前工作流阶段白名单开放，未通过前置校验不能调用后续工具。

### 2. 本体 / 数据契约

- 建立版本化实体关系：
  - `Job -> Drawing -> DrawingProfile -> Part -> NestingResult -> Sheet`
  - `Part -> Holes`
  - `Part -> Number -> MaterialGroup`
  - `Drawing -> DrawingIssue`
- 以 Pydantic v2 或 JSON Schema 作为唯一数据契约源。
- 几何跨服务传输统一使用 `WKT/WKB + unit + tolerance + crs`，不传 Shapely 对象。
- 每个契约包含 `schema_version`、`job_id`、`artifact_id`、`input_digest`、
  `output_digest`。
- 本体为 AI 生成上下文包：当前实体、关系、允许操作、约束条件。

### 3. 工具 / 确定性执行

- 将 `main.run()` 拆成小型工具，例如：
  - `analyze_drawing`
  - `match_profile`
  - `read_dxf`
  - `audit_drawing`
  - `assign_numbers`
  - `nest_parts`
  - `postprocess`
  - `write_dxf`
  - `write_report`
- 每个工具声明输入 schema、输出 schema、所属阶段、副作用、超时、重试策略和幂等键。
- 执行层使用确定性 job：
  - `输入哈希 + 代码版本 + 工具版本 + seed` 生成 job key。
  - 相同 job key 命中缓存，不重复计算。
  - 优化器必须显式记录 seed、trials、budget、rotation 配置。
  - 排序、集合遍历、几何结果归一化，保证同输入同输出。
- AI 只提交 JSON 工具调用，由 Worker 执行；不允许 AI 在服务端直接运行 Python。

### 4. 调度循环

- 实现 `WorkflowSession` 状态机：
  - `uploaded -> analyzed -> profile_matched -> read -> audited -> numbering_confirmed
    -> nested_reported -> postprocess_confirmed -> completed`
  - 支持 `blocked / cancelled / backtracked`。
- 每次状态迁移由 orchestrator 校验，AI 不能直接改状态。
- 人工确认只放在关键门槛：编号确认、板数确认、后处理确认。
- 每个阶段记录输入摘要、工具调用、输出摘要、校验结果、耗时和进度。
- 内容寻址产物使回退等于切换 `artifact_id`。

### 5. 边界

- 信任边界：`DXF/DWG` 属于不可信外部输入，解析必须在独立进程或容器中运行，
  限制实体数、文件大小、解析时间。
- 服务边界：读图、编号、排板、后处理、写文件拆成独立服务/任务，只通过契约通信。
- AI 边界：AI 可以生成配置、提出编号修正、解释图纸，但不能直接写生产文件。
- 配置与代码边界：用户和 AI 只改 JSON/YAML 配置，工具代码由开发流程维护。
- 租户边界：每个项目独立存储、队列、凭据、配额，禁止跨租户读取。
- 资源边界：排板任务按 CPU、内存、时长配额执行，超限则降级、取消或保存当前最优解。

## 演进顺序

1. 完成 StoneBot Core 的确定性审图与排板闭环，包括有界重试、候选比较和回读验证。
2. 稳定版本化 MCP 契约、结构化错误码、证据和 `full / compact / minimal` 工具档。
3. 完成跨平台黄金案例，Codex 与 DeepSeek Harness 使用同一业务核心。
4. 接入 Claude Code、Kimi Code 等平台，适配器只保留平台连接能力。
5. 提供可迁移的本地 Runtime 安装包和远程 MCP 部署方式。
6. 建立画像反馈、候选补丁、历史回归和版本化画像包分发。
7. 接入多租户、队列、限流、审计、配额、权限和回滚。

## 与现有文档的关系

- 排板交互流程：`docs/ai-workflow-improvement-plan.md`
- 约束外置：`docs/constraint-externalization.md`
- 图纸画像：`docs/drawing-profile-dongguan-outlets.md`
- 读图子系统：`docs/drawing-intake-subsystem.md`
