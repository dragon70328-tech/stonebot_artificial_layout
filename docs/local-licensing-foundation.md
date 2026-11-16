# Local Licensing Foundation

日期：2026-11-16

## 目标

在本地 Runtime 中建立可验证的订阅准入层，同时保持业务核心与 License
Service 解耦。

本轮完成：

- 设备安装身份；
- Ed25519 签名 Entitlement；
- 月付/年付/试用计划；
- 有效期、未生效、宽限期和过期状态；
- 设备绑定和签名校验；
- 原子本地许可文件；
- 生产交付门禁；
- `stonebot_license_status` MCP 工具。

尚未完成：

- 邮箱/手机验证码服务端；
- 三次试用服务端计数；
- 外部付款订单和一次性激活码；
- 设备解绑后台；
- DPAPI/Credential Manager 加密存储；
- 在线刷新、吊销和离线租约。

## 本地文件

```text
data/license/
  device.json
  entitlement.json
```

`device.json` 保存随机 installation ID。损坏时 fail closed，不自动重新生成，
避免通过反复删除本地文件绕过服务端设备/trial 约束。

`entitlement.json` 是服务器签名的许可快照。Runtime 只持有 Ed25519 公钥。

## 状态

```text
active
trial_exhausted
grace
expired
not_yet_valid
invalid
missing
disabled
```

- `active`：允许生产交付。
- `trial_exhausted`、`expired`、`invalid`、`missing`：不允许生产交付。
- `grace`：只读宽限期，仍禁止新的正式交付。
- `disabled`：仅开发环境未配置许可公钥时使用，正式安装包不得启用。

## 交付门禁

`PluginJobService.finalize_job` 在进入最终确认和文件交付之前调用
`LicenseGuard.require_production_access()`。

已完成的旧交付仍可查看；过期许可不会删除历史 DXF。

## 启动配置

本地 Runtime 可选加载许可公钥：

```powershell
python -X utf8 scripts\run_local_runtime.py `
  --license-public-key-file .\config\license-public-key.txt `
  --device-name "Office-PC"
```

公钥文件内容为 base64 raw Ed25519 public key。正式安装包应默认携带可信公钥，
而不是让用户自由替换。
