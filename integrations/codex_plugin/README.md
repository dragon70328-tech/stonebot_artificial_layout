# StoneBot-Artificial-Stone-Layout for Codex

This plugin exposes the existing Stonebot workflow through one local MCP
server. The stable application remains available through the original CLI,
HTTP API and portal.

## Development layout

- Plugin manifest: `.codex-plugin/plugin.json`
- MCP configuration: `.mcp.json`
- Launcher: `scripts/launch_mcp.py` (PowerShell helper also available for
  interactive diagnostics)
- Agent workflow instructions:
  `skills/stonebot-artificial-stone-layout/SKILL.md`
- Deterministic implementation: repository `src/` directory

The Python launcher uses `STONEBOT_PYTHON` when set, otherwise the repository
`.venv`, otherwise `python` from `PATH`. Plugin data defaults to
`output/mcp_jobs`; set `STONEBOT_MCP_DATA_ROOT` to isolate development or
production work.

## Local verification

From the repository root:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m src.mcp_server --version
.\.venv\Scripts\python.exe -X utf8 -m pytest -q tests\test_plugin_service.py tests\test_mcp_server.py
```

The MCP server uses newline-delimited JSON-RPC over standard input/output.
Diagnostics are written only to standard error.
