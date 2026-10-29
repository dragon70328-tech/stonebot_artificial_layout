# DeepSeek Harness Adapter

This adapter uses the same Stonebot MCP server and business service as Codex.
It does not contain a second reading or nesting engine.

## Preferred integration

1. Configure the current DeepSeek Harness to launch:

   ```text
   python -X utf8 -m src.mcp_server --tool-profile compact
   ```

   with the repository root as the working directory.

2. Load `system_prompt.md` as the persistent Stonebot instruction.

3. Set:

   ```text
   STONEBOT_TOOL_PROFILE=compact
   STONEBOT_MCP_DATA_ROOT=<isolated plugin data directory>
   ```

4. Verify that the Harness exposes all tools listed in
   `agent_contract.json` and can consume newline-delimited JSON-RPC over stdio.

## Fallback integration

If the installed Harness does not support MCP:

1. Export OpenAI-compatible tools:

   ```powershell
   python -X utf8 -m src.agent_contract `
     --profile compact `
     --format openai-tools `
     --output output\deepseek_tools.json
   ```

2. Add an adapter process which maps DeepSeek function calls to the same
   `PluginJobService` methods used by the MCP server.

3. Do not duplicate prompts or workflow rules. Read the generated contract and
   attach `system_prompt.md` to the Harness conversation.

The current DeepSeek Harness plugin format has been verified against the
installed version. `adapter.json` remains the Harness-neutral source for that
packaging layer.

## Local verification

Verified on 2026-10-29 against DeepSeek Harness Desktop 0.2.1-rc.3:

- the local plugin bundle loaded into the desktop profile;
- the plugin detail page showed `@deepseek-ai/dsh-mcp-client` running as
  `mcp-stonebot`;
- a new DeepSeek conversation listed the declared `compact` profile tools;
- the model called `stonebot_list_jobs`, which returned `ok: true`,
  `total: 0`, and an empty job list without reading any files.
- the model called `stonebot_analyze_drawing` on a real Chinese-path DXF and
  returned 6 panels, 4 holes, 0 audit issues and the `audited` human gate.
- plugin `0.2.2` blocks automatic numbering when text labels exist but no
  panel receives an original number, preventing arbitrary closed frames from
  being reported as audited panels.

The sandboxed development environment could not use DSH's normal pnpm installer
because the existing pnpm store could not be hard-linked. The local test was
therefore installed by copying the dependency-free bundle into the profile and
registering it in `dependencies` and `dsh.profile.bundles`. Normal distribution
should still use the DSH plugin manager rather than manual profile edits.
