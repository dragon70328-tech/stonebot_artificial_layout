# StoneBot-Artificial-Stone-Layout for DeepSeek Harness

This is an installable DeepSeek Harness bundle. It does not duplicate Stonebot
business logic; its Cordis patch mounts `@deepseek-ai/dsh-mcp-client` against
the shared Stonebot MCP server.

## Before installation

Set these environment variables for the DeepSeek Harness process:

```text
STONEBOT_ROOT=C:\Users\drago\Documents\Codex\2026-10-28\new-chat\work\stonebot-plugin-dev
STONEBOT_PYTHON=C:\Users\drago\stonebot_artificial_layout_architecture\.venv\Scripts\python.exe
STONEBOT_LAUNCHER=C:\Users\drago\Documents\Codex\2026-10-28\new-chat\work\stonebot-plugin-dev\integrations\codex_plugin\scripts\launch_mcp.py
STONEBOT_TOOL_PROFILE=compact
STONEBOT_MCP_DATA_ROOT=<dedicated plugin data directory>
```

`STONEBOT_LAUNCHER` must point to the repository copy of
`integrations/codex_plugin/scripts/launch_mcp.py`. `STONEBOT_ROOT` is retained
for diagnostics and future profile-specific configuration.

To avoid persistent environment variables, materialize a concrete local copy:

```powershell
python -X utf8 ../make_local_plugin.py `
  --python C:\path\to\python.exe `
  --output C:\path\to\dsh-stonebot-artificial-stone-layout
```

Install the generated directory rather than this environment-driven template.

## Install

In DeepSeek Harness:

1. Open **插件**.
2. Choose **添加插件**.
3. Enter this directory as the local plugin path.
4. Fully quit and restart DSH so the Cordis patch is applied.

Installation has not been performed automatically because it changes the DSH
profile and runs plugin code inside the Harness process.
