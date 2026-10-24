# Plugin-first architecture

## Decision

The primary product surface is a local MCP plugin consumed by a user-selected
agent harness, such as Codex or another MCP-capable client. The project does not
need to maintain a general-purpose SaaS portal or a second chat interface.

The model provider owns conversation, context management, and token billing.
Stonebot owns deterministic DXF processing, manufacturing constraints, nesting,
validation, and artifact generation.

## Boundary

```text
Codex / DeepSeek / another MCP client
                  |
                  v
          local Stonebot MCP
                  |
                  v
 DXF reader -> profile -> parts -> nesting -> validation
                  |
                  v
          DXF / CSV / DOCX deliverables
```

The MCP server must not call an LLM. The agent must not directly manipulate DXF
or manufacturing files. Every write is an explicitly named deterministic tool
call backed by Python code and validated artifacts.

## Initial implementation

The first implementation is available at `plugins/stonebot/mcp_server.py`. It
uses a dependency-free STDIO JSON-RPC implementation so the plugin can run in an
offline local environment.

Current read-only tools:

- health check;
- drawing-profile inventory;
- DXF inspection;
- nesting plan calculation.

Current write tool:

- numbered in-place DXF export.

The next increments should add confirmation-aware material extraction, formal
nesting generation, and DOCX report generation. JSON remains an internal
protocol/working format and must not be emitted as a user deliverable.

## Legacy boundaries

`portal/`, `src/http_api.py`, tenant stores, and worker infrastructure remain in
the repository for compatibility and possible later batch integration. They are
no longer the required product UI and should not receive new feature work unless
a concrete deployment need appears.
