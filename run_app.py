# -*- coding: utf-8 -*-
"""人造石排板系统 - 启动脚本"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 尝试加载 .env 文件
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from app.server import app

PORT = int(os.environ.get("PORT", "5000"))

# 检查 API 配置
api_key = os.environ.get("OPENAI_API_KEY", "")
if api_key:
    model = os.environ.get("OPENAI_MODEL", "deepseek-chat")
    base = os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com")
    print(f"LLM 已配置: {model} @ {base}")
else:
    print("提示: 未设置 OPENAI_API_KEY，将使用规则引擎模式")
    print("设置方法: set OPENAI_API_KEY=sk-your-key 或创建 .env 文件")

print(f"人造石排板系统启动中... http://127.0.0.1:{PORT}")
app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
