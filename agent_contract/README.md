# Stonebot Agent Contract

The contract is generated from `src/agent_contract.py` and the canonical tool
registry in `src/plugin_protocol.py`. Do not hand-maintain another copy of the
workflow in Codex and DeepSeek prompts.

Export a portable contract:

```powershell
python -X utf8 -m src.agent_contract --profile full
python -X utf8 -m src.agent_contract --profile compact
```

Export OpenAI-compatible function definitions for a lower-level Harness:

```powershell
python -X utf8 -m src.agent_contract `
  --profile compact `
  --format openai-tools `
  --output output\deepseek_tools.json
```

The MCP server also publishes the contract as:

```text
stonebot://agent-contract
stonebot://agent-contract/full
stonebot://agent-contract/compact
```
