# -*- coding: utf-8 -*-
"""人造石排板系统 - 启动脚本"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.server import app

PORT = int(os.environ.get("PORT", "5000"))
print(f"人造石排板系统启动中... http://127.0.0.1:{PORT}")
app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
