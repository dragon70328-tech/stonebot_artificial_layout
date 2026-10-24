# Stonebot MCP plugin

The plugin is a local, model-independent MCP server. It uses STDIO and has no
web server, account system, or model API dependency.

This follows the official Model Context Protocol integration guidance:
<https://learn.chatgpt.com/docs/extend/mcp>.

## Current tools

- `stonebot_health`
- `stonebot_list_profiles`
- `stonebot_inspect_dxf`
- `stonebot_export_numbered`
- `stonebot_plan_nesting`

`stonebot_plan_nesting` is read-only. It computes a nesting plan but does not
create DXF, JSON, or other deliverables. `stonebot_export_numbered` writes only
the requested DXF.

## Codex

This repository includes `.codex/config.toml` for trusted project-local use:

```toml
[mcp_servers.stonebot]
command = "powershell.exe"
args = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ".\\plugins\\stonebot\\run_mcp.ps1"]
cwd = "."
```

Restart Codex after adding or changing MCP configuration. Use `/mcp` in the
Codex TUI to verify that `stonebot` is connected.

## Other MCP-compatible harnesses

The STDIO command is:

```text
powershell.exe -NoProfile -ExecutionPolicy Bypass -File <repo>\plugins\stonebot\run_mcp.ps1
```

The working directory should be the repository root. The launcher prefers the
repository `.venv` and falls back to `python` on `PATH`.

To register it explicitly in the current user's Codex configuration:

```powershell
.\scripts\install_stonebot_mcp.ps1
```

The script uses absolute paths for the current checkout. Project-local
`.codex/config.toml` is an alternative, but Codex only loads it for trusted
projects.

## Boundaries

- The MCP server never calls an LLM.
- The harness owns the conversation and token billing.
- DXF geometry and nesting remain deterministic Python operations.
- User-facing outputs are DXF, CSV when needed, and DOCX. JSON is used on the
  MCP wire protocol but is not written as a deliverable.
