"""Lightweight FastAPI tool server exposing video CV capabilities over local HTTP.

[文件说明]
作用:工具服务包入口。create_app(workspace) 构建 FastAPI 应用,把
    video_meta / extract_frames / draw_boxes 三个能力暴露为 POST /tools/*
    JSON 端点,供 TS agent 运行时的工具层通过本地回环 HTTP 调用。
上游:agent/src/tools/(TS HTTP client);__main__.py(uvicorn 启动入口)。
下游:server.py(路由与处理);web/frames.py(抽帧/元信息);utils/image_drawing.py、
    utils/bbox_geometry.py(画框原语)。
"""

from __future__ import annotations

from traffic_analyzer.toolserver.server import create_app

__all__ = ["create_app"]
