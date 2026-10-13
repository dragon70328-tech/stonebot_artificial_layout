# -*- coding: utf-8 -*-
"""自检 LLM 配置：密钥 / base_url / model 是否可用（不完整打印密钥）。

用法：
  . .\\scripts\\dev_env.ps1        # 先载入环境变量（或自己 export OPENAI_*）
  python -X utf8 scripts/check_llm.py

退出码：0 = 调用成功；1 = 配置缺失或调用失败。
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.llm_client import LLMError  # noqa: E402
from src.openai_llm_client import OpenAICompatibleClient  # noqa: E402


def main() -> int:
    client = OpenAICompatibleClient()
    key = client.api_key or ""
    print(f"base_url : {client.base_url}")
    print(f"model    : {client.model}")
    if not key:
        print("api_key  : 未设置（请先载入 scripts/dev_env.ps1）")
        return 1
    print(f"api_key  : {key[:6]}…（长度 {len(key)}）")
    if key.startswith("sk-在这里填"):
        print("提示：dev_env.ps1 里还是占位符，请填回轮换后的新密钥。")
        return 1
    try:
        result = client.complete_json(
            "只输出 JSON，不要任何多余文字。",
            '请返回 {"ok": true, "echo": "pong"}',
        )
    except LLMError as exc:
        print(f"调用失败：{exc}")
        return 1
    print(f"调用成功：{result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
