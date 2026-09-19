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
