# Stonebot SaaS 搭建与运行

> 最后更新: 2026-09-23

本文件描述 SaaS 形态的**当前**搭建与运行方式。文末保留 2026-08-03 的早期
规划记录，但其中“SaaS 代码尚未开始编写”“宝塔面板找不到添加站点入口”等
结论已被实现取代，不要再据此排期。

架构目标与五个支柱见 `docs/saas-architecture-goal.md`；读图子系统见
`docs/drawing-intake-subsystem.md`；模型运维见
`docs/recognition-model-operations.md`。

---

## 1. 当前形态

| 层 | 实现 |
|---|---|
| 后端 API | FastAPI，`src/http_api.py`，uvicorn 单进程 |
| 任务调度 | `src/task_queue.py`（文件 / SQLite）+ `src/worker.py`，可拆独立 worker 进程 |
| 工作流 | `src/workflow_session.py` 状态机 + `src/agent_runtime.py` + `src/orchestrator.py` |
| 数据 | 文件存储（`STONEBOT_DATA_ROOT`），可切 SQLite 统一状态库 |
| 对象存储 | 本地目录，或 S3 / MinIO / LocalStack（`src/object_store.py`） |
| 多租户 | `src/tenant_store.py` + API Key（`X-API-Key` / `X-Tenant-Id`） |
| 前端 | 静态门户 `portal/`，由 API 直接挂载：`/` 识图排板、`/admin` 模型运维 |
| 部署 | `Dockerfile` + `docker-compose.yml`（api + worker 两个服务） |
| CI | GitHub Actions 跑 `python -X utf8 -m pytest -q -m "not slow"` |

---

## 2. 本地启动

### 2.1 依赖（推荐用项目自带 `.venv`）

仓库根目录的 `.venv` 是本地虚拟环境（已被 `.gitignore` 忽略）。首次搭建：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

三份依赖文件的分工：

| 文件 | 作用 |
|---|---|
| `requirements.txt` | **允许的版本区间**（人工维护的唯一来源） |
| `requirements.lock.txt` | **实测版本快照**（2026-10-13 在 `.venv` 冻结；该组合下 `pytest -q -m "not slow"` = 443 passed）。CI 与部署建议用它锁定 |
| `requirements-dev.txt` | `-r requirements.lock.txt` + `pytest` + `httpx`（测试/开发用） |

历史说明：2026-10-13 之前本项目**一直直接用系统 Python**
（`C:\Program Files\Python310`）跑，没有虚拟环境；`pytest` 与 `httpx`
也从未在依赖文件里声明过（靠机器上恰好装着，所以本地能跑、新建 venv 才暴露）。
现在 `.venv` + lock 把环境固定下来了。

日常运行：

```powershell
.\.venv\Scripts\python.exe -X utf8 main.py input.dxf 3200 1800 20 --profile min_sheets
.\.venv\Scripts\python.exe -X utf8 -m pytest -q -m "not slow"
```

`scripts/run_dev.ps1` 已改为**优先使用 `.venv`**，没有 `.venv` 时回退到 PATH 上的
`python`。

Python 3.10+（CI 使用 3.11）。

### 2.2 直接启动 API

```bash
python -X utf8 -m uvicorn src.http_api:app --host 127.0.0.1 --port 8002
```

- `http://127.0.0.1:8002/` 识图排板页
- `http://127.0.0.1:8002/admin/` 识别模型运维页
- `http://127.0.0.1:8002/docs` OpenAPI 文档
- `http://127.0.0.1:8002/health` 健康检查（返回数据根目录）

### 2.3 带 LLM 对话（可选）

```powershell
# 1. 复制 scripts/dev_env.example.ps1 为 scripts/dev_env.ps1 并填入密钥
# 2. 启动（默认 DeepSeek 兼容端点）
scripts\run_dev.ps1
```

`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` 见 `src/openai_llm_client.py`。
不配置 LLM 时，`--agent-provider queue`（默认）走确定性计划，功能不受影响。

### 2.4 Docker Compose

```bash
docker compose up --build
```

- `api` 服务：`STONEBOT_WORKER_ENABLED=0`，只处理 HTTP；
- `worker` 服务：`python scripts/api_worker.py`，执行排板等长任务；
- 两者共享 `/data` 卷，并必须使用同一个 SQLite 任务库。

独立部署 worker 时必须满足：

```text
STONEBOT_TASK_STORE=sqlite     # 文件队列无法跨进程
STONEBOT_DATA_ROOT=<同一目录>
STONEBOT_WORKER_ID=<每个 worker 唯一>
```

---

## 3. 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `STONEBOT_DATA_ROOT` | `output/api_jobs` | 任务、租户、会话、对象存储根目录 |
| `STONEBOT_TASK_STORE` | `file` | 任务队列后端，跨进程必须为 `sqlite` |
| `STONEBOT_JOB_STORE` | `file` | 任务清单持久化，可选 `sqlite` |
| `STONEBOT_SESSION_STORE` | `file` | 工作流会话持久化，可选 `sqlite` |
| `STONEBOT_TENANT_STORE` | `file` | 租户/项目目录持久化，可选 `sqlite` |
| `STONEBOT_STATE_DB` | 未设置 | 设为 `unified` 时上述存储共用一个 `state.sqlite3` |
| `STONEBOT_WORKER_ENABLED` | `1` | 当前进程是否内嵌 worker |
| `STONEBOT_WORKER_ID` | 自动 | worker 标识 |
| `STONEBOT_OBJECT_STORE` | `local` | `local` / `s3` / `minio` / `localstack` |
| `STONEBOT_S3_BUCKET` | 未设置 | S3 桶名（选择 S3 模式时必填） |
| `STONEBOT_S3_ENDPOINT_URL` / `STONEBOT_S3_REGION` / `STONEBOT_S3_PREFIX` | 未设置 | S3 连接参数 |
| `STONEBOT_S3_ACCESS_KEY_ID` / `STONEBOT_S3_SECRET_ACCESS_KEY` | 未设置 | S3 凭据 |
| `STONEBOT_TOOL_TIMEOUT_S` | `1800` | 单个工具调用超时 |
| `STONEBOT_TENANT_API_KEYS` | 未设置 | 多租户凭据 JSON（见下节） |
| `STONEBOT_API_KEY` | 未设置 | 单租户简写，等价于 `default` 租户的 key |
| `STONEBOT_LOG_LEVEL` | `INFO` | 日志级别 |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` | 未设置 | LLM 对话式规划 |

---

## 4. 认证与租户边界

- **未配置任何 key**：单机开发模式，请求归属 `default` 租户且拥有 `admin` 角色，
  不做鉴权。**不要以此模式对公网开放。**
- **配置 key 后**：请求必须携带 `X-API-Key` 与 `X-Tenant-Id`，两者不匹配返回 401；
  跨租户访问任务返回 403。

```bash
export STONEBOT_TENANT_API_KEYS='{
  "tenant-a": {"key": "sk-tenant-a", "roles": ["operator"]},
  "ops": {"key": "sk-ops", "roles": ["admin"], "expires_at": 1790000000}
}'
```

角色说明：`admin` 可访问 `/admin/*` 模型运维接口，也隐含其他所有角色权限；
`operator` 可执行任务创建、确认与纠正。

---

## 5. 识别模型运维

策略与命令见 `docs/recognition-model-operations.md`，运行时目录：

```text
<STONEBOT_DATA_ROOT>/recognition_models/
  current.json              # 当前启用的全局模型
  versions/                 # 训练版本
  global_feedback/<tenant>/ # 用户同意全局学习的脱敏反馈
  monitoring/latest.json    # 漂移监控报告
  operations/audit.jsonl    # 管理审计日志
```

管理接口：`GET /admin/recognition-model/status`、
`POST /admin/recognition-model/operations`、
`POST /admin/recognition-model/rollback`。

---

## 6. 服务器与反向代理（需人工核对）

早期记录保留如下，但**域名、DNS、服务器状态未在本仓库得到验证**，部署前
必须与运维核对：

| 项目 | 早期记录 |
|---|---|
| 云服务商 | 腾讯云 |
| 操作系统 | OpenCloudOS 9.4 |
| 管理面板 | 宝塔面板（端口 23524） |
| Web 服务器 | Nginx 1.26.3 |
| 端口占用 | 80 / 888 Nginx，8000 旧版 stonebot，23524 宝塔 |
| 旧项目 | `/opt/stonebot/`，端口 8000，保持不动 |

新服务建议用独立子域反代到本服务端口，示例见 `deploy/nginx-portal.conf.example`
（该示例假设门户静态目录 `/var/www/stonebot-portal`，`/nesting/` 反代到 8002）。
若直接由 FastAPI 提供门户，则可只配置一条 `proxy_pass`，无需单独静态根目录。

---

## 7. 历史记录（2026-08-03，已被取代）

原文结论：

- “SaaS 代码尚未开始编写” —— 已不成立，见第 1 节与 `docs/saas-architecture-goal.md`。
- “宝塔面板未找到添加站点入口” —— 已不作为阻塞项，当前部署走 Docker/uvicorn。
- “域名待确认，DNS 待解析” —— 仍需人工确认，见第 6 节。

早期技术选型表（前端待定 / Celery + Redis / PostgreSQL）**未采用**：当前后端为
FastAPI + 文件或 SQLite 存储 + 内置任务队列，未引入 Redis 与 PostgreSQL。
