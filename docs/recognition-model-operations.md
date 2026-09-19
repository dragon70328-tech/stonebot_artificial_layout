# 识别模型训练与运维

## 目录

```text
recognition_models/
  current.json              # 当前启用的全局模型
  versions/                 # 所有训练版本
  global_feedback/          # 用户选择全局学习后产生的反馈
  monitoring/latest.json    # 最近一次漂移监控报告
  training_state.json       # 定时训练状态
  scheduled_training_latest.json
```

## 手动训练

```bash
python scripts/train_recognition_model.py \
  --feedback-dir recognition_models/global_feedback \
  --output-dir recognition_models \
  --min-samples 20 \
  --activate
```

只有同时通过绝对质量门和相对当前模型回归门时才会激活。需要人工覆盖时使用
`--force-activate`。

## 漂移监控

```bash
python scripts/monitor_recognition_model.py \
  --feedback-dir recognition_models/global_feedback \
  --current recognition_models/current.json \
  --output recognition_models/monitoring/latest.json
```

检测到特征漂移时脚本返回退出码 `2`，可接入告警系统。

## 定时重训

```bash
python scripts/schedule_recognition_training.py \
  --feedback-dir recognition_models/global_feedback \
  --output-dir recognition_models \
  --min-new-feedback 20 \
  --max-interval-days 30 \
  --activate
```

触发条件：

- 当前没有可用模型且反馈达到最低数量；
- 自上次训练后新增反馈达到阈值；
- 距离上次训练超过最大间隔且反馈总量足够；
- 监控报告检测到漂移，并且新增反馈达到漂移训练阈值；
- 使用 `--force` 人工强制。

脚本使用 `training.lock` 防止并发重训。`--dry-run` 只输出决策，不执行训练。

## 回滚

```bash
python scripts/activate_recognition_model.py \
  recognition_models/versions/<model-version>.json \
  --current recognition_models/current.json \
  --backup-dir recognition_models/backups
```

默认再次执行质量门；紧急回滚可使用 `--force`。
## 租户隔离

全局反馈按租户保存：

```text
recognition_models/global_feedback/<tenant_id>/
```

未指定 `--tenant-id` 时训练会汇总所有已同意全局学习的租户。需要训练租户专属模型时：

```bash
python scripts/train_recognition_model.py \
  --tenant-id tenant-a \
  --feedback-dir recognition_models/global_feedback \
  --output-dir recognition_models/tenants/tenant-a \
  --activate
```

全局队列中的几何、图层、编号和材料信息会先脱敏，只保留训练特征、标签和匿名图纸摘要。

## 反馈保留与归档

默认只预演，不移动文件：

```bash
python scripts/prune_recognition_feedback.py \
  --feedback-dir recognition_models/global_feedback \
  --max-age-days 180 \
  --max-files-per-tenant 500
```

确认结果后执行归档：

```bash
python scripts/prune_recognition_feedback.py \
  --feedback-dir recognition_models/global_feedback \
  --max-age-days 180 \
  --max-files-per-tenant 500 \
  --apply
```

归档目录按月份和租户组织：

```text
global_feedback/archive/YYYYMM/<tenant_id>/
```
## 一体化定时运维周期

推荐定时任务直接调用一体化脚本，它会依次执行：

```text
监控当前模型
  -> 判断是否重训
  -> 训练和质量审批
  -> 可选激活
  -> 反馈保留归档
```

```bash
python scripts/run_recognition_model_ops.py \
  --feedback-dir recognition_models/global_feedback \
  --output-dir recognition_models \
  --min-new-feedback 20 \
  --max-interval-days 30 \
  --retention-max-age-days 180 \
  --retention-max-files-per-tenant 500
```

首次运行或确认流程时建议使用：

```bash
python scripts/run_recognition_model_ops.py --dry-run
```

只有显式添加 `--apply-retention` 才会移动反馈文件。周期报告保存在
`recognition_models/operations/latest.json`。
## 管理员界面

启动 FastAPI 后访问：

```text
/admin/
```

需要 `admin` 角色。界面支持：

- 查看当前模型、版本、指标和反馈数量；
- 查看最近训练、监控和运维报告；
- 生成 dry-run 运维计划；
- 后台执行监控、重训和可选激活；
- 按版本回滚模型。

管理 API：

```text
GET  /admin/recognition-model/status
POST /admin/recognition-model/operations
POST /admin/recognition-model/rollback
```

## 定时任务模板

Linux Cron 示例：

```text
deploy/recognition-model-ops.cron.example
```

Windows 任务计划程序可使用：

```powershell
deploy\recognition-model-ops-task.ps1 `
  -ProjectRoot C:\path\to\stonebot_artificial_layout_architecture
```

添加 `-ApplyRetention` 才会执行反馈归档。

## 管理审计

管理页支持保存管理员租户 ID 和 API Key，并在请求时自动添加认证头。

新增审计接口：

```text
GET /admin/recognition-model/audit
GET /admin/recognition-model/audit/export
```

审计日志写入：

```text
recognition_models/operations/audit.jsonl
```

每次运维预演、正式运维和模型回滚都会记录操作租户、动作、状态和参数。
