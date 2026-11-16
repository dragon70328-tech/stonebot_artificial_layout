# Local Runtime Foundation

日期：2026-11-16

## 目标

把现有 StoneBot 核心包装成可安装、可持久化、可迁移的本地 Runtime。

本轮完成的是基础层，不包含账号、订阅、云同步或自动更新：

- 每用户本地应用目录；
- 单实例进程锁；
- 本地 MCP 启动脚本；
- 带清单和 SHA-256 校验的任务胶囊；
- 同一任务在另一个数据根目录导入后恢复原阶段。

## 本地目录

默认根目录：

```text
Windows:
%LOCALAPPDATA%\StoneBot

其他平台:
~/.local/share/stonebot
```

可通过 `STONEBOT_LOCAL_ROOT` 或 `--root` 覆盖。

```text
StoneBot/
  runtime/
    runtime.lock
  data/
    jobs/
    sessions/
    artifacts/
    profiles/
    plugins/
    logs/
  config/
    settings.json
```

启动入口：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\run_local_runtime.py
```

该入口获取每用户单实例锁，并使用本地 `data` 目录作为 PluginJobService 数据根。

## 任务胶囊

`src/task_capsule.py` 定义可传输的 `TaskCapsule` 容器，内部包含：

- `capsule.json`；
- `state/job_manifest.json`；
- `state/context_snapshot.json`；
- `state/state_<name>/...json`；
- `files/<role>/<filename>`。

清单记录每个文件的角色、相对路径、大小和 SHA-256，并带整体清单摘要。

任务胶囊：

- 不包含可执行代码；
- 不使用 pickle；
- 拒绝绝对路径、目录穿越、符号链接、重复路径和未登记文件；
- 解压到临时目录并通过原子重命名落到目标目录；
- 使用前校验每个文件的大小和 SHA-256。

## MCP 工具

新增：

```text
stonebot_export_task_capsule
stonebot_import_task_capsule
```

导出内容包括：

- 输入 DXF；
- 工作流会话；
- JobContext 快照；
- 确认图纸；
- 当前及历史排板版本；
- 已存在的原位/排板 DXF、检查文件、报表和纹理包文件；
- 任务清单和本地配置。

导入时会把旧电脑的绝对路径改写为新数据根中的文件路径，并恢复原工作流阶段。

## 当前边界

- 当前是显式文件导出/导入，不是在线自动同步；
- 尚未加入用户身份、端到端加密和任务租约；
- 同一任务不能同时在两台电脑写入；
- 下一步由 Sync Service 负责上传、下载、设备租约和冲突分支。
