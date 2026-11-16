# Secure Local Updates

日期：2026-11-16

## 目标

为本地 StoneBot Runtime 建立可验证、可回滚的发布链：

- Ed25519 签名的发布清单；
- 发布物 SHA-256 和文件清单校验；
- 下载到临时目录；
- 安全解压到版本目录；
- 自检后原子切换；
- 保留上一版本并支持回滚；
- 正式安装器继续使用 Windows Authenticode 代码签名。

## 发布目录

```text
%LOCALAPPDATA%\StoneBot\
  versions/
    1.4.1/
    1.4.2/
  updates/
    downloads/
    staging/
  current.json
```

`current.json` 只指向已经验证并激活的版本，不参与业务计算。

## 发布清单

`src/release_manifest.py` 定义：

- 产品、渠道、版本和发布时间；
- 最低兼容版本和最低数据版本；
- 发布包文件名、大小和 SHA-256；
- 发布包解压后的完整文件清单；
- 签名算法、密钥 ID 和签名。

签名覆盖除 `signature` 外的规范化 JSON。修改版本号、文件哈希、最低数据版本或任何文件条目都会使签名失效。

私钥只保存在发布环境中，不进入安装包、Runtime 或 GitHub。Runtime 只保存可信 Ed25519 公钥。

## 更新器

`src/update_manager.py` 提供：

- 版本比较和候选更新判断；
- HTTPS 下载并校验发布包大小和 SHA-256；
- ZIP 路径穿越、绝对路径、符号链接、重复文件和文件数限制；
- 解压后逐文件哈希校验；
- 可选的无副作用自检回调；
- 解压到临时 staging 后原子重命名到 `versions/<version>`；
- 原子写入 `current.json`；
- 一键回滚到上一版本。

更新器不执行发布清单中的任意命令。自检只能由 Runtime 内部可信代码提供。

## 命令

生成签名清单：

```powershell
python -X utf8 scripts\build_release_manifest.py `
  --release-dir .\dist\StoneBot-1.4.2 `
  --artifact .\dist\StoneBot-1.4.2.zip `
  --version 1.4.2 `
  --key-id stable-2026 `
  --private-key-file .\release-private-key.txt `
  --output .\dist\release.json
```

检查更新：

```powershell
python -X utf8 scripts\check_update.py `
  --manifest-url https://updates.example.com/stable/release.json `
  --public-key-file .\release-public-key.txt `
  --current-version 1.4.1
```

下载、校验、staging 并切换：

```powershell
python -X utf8 scripts\install_update.py `
  --manifest-url https://updates.example.com/stable/release.json `
  --public-key-file .\release-public-key.txt
```

## 尚未完成

- Windows Authenticode 证书申请和签名流水线；
- 模拟更新服务或 CDN；
- 桌面端“发现更新/一键更新”界面；
- 独立 updater helper 替换正在运行的主程序；
- 数据迁移脚本和兼容版本策略；
- 稳定版、Beta 和离线更新包发布流程；
- 公钥轮换和吊销清单。
