# 大文件拆分计划

> 最后更新: 2026-09-23

`docs/saas-architecture-goal.md` 明确要求“避免继续把功能堆进 `main.py` 单点”。
本文件记录超大模块的拆分计划、已完成步骤和安全规则，任何拆分都必须按第 3 节
的流程验证。

## 1. 现状（2026-09-23）

| 文件 | 拆分前 | 目前 | 状态 |
|---|---:|---:|---|
| `main.py` | 1445 行 / 57 KB | 1152 行 / 45 KB | 审图 CLI 已抽出 |
| `src/list_nesting.py` | 2872 行 / 96 KB | 2524 行 / 83 KB | 解析层已抽出 |
| `src/drawing_profile.py` | 2647 行 / 92 KB | 未动 | 待拆 |
| `src/http_api.py` | 1676 行 / 56 KB | 未动 | 待拆 |
| `src/postprocess.py` | 1202 行 / 45 KB | 未动 | 观察 |
| `src/nesting.py` | 863 行 / 34 KB | 未动 | 观察 |

已完成的拆分：

- `src/cli_audit.py`：审图 CLI（`run_audit`、问题状态、复检）。
  `main.py --audit` 行为不变；测试改为直接针对该模块打桩。
- `src/cli_paths.py`：`make_output_dir`，供排板 CLI 与审图 CLI 共用，
  避免 `main` 与 `cli_audit` 互相 import。
- `src/list_parsing.py`：`.xlsx` / `.pdf` 解析与 `ListItem`。
  `list_nesting` 继续再导出 `ListItem` / `parse_list_file` / `parse_pdf` /
  `parse_xlsx`，旧导入路径不受影响。

## 2. 后续拆分步骤

按“收益 / 风险”从高到低排列，每一步独立提交、独立验证。

### 步骤 1：`drawing_profile.py` 抽出审图（推荐下一步）

- 目标：把 `_add_*_issues`、`_issue_matches_rule`、`_apply_audit_ignore_rules`、
  `audit_drawing`、`write_audit_json`、`write_audit_dxf`（约 1767-2647 行）
  移到 `src/drawing_audit.py`。
- 依赖方向：`drawing_audit` 依赖 `drawing_profile` 的画像模型与几何提取
  helper，因此保留 `drawing_profile` 对外的 `audit_drawing` 再导出。
- 风险：审图 helper 与提取 helper 有交叉引用（`_entity_is_closed`、
  `_text_box_polygon`、`_polygons_from_line_entities`），必须显式 import，
  不要复制实现。

### 步骤 2：`http_api.py` 抽出请求模型与认证

- 目标：`JobCreateRequest` 等 Pydantic 请求模型 → `src/api_models.py`；
  `TenantAuth`、`get_authenticated_tenant`、`_require_role` 等 → `src/api_auth.py`。
- 好处：认证与路由解耦，便于单独测越权与过期凭据。
- 注意：`JobCreateRequest` 是多处 API 与测试的引用点，需要在新模块定义并
  在原模块再导出，避免外部破坏。

### 步骤 3：`http_api.py` 抽出 `JobManager`

- 目标：`JobManager`（约 353-913 行）→ `src/job_manager.py`。
- 依赖：`AgentRuntime`、`TenantStore`、`JobRepository`、`TaskQueue`、
  `ObjectStore`、`ConversationStore`。抽出后 `http_api` 只保留路由与 DI。
- 收益最大（单一类占文件三分之一），但改动面也最大，放在步骤 2 之后做。

### 步骤 4：`list_nesting.py` 再拆排板策略

- 候选切口：配对/模式生成（`_build_pair_patterns`、`_pack_group_pairing*`）、
  窄条填充（`_fill_*`、`_best_strip_subset*`）、多尺寸选板
  （`_nest_group_rect_multi`、`_result_uses_all_sizes`）。
- 这些函数互相调用密集，必须先补“给定清单 -> 板数/出材率”的回归基准，
  再按切口逐步迁移。

## 3. 拆分安全规则（每一步都必须满足）

1. **一次只搬一个层**：优先搬依赖少、被调用面清晰的模块（解析、路径、
   审图这类适配层），不要同时改算法。
2. **行为零变化**：拆分提交不得包含逻辑修改；发现 bug 先单独提交修复。
3. **保留再导出**：被外部或测试引用的名字，在原模块保留同名导入，避免
   破坏 `from src.x import y` 的既有用法。
4. **测试打桩点同步迁移**：`monkeypatch.setattr(module, name)` 作用的模块
   决定打桩是否生效，搬动函数后必须把测试打桩点改到新模块。
5. **BOM 与换行**：部分文件带 UTF-8 BOM 或 CRLF，读写时保持原编码与换行，
   不要顺带重排全文。
6. **验证组合**：
   - `python -X utf8 -m pytest -q -m "not slow"` 全绿；
   - `node tests/frontend_ui.test.cjs` 全绿（涉及 `portal/` 或 API 契约时）；
   - 至少一个真实样例端到端冒烟（如 `main.py --audit <真实 DXF>`、
     `main.py <清单.xlsx> --sizes ... --list-nest --no-dxf`）；
   - `git diff --check` 通过。

## 4. 验收口径

- 拆分后每个模块有单一职责，且能用一句话说明边界；
- 没有任何模块同时承担“外部输入解析 + 业务规则 + 持久化”；
- 新模块有对应测试或至少被现有测试覆盖；
- 状态机、契约与工具白名单不因拆分改变。
