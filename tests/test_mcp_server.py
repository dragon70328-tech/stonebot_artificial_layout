from __future__ import annotations

import io
import json
from pathlib import Path

from src.agent_contract import TOOL_PROFILES, agent_contract
from src.mcp_server import MCPServer, serve_stdio
from src.plugin_protocol import TOOL_DEFINITIONS


class FakeService:
    def analyze_drawing(self, args):
        return {
            "ok": True,
            "job_id": "job-1",
            "stage": "audited",
            "dxf_path": args["dxf_path"],
        }

    def list_jobs(self, args):
        return {"ok": True, "total": 1, "jobs": [{"job_id": "job-1"}]}

    def job_status(self, args):
        return {
            "ok": True,
            "job_id": args["job_id"],
            "stage": "audited",
            "next_actions": ["stonebot_confirm_drawing"],
        }

    def get_agent_contract(self, args):
        return {"ok": True, **agent_contract(args.get("profile", "full"))}

    def __getattr__(self, name):
        def unexpected(_args):
            raise AssertionError(f"unexpected tool: {name}")

        return unexpected


def test_mcp_initialize_and_tool_discovery():
    server = MCPServer(FakeService())

    initialized = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }
    )
    listed = server.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )

    assert initialized["result"]["protocolVersion"] == "2025-06-18"
    assert initialized["result"]["capabilities"]["tools"]
    assert {
        tool["name"] for tool in listed["result"]["tools"]
    } == {tool["name"] for tool in TOOL_DEFINITIONS}


def test_mcp_tool_call_returns_text_and_structured_content():
    server = MCPServer(FakeService())

    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "stonebot_analyze_drawing",
                "arguments": {"dxf_path": "demo.dxf"},
            },
        }
    )

    result = response["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["stage"] == "audited"
    assert json.loads(result["content"][0]["text"]) == (
        result["structuredContent"]
    )


def test_mcp_unknown_tool_is_a_structured_tool_error():
    server = MCPServer(FakeService())

    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "missing_tool", "arguments": {}},
        }
    )

    result = response["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "UNKNOWN_TOOL"


def test_mcp_stdio_uses_newline_delimited_json_rpc():
    incoming = io.StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {},
            }
        )
        + "\n"
        + json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            }
        )
        + "\n"
    )
    outgoing = io.StringIO()

    serve_stdio(
        service=FakeService(),
        stdin=incoming,
        stdout=outgoing,
    )

    responses = [
        json.loads(line)
        for line in outgoing.getvalue().splitlines()
    ]
    assert [item["id"] for item in responses] == [1, 2]
    assert len(responses[1]["result"]["tools"]) == len(TOOL_DEFINITIONS)


def test_mcp_exposes_job_resources():
    server = MCPServer(FakeService())

    listed = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "resources/list",
            "params": {},
        }
    )
    read = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "resources/read",
            "params": {"uri": "stonebot://jobs/job-1"},
        }
    )

    uris = {
        item["uri"] for item in listed["result"]["resources"]
    }
    assert "stonebot://jobs/job-1" in uris
    payload = json.loads(read["result"]["contents"][0]["text"])
    assert payload["job_id"] == "job-1"


def test_compact_profile_uses_high_level_gate_tool():
    server = MCPServer(FakeService(), tool_profile="compact")

    listed = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/list",
            "params": {},
        }
    )
    rejected = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "stonebot_confirm_drawing",
                "arguments": {
                    "job_id": "job-1",
                    "decision": "confirm",
                },
            },
        }
    )

    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert names == set(TOOL_PROFILES["compact"])
    assert "stonebot_advance_until_gate" in names
    assert "stonebot_choose_output_directory" in names
    assert "stonebot_export_word_report" in names
    assert "stonebot_confirm_drawing" not in names
    assert rejected["result"]["isError"] is True
    assert (
        rejected["result"]["structuredContent"]["error"]["code"]
        == "TOOL_NOT_IN_PROFILE"
    )


def test_agent_contract_resource_is_harness_neutral():
    server = MCPServer(FakeService(), tool_profile="compact")

    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "resources/read",
            "params": {"uri": "stonebot://agent-contract"},
        }
    )

    payload = json.loads(response["result"]["contents"][0]["text"])
    assert payload["tool_profile"] == "compact"
    assert payload["policies"]["server_is_source_of_truth"] is True
    assert payload["policies"]["agent_may_edit_dxf"] is False
    assert payload["openai_compatible_tools"]
    assert len(payload["workflow"]["human_gates"]) == 3


def test_codex_plugin_manifest_points_to_mcp_and_skill():
    plugin_root = (
        Path(__file__).parents[1]
        / "integrations"
        / "codex_plugin"
    )
    manifest = json.loads(
        (plugin_root / ".codex-plugin" / "plugin.json").read_text(
            encoding="utf-8"
        )
    )
    mcp_config = json.loads(
        (plugin_root / ".mcp.json").read_text(encoding="utf-8")
    )

    assert manifest["name"] == "stonebot-artificial-stone-layout"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert manifest["skills"] == "./skills/"
    assert mcp_config["mcpServers"]["stonebot"]["command"] == "python"
    assert (
        plugin_root
        / "skills"
        / "stonebot-artificial-stone-layout"
        / "SKILL.md"
    ).exists()
