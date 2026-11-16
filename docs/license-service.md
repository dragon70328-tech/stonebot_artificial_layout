# License Service

日期：2026-11-16

## 目标

提供独立于业务数据的账号和许可控制面：

- 邮箱或手机号验证码登录/注册；
- 每个账号三次免费试用；
- 试用使用预留、提交和失败释放；
- 管理员录入外部付款订单；
- 付款确认后发送一次性激活码；
- 签发月付/年付 Ed25519 Entitlement；
- 不接收 DXF、零件几何、排板布局或客户项目内容。

## 数据

默认 SQLite：

```text
data/license_service.sqlite3
```

生产环境建议使用独立服务账号、HTTPS 反向代理、数据库备份和限流。

主要表：

```text
accounts
verification_codes
devices
trial_usages
orders
activation_codes
entitlements
admin_audit
```

验证码和激活码只保存带服务端 pepper 的 HMAC，不保存明文。

## API

```text
POST /v1/auth/request-code
POST /v1/auth/verify-code

POST /v1/usage/reserve
POST /v1/usage/commit
POST /v1/usage/release

POST /v1/admin/orders
POST /v1/admin/orders/{order_no}/confirm
POST /v1/license/activate
```

管理员接口使用：

```text
Authorization: Bearer <STONEBOT_LICENSE_ADMIN_TOKEN>
```

## 免费试用

试用以服务端账号为权威：

1. 最终交付前调用 `reserve`；
2. 同一账号 + 同一 `job_id` 只计一次；
3. 文件交付成功后调用 `commit`；
4. 交付失败调用 `release`；
5. 三个 committed/reserved 任务用完后返回 `TRIAL_EXHAUSTED`。

预留 15 分钟后自动过期，不会永久占用次数。

登录成功后服务端签发设备绑定会话：

```text
data/license/session.json
```

除登录接口外，试用预留/提交/释放和订单激活都使用该 Bearer Session。

## 外部付款

首版不接自动支付：

```text
管理员录单
-> 人工确认付款
-> 邮件/短信发送一次性激活码
-> Runtime 提交订单号和激活码
-> 服务端签发月付/年付 Entitlement
```

同一外部付款流水号不能重复建单。

## 发送器

`src/license_delivery.py` 提供：

- SMTP 邮件发送；
- HTTPS 短信 Webhook；
- Development captured sender。

开发 sender 只用于测试，不应在生产配置启用。

## 启动

必需环境变量：

```text
STONEBOT_LICENSE_PRIVATE_KEY
STONEBOT_LICENSE_KEY_ID
STONEBOT_LICENSE_ADMIN_TOKEN
STONEBOT_LICENSE_CODE_PEPPER
```

邮件/SMS 可选：

```text
STONEBOT_SMTP_HOST
STONEBOT_SMTP_PORT
STONEBOT_SMTP_USERNAME
STONEBOT_SMTP_PASSWORD
STONEBOT_SMTP_FROM
STONEBOT_SMTP_SSL
STONEBOT_SMTP_STARTTLS

STONEBOT_SMS_WEBHOOK_URL
STONEBOT_SMS_WEBHOOK_TOKEN
```

启动：

```powershell
python -X utf8 scripts\run_license_service.py
```

默认只监听 `127.0.0.1:8120`。

## 本地客户端

`src/license_client.py` 提供：

- 请求/验证登录验证码；
- 保存设备会话；
- 试用 reserve/commit/release；
- 订单激活码兑换。

命令行联调：

```powershell
python -X utf8 scripts\license_login.py `
  --service-url https://license.example.com `
  --identity-type email `
  --destination user@example.com

python -X utf8 scripts\license_activate.py `
  --service-url https://license.example.com `
  --order-no ord-xxxx

python -X utf8 scripts\license_status.py `
  --public-key-file .\license-public-key.txt
```

启用在线许可时，最终交付会：

```text
reserve
-> 写正式文件
-> commit
```

文件交付失败时调用 `release`；commit 失败会保留待重试记录。

## 当前缺口

- 尚未实现本地登录/激活 UI；
- 设备解绑、退款、吊销和管理员页面尚未实现；
- 尚未接自动支付渠道；
- `session.json` 仍使用用户目录权限保护，尚未接入 Windows DPAPI；
- 生产部署必须配置 HTTPS、限流、备份和监控。
