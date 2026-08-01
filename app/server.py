"""Flask 服务器 — 人造石排板系统前端 + API"""

import json
import os
import uuid
from pathlib import Path

from flask import (Flask, Response, jsonify, render_template, request,
                   send_file, session, stream_with_context)

from app.workflow import SessionState, llm_chat

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "stonebot-dev-key")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UPLOAD_DIR = PROJECT_ROOT / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# 内存中的会话状态（keyed by session id）
SESSIONS: dict[str, SessionState] = {}


def _get_state() -> SessionState:
    sid = session.get("sid")
    if not sid or sid not in SESSIONS:
        sid = uuid.uuid4().hex[:12]
        session["sid"] = sid
        SESSIONS[sid] = SessionState()
    return SESSIONS[sid]


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.route("/api/chat", methods=["POST"])
def chat():
    """处理对话消息。支持 JSON 和多部分表单上传。"""
    state = _get_state()

    if request.is_json:
        data = request.get_json()
        msg = data.get("message", "").strip()
        if not msg:
            return jsonify({"error": "empty message"}), 400
    else:
        msg = request.form.get("message", "").strip()

    if not msg and "file" not in request.files:
        return jsonify({"error": "empty message"}), 400

    # 如果附带 DXF 文件
    dxf_file = request.files.get("file") if "file" in request.files else None
    if dxf_file and dxf_file.filename and dxf_file.filename.lower().endswith(".dxf"):
        fname = f"{uuid.uuid4().hex[:8]}_{dxf_file.filename}"
        save_path = UPLOAD_DIR / fname
        dxf_file.save(str(save_path))
        state.dxf_path = str(save_path)
        state.step = "numbered"
        reply = f"DXF 文件已上传：{dxf_file.filename}。是否需要生成带编号的纯规格板DXF文件以便检查？（是/否）"
        state.messages.append({"role": "user", "content": f"[上传了 {dxf_file.filename}]"})
        state.messages.append({"role": "assistant", "content": reply})
        return jsonify({"reply": reply})

    reply = llm_chat(state, msg)
    return jsonify({"reply": reply, "step": state.step})


@app.route("/api/reset", methods=["POST"])
def reset():
    state = _get_state()
    state.__init__()
    return jsonify({"ok": True})


@app.route("/api/download/<path:filename>")
def download_file(filename):
    """下载生成的文件"""
    from urllib.parse import unquote
    filename = unquote(filename)
    # 优先从 output/ 查找，其次从 uploads/
    for base in (PROJECT_ROOT / "output", UPLOAD_DIR):
        p = base / Path(filename).name
        if p.exists():
            return send_file(str(p), as_attachment=True)
    return jsonify({"error": "file not found"}), 404


@app.route("/api/state")
def get_state():
    state = _get_state()
    return jsonify({
        "step": state.step,
        "sheet_width": state.sheet_width,
        "sheet_height": state.sheet_height,
        "sheet_thickness": state.sheet_thickness,
        "dxf_path": state.dxf_path,
        "numbered_dxf_path": state.numbered_dxf_path,
    })


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
