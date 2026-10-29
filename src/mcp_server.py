"""Minimal MCP stdio server for the Stonebot Agent plugin.

This server intentionally uses the standard-library JSON-RPC implementation so
the plugin has no additional runtime dependency beyond Stonebot itself.  The
transport supports tool discovery and calls plus lightweight job resources.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, TextIO

from src.agent_contract import (
    TOOL_PROFILES,
    tool_definitions_for_profile,
)
from src.plugin_protocol import (
    MCP_PROTOCOL_VERSION,
    PLUGIN_VERSION,
    SCHEMA_VERSION,
    tool_result,
)
from src.plugin_service import PluginJobService, StonebotProtocolError


logger = logging.getLogger("stonebot.mcp")


class MCPServer:
    def __init__(
        self,
        service: PluginJobService,
        *,
        tool_profile: str = "full",
    ) -> None:
        self.service = service
        if tool_profile not in TOOL_PROFILES:
            raise ValueError(f"unknown Stonebot tool profile: {tool_profile}")
        self.tool_profile = tool_profile
        self.initialized = False
        self._methods = {
            "initialize": self._initialize,
            "ping": lambda _params: {},
            "tools/list": self._list_tools,
            "tools/call": self._call_tool,
            "resources/list": self._list_resources,
            "resources/templates/list": self._list_resource_templates,
            "resources/read": self._read_resource,
            "logging/setLevel": lambda _params: {},
        }

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if message.get("jsonrpc") != "2.0":
            return self._error(
                message.get("id"),
                -32600,
                "Invalid JSON-RPC request",
            )
        method = str(message.get("method") or "")
        request_id = message.get("id")
        is_notification = "id" not in message

        if method == "notifications/initialized":
            self.initialized = True
            return None
        if method in {
            "notifications/cancelled",
            "notifications/roots/list_changed",
        }:
            return None

        handler = self._methods.get(method)
        if handler is None:
            if is_notification:
                return None
            return self._error(
                request_id,
                -32601,
                f"Method not found: {method}",
            )
        try:
            result = handler(message.get("params") or {})
        except StonebotProtocolError as exc:
            # Required MCP initialization cannot be represented as a tool
            # result; every other service error stays structured.
            if method == "tools/call":
                return self._result(
                    request_id,
                    tool_result(exc.to_dict(), is_error=True),
                )
            return self._error(
                request_id,
                -32000,
                exc.message,
                data=exc.to_dict(),
            )
        except Exception as exc:  # noqa: BLE001 - transport boundary
            logger.exception("MCP method failed: %s", method)
            if method == "tools/call":
                return self._result(
                    request_id,
                    tool_result(
                        {
                            "ok": False,
                            "error": {
                                "code": "INTERNAL_ERROR",
                                "message": str(exc),
                            },
                        },
                        is_error=True,
                    ),
                )
            return self._error(
                request_id,
                -32603,
                "Internal error",
                data={"message": str(exc)},
            )
        if is_notification:
            return None
        return self._result(request_id, result)

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = str(params.get("protocolVersion") or "").strip()
        self.initialized = True
        return {
            "protocolVersion": requested or MCP_PROTOCOL_VERSION,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {
                    "subscribe": False,
                    "listChanged": False,
                },
            },
            "serverInfo": {
                "name": "stonebot-artificial-stone-layout",
                "version": PLUGIN_VERSION,
            },
            "instructions": (
                "Use Stonebot tools for all drawing and nesting work. "
                "Never infer a successful geometric result from prose; "
                "inspect structured validation state and obtain explicit "
                "human confirmation at each gate. The portable contract is "
                "available at stonebot://agent-contract."
            ),
        }

    def _list_tools(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {
            "tools": tool_definitions_for_profile(self.tool_profile)
        }

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise StonebotProtocolError(
                "INVALID_ARGUMENTS",
                "Tool arguments must be a JSON object.",
            )
        handlers = {
            "stonebot_analyze_drawing": self.service.analyze_drawing,
            "stonebot_configure_nesting": self.service.configure_nesting,
            "stonebot_list_drawing_issues": self.service.list_drawing_issues,
            "stonebot_list_correction_targets": (
                self.service.list_correction_targets
            ),
            "stonebot_correct_drawing": self.service.correct_drawing,
            "stonebot_rename_panels": self.service.rename_panels,
            "stonebot_confirm_drawing": self.service.confirm_drawing,
            "stonebot_confirm_nesting": self.service.confirm_nesting,
            "stonebot_choose_output_directory": (
                self.service.choose_output_directory
            ),
            "stonebot_finalize_job": self.service.finalize_job,
            "stonebot_export_word_report": (
                self.service.export_word_report
            ),
            "stonebot_advance_until_gate": (
                self.service.advance_until_gate
            ),
            "stonebot_get_agent_contract": (
                self.service.get_agent_contract
            ),
            "stonebot_list_profile_patches": (
                self.service.list_profile_patches
            ),
            "stonebot_accept_profile_patch": (
                self.service.accept_profile_patch
            ),
            "stonebot_review_profile_patch": (
                self.service.review_profile_patch
            ),
            "stonebot_reject_profile_patch": (
                self.service.reject_profile_patch
            ),
            "stonebot_list_profile_overrides": (
                self.service.list_profile_overrides
            ),
            "stonebot_rollback_profile_override": (
                self.service.rollback_profile_override
            ),
            "stonebot_job_status": self.service.job_status,
            "stonebot_list_jobs": self.service.list_jobs,
            "stonebot_cancel_job": self.service.cancel_job,
        }
        handler = handlers.get(name)
        if handler is None:
            raise StonebotProtocolError(
                "UNKNOWN_TOOL",
                f"Unknown Stonebot tool: {name}",
                allowed_actions=["stonebot_list_jobs"],
            )
        if name not in set(TOOL_PROFILES[self.tool_profile]):
            raise StonebotProtocolError(
                "TOOL_NOT_IN_PROFILE",
                f"Tool {name!r} is not enabled in the "
                f"{self.tool_profile!r} profile.",
                allowed_actions=["stonebot_get_agent_contract"],
            )
        # Existing Stonebot modules may emit informational text to stdout.
        # MCP stdio owns stdout for JSON-RPC, so all tool-side output must be
        # redirected to stderr while an invocation is in progress.
        with redirect_stdout(sys.stderr):
            payload = handler(arguments)
        return tool_result(payload)

    def _list_resources(self, _params: dict[str, Any]) -> dict[str, Any]:
        jobs = self.service.list_jobs({}).get("jobs", [])
        resources = [
            {
                "uri": "stonebot://agent-contract",
                "name": "Stonebot Agent Contract",
                "description": (
                    "Portable workflow, tool profile and policy contract."
                ),
                "mimeType": "application/json",
            },
            {
                "uri": "stonebot://jobs",
                "name": "Stonebot jobs",
                "description": "All jobs known to this plugin service.",
                "mimeType": "application/json",
            }
        ]
        resources.extend(
            {
                "uri": f"stonebot://jobs/{job['job_id']}",
                "name": f"Stonebot job {job['job_id']}",
                "description": (
                    f"Job status for project {job.get('project_id', '')}."
                ),
                "mimeType": "application/json",
            }
            for job in jobs
        )
        return {"resources": resources}

    def _list_resource_templates(
        self,
        _params: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "resourceTemplates": [
                {
                    "uriTemplate": "stonebot://agent-contract/{profile}",
                    "name": "Stonebot Agent Contract profile",
                    "description": (
                        "Portable Stonebot contract for the full or compact "
                        "Harness tool profile."
                    ),
                    "mimeType": "application/json",
                },
                {
                    "uriTemplate": "stonebot://jobs/{job_id}",
                    "name": "Stonebot job status",
                    "description": (
                        "Compact state, validation and artifact summary for "
                        "one Stonebot job."
                    ),
                    "mimeType": "application/json",
                }
            ]
        }

    def _read_resource(self, params: dict[str, Any]) -> dict[str, Any]:
        uri = str(params.get("uri") or "")
        if uri == "stonebot://jobs":
            payload = self.service.list_jobs({})
        elif uri == "stonebot://agent-contract":
            payload = self.service.get_agent_contract(
                {"profile": self.tool_profile}
            )
        elif uri.startswith("stonebot://agent-contract/"):
            profile = uri.removeprefix(
                "stonebot://agent-contract/"
            ).strip("/")
            payload = self.service.get_agent_contract(
                {"profile": profile}
            )
        elif uri.startswith("stonebot://jobs/"):
            job_id = uri.removeprefix("stonebot://jobs/").strip("/")
            payload = self.service.job_status({"job_id": job_id})
        else:
            raise StonebotProtocolError(
                "RESOURCE_NOT_FOUND",
                f"Unknown Stonebot resource: {uri}",
            )
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": json.dumps(
                        payload,
                        ensure_ascii=False,
                        indent=2,
                    ),
                }
            ]
        }

    def _result(self, request_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _error(
        self,
        request_id: Any,
        code: int,
        message: str,
        *,
        data: Any | None = None,
    ) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}


def serve_stdio(
    *,
    service: PluginJobService | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    tool_profile: str = "full",
) -> None:
    server = MCPServer(
        service or PluginJobService(),
        tool_profile=tool_profile,
    )
    source = stdin or sys.stdin
    target = stdout or sys.stdout
    for raw_line in source:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = server._error(None, -32700, "Parse error")
        else:
            if not isinstance(message, dict):
                response = server._error(
                    None,
                    -32600,
                    "Invalid JSON-RPC request",
                )
            else:
                response = server.handle(message)
        if response is not None:
            target.write(
                json.dumps(
                    response,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
            target.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stonebot drawing and nesting MCP server",
    )
    parser.add_argument(
        "--data-root",
        help="Plugin job data directory; defaults to STONEBOT_MCP_DATA_ROOT.",
    )
    parser.add_argument(
        "--tool-profile",
        choices=sorted(TOOL_PROFILES),
        default=os.environ.get("STONEBOT_TOOL_PROFILE", "full"),
        help="Expose the full or compact Agent tool profile.",
    )
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)
    if args.version:
        print(f"{PLUGIN_VERSION} schema={SCHEMA_VERSION}")
        return 0
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    serve_stdio(
        service=PluginJobService(
            Path(args.data_root) if args.data_root else None
        ),
        tool_profile=args.tool_profile,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
