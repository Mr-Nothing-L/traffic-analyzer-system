"""
工具注册表集成

将项目中已有的工具 (如 yolo_track_tool) 注册到 ToolRouter。
提供默认的 router 实例供上层使用。
"""

from __future__ import annotations

import logging
from typing import Optional

from .tool_schema import (
    ParameterType,
    ToolConstraint,
    ToolDefinition,
    ToolParameter,
    ToolReturn,
)
from .tool_router import ToolRouter
from .yolo_track_tool import YoloTrackTool

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 默认 Router 实例 (懒加载)
# ──────────────────────────────────────────────

_default_router: Optional[ToolRouter] = None


def get_default_router() -> ToolRouter:
    """
    获取默认的 ToolRouter 实例 (单例)。

    首次调用时会自动注册所有内置工具。
    """
    global _default_router
    if _default_router is None:
        _default_router = create_router()
        logger.info(f"默认 ToolRouter 创建完成，注册工具: {_default_router.list_tools()}")
    return _default_router


def create_router() -> ToolRouter:
    """
    创建新的 ToolRouter 并注册所有工具。

    返回全新的实例 (非单例)。
    """
    router = ToolRouter()

    # 注册 yolo_track_tool
    _register_yolo_track_tool(router)

    # TODO: 注册更多工具
    # _register_scene_understanding_tool(router)
    # _register_video_preprocessing_tool(router)

    return router


# ──────────────────────────────────────────────
# 工具注册函数
# ──────────────────────────────────────────────

def _register_yolo_track_tool(router: ToolRouter) -> None:
    """
    注册 YOLOv8 + ByteTrack 跟踪工具。

    该工具对视频进行目标检测和跟踪，输出:
    1. 带跟踪框的关键帧
    2. 每个跟踪目标的位移矢量表
    """

    definition = ToolDefinition(
        name="yolo_track_tool",
        description=(
            "对高速公路监控视频进行车辆/行人/摩托车目标检测和跟踪。"
            "使用 YOLOv8 检测 + ByteTrack 跟踪算法。"
            "输出带跟踪框的关键帧图片，以及每个跟踪目标的位移矢量表。"
            "适用于分析车辆运动轨迹、检测静止车辆(违停)、检测车辆逆行，倒车等场景。"
        ),
        parameters=[
            ToolParameter(
                name="video_path",
                type=ParameterType.STRING,
                description="输入视频文件的绝对路径 (容器内路径，如 /data/test_videos/test.mp4)",
                constraints=ToolConstraint(
                    required=True,
                    min_length=1,
                    pattern=r"^/.*\.(mp4|avi|mov|mkv)$",
                ),
            ),
            ToolParameter(
                name="output_dir",
                type=ParameterType.STRING,
                description="输出目录的绝对路径 (如 /data/output/tracking_result/)",
                constraints=ToolConstraint(
                    required=False,
                    min_length=1,
                ),
                default="/data/output",
            ),
            ToolParameter(
                name="model_path",
                type=ParameterType.STRING,
                description="YOLOv8 模型路径，默认自动下载 yolov8n.pt",
                constraints=ToolConstraint(required=False),
                default="yolov8n.pt",
            ),
            ToolParameter(
                name="conf_threshold",
                type=ParameterType.FLOAT,
                description="检测置信度阈值 (0.0-1.0)，越高越严格",
                constraints=ToolConstraint(
                    required=False,
                    min_value=0.0,
                    max_value=1.0,
                ),
                default=0.5,
            ),
            ToolParameter(
                name="stationary_threshold",
                type=ParameterType.INTEGER,
                description="静止判定阈值 (像素)，位移小于此值视为静止",
                constraints=ToolConstraint(
                    required=False,
                    min_value=0,
                    max_value=100,
                ),
                default=5,
            ),
            ToolParameter(
                name="target_classes",
                type=ParameterType.ARRAY,
                description="要跟踪的目标类别列表，如 ['car', 'truck', 'bus']",
                constraints=ToolConstraint(
                    required=False,
                    items_type=ParameterType.STRING,
                ),
                default=None,
            ),
        ],
        returns=ToolReturn(
            type=ParameterType.OBJECT,
            description=(
                "包含以下字段的对象: "
                "video_path (输入视频路径), "
                "total_frames (总帧数), "
                "tracked_objects (跟踪目标数量), "
                "keyframes_dir (关键帧目录), "
                "displacement_json (位移矢量表JSON路径), "
                "displacements (位移矢量表数据)"
            ),
        ),
        examples=[
            {
                "tool_name": "yolo_track_tool",
                "arguments": {
                    "video_path": "/data/test_videos/highway_01.mp4",
                    "conf_threshold": 0.5,
                },
            },
            {
                "tool_name": "yolo_track_tool",
                "arguments": {
                    "video_path": "/data/test_videos/highway_02.mp4",
                    "output_dir": "/data/output/tracking_02",
                    "target_classes": ["car", "truck"],
                    "stationary_threshold": 3,
                },
            },
        ],
    )

    def handler(
        video_path: str,
        output_dir: str = "/data/output",
        model_path: str = "yolov8n.pt",
        conf_threshold: float = 0.5,
        stationary_threshold: int = 5,
        target_classes: Optional[list] = None,
    ) -> dict:
        """yolo_track_tool 的 handler 包装。"""
        tool = YoloTrackTool(
            model_path=model_path,
            conf_threshold=conf_threshold,
            stationary_threshold=float(stationary_threshold),
        )
        result = tool.track(video_path, output_dir=output_dir)
        return result.to_dict()

    router.register(definition, handler)
    logger.info("yolo_track_tool 注册完成")


# ──────────────────────────────────────────────
# 快捷函数
# ──────────────────────────────────────────────

def execute_tool(request_json: str) -> str:
    """
    快捷函数: 解析 JSON 请求，执行工具，返回 JSON 结果。

    Args:
        request_json: 工具调用请求的 JSON 字符串

    Returns:
        ToolResponse 的 JSON 字符串
    """
    router = get_default_router()
    response = router.route(request_json)
    return response.to_json()
