"""Shared filesystem locations for the quick-chat feature.

[文件说明]
作用:快速对话功能的共享路径常量。UPLOAD_DIR 为聊天上传/产出根目录
(仓库根 output/chat_uploads/,上传原始文件在 incoming/ 子目录,画框结果图在
<sha1(ip)[:12]>/ 子目录);INCOMING_DIR 为上传原始文件目录。
上游:web/chat/qa.py(画框图落盘)、web/chat/routes.py(上传写盘、文件服务)、
web/chat/janitor.py(过期清理)。
下游:仅文件系统路径;不执行任何 IO。
"""

from __future__ import annotations

from pathlib import Path

# traffic_analyzer/web/chat/paths.py → parents[3] = 仓库根。
REPO_ROOT = Path(__file__).resolve().parents[3]

UPLOAD_DIR = REPO_ROOT / "output" / "chat_uploads"
INCOMING_DIR = UPLOAD_DIR / "incoming"
