"""
工具路由层测试

覆盖:
- ToolRequest 解析 (JSON / Markdown / XML)
- ToolResponse 创建和序列化
- ToolRouter 注册、校验、路由执行
- yolo_track_tool 集成
- 错误处理
"""

import json
import pytest
from typing import Any, Dict

from traffic_analyzer.tools.tool_schema import (
    ParameterType,
    ToolConstraint,
    ToolDefinition,
    ToolParameter,
    ToolReturn,
    ToolRegistry,
)
from traffic_analyzer.tools.tool_router import ToolRequest, ToolResponse, ToolRouter


# ──────────────────────────────────────────────
# ToolRequest 测试
# ──────────────────────────────────────────────

class TestToolRequest:
    def test_basic_construction(self):
        req = ToolRequest(tool_name="test_tool", arguments={"a": 1})
        assert req.tool_name == "test_tool"
        assert req.arguments == {"a": 1}
        assert req.request_id is None

    def test_from_json_plain(self):
        json_str = '{"tool_name": "calc", "arguments": {"x": 10, "y": 20}}'
        req = ToolRequest.from_json(json_str)
        assert req.tool_name == "calc"
        assert req.arguments == {"x": 10, "y": 20}

    def test_from_json_markdown(self):
        json_str = '```json\n{"tool_name": "calc", "arguments": {"x": 1}}\n```'
        req = ToolRequest.from_json(json_str)
        assert req.tool_name == "calc"

    def test_from_json_xml(self):
        json_str = '<tool_call>{"tool_name": "calc", "arguments": {"x": 1}}</tool_call>'
        req = ToolRequest.from_json(json_str)
        assert req.tool_name == "calc"

    def test_from_json_invalid(self):
        with pytest.raises(ValueError, match="无效的 JSON"):
            ToolRequest.from_json("not json at all")

    def test_from_json_missing_tool_name(self):
        with pytest.raises(ValueError, match="缺少 tool_name"):
            ToolRequest.from_json('{"arguments": {}}')

    def test_to_json(self):
        req = ToolRequest(tool_name="test", arguments={"a": 1}, request_id="req-1")
        s = req.to_json()
        data = json.loads(s)
        assert data["tool_name"] == "test"
        assert data["request_id"] == "req-1"


# ──────────────────────────────────────────────
# ToolResponse 测试
# ──────────────────────────────────────────────

class TestToolResponse:
    def test_success_response(self):
        resp = ToolResponse.success_response("tool", {"result": 42})
        assert resp.success is True
        assert resp.data == {"result": 42}
        assert resp.error is None

    def test_error_response(self):
        resp = ToolResponse.error_response("tool", "something wrong")
        assert resp.success is False
        assert resp.error == "something wrong"
        assert resp.data is None

    def test_to_markdown_success(self):
        resp = ToolResponse.success_response("tool", {"val": 1})
        md = resp.to_markdown()
        assert "执行成功" in md
        assert "val" in md

    def test_to_markdown_error(self):
        resp = ToolResponse.error_response("tool", "fail")
        md = resp.to_markdown()
        assert "执行失败" in md
        assert "fail" in md


# ──────────────────────────────────────────────
# ToolRouter 基础测试
# ──────────────────────────────────────────────

class TestToolRouter:
    @pytest.fixture
    def router(self):
        return ToolRouter()

    def test_register_and_list(self, router: ToolRouter):
        def handler(x: int) -> int:
            return x * 2

        definition = ToolDefinition(
            name="double",
            description="将输入数字翻倍，返回其两倍值",
            parameters=[
                ToolParameter(
                    name="x",
                    type=ParameterType.INTEGER,
                    description="输入数字",
                    constraints=ToolConstraint(required=True),
                )
            ],
            returns=ToolReturn(type=ParameterType.INTEGER, description="翻倍后的数字"),
        )

        router.register(definition, handler)
        assert "double" in router.list_tools()
        assert router.get_tool("double").name == "double"

    def test_route_success(self, router: ToolRouter):
        def handler(x: int) -> int:
            return x * 2

        definition = ToolDefinition(
            name="double",
            description="将输入数字翻倍，返回其两倍值",
            parameters=[
                ToolParameter(
                    name="x",
                    type=ParameterType.INTEGER,
                    description="输入数字",
                    constraints=ToolConstraint(required=True),
                )
            ],
            returns=ToolReturn(type=ParameterType.INTEGER, description="翻倍后的数字"),
        )

        router.register(definition, handler)
        resp = router.route('{"tool_name": "double", "arguments": {"x": 5}}')
        assert resp.success is True
        assert resp.data == 10

    def test_route_tool_not_found(self, router: ToolRouter):
        resp = router.route('{"tool_name": "nonexist", "arguments": {}}')
        assert resp.success is False
        assert "未注册" in resp.error

    def test_route_validation_error(self, router: ToolRouter):
        def handler(x: int) -> int:
            return x

        definition = ToolDefinition(
            name="identity",
            description="返回输入值本身，用于测试参数校验",
            parameters=[
                ToolParameter(
                    name="x",
                    type=ParameterType.INTEGER,
                    description="输入",
                    constraints=ToolConstraint(required=True, min_value=0, max_value=100),
                )
            ],
            returns=ToolReturn(type=ParameterType.INTEGER, description="输出"),
        )

        router.register(definition, handler)

        # 缺少必填参数
        resp = router.route('{"tool_name": "identity", "arguments": {}}')
        assert resp.success is False
        assert "缺少必填参数" in resp.error

        # 超出范围
        resp = router.route('{"tool_name": "identity", "arguments": {"x": 200}}')
        assert resp.success is False
        assert "大于最大值" in resp.error

    def test_route_handler_exception(self, router: ToolRouter):
        def handler(x: int) -> int:
            raise RuntimeError("boom")

        definition = ToolDefinition(
            name="boom",
            description="总是抛出异常，用于测试错误处理机制",
            parameters=[
                ToolParameter(
                    name="x",
                    type=ParameterType.INTEGER,
                    description="输入",
                    constraints=ToolConstraint(required=True),
                )
            ],
            returns=ToolReturn(type=ParameterType.INTEGER, description="不会返回"),
        )

        router.register(definition, handler)
        resp = router.route('{"tool_name": "boom", "arguments": {"x": 1}}')
        assert resp.success is False
        assert "boom" in resp.error
        assert "traceback" in resp.error.lower() or "Traceback" in resp.error

    def test_get_tool_descriptions_json(self, router: ToolRouter):
        def handler():
            return 1

        definition = ToolDefinition(
            name="one",
            description="返回数字 1，用于测试工具描述输出",
            parameters=[],
            returns=ToolReturn(type=ParameterType.INTEGER, description="数字 1"),
        )

        router.register(definition, handler)
        desc = router.get_tool_descriptions(format="json")
        assert isinstance(desc, list)
        assert desc[0]["type"] == "function"

    def test_batch_route(self, router: ToolRouter):
        def handler(x: int) -> int:
            return x + 1

        definition = ToolDefinition(
            name="inc",
            description="将输入数字加一，返回结果",
            parameters=[
                ToolParameter(
                    name="x",
                    type=ParameterType.INTEGER,
                    description="输入",
                    constraints=ToolConstraint(required=True),
                )
            ],
            returns=ToolReturn(type=ParameterType.INTEGER, description="输出"),
        )

        router.register(definition, handler)
        reqs = [
            {"tool_name": "inc", "arguments": {"x": 1}},
            {"tool_name": "inc", "arguments": {"x": 2}},
        ]
        resps = router.route_batch(reqs)
        assert len(resps) == 2
        assert resps[0].data == 2
        assert resps[1].data == 3


# ──────────────────────────────────────────────
# 参数约束测试
# ──────────────────────────────────────────────

class TestParameterConstraints:
    def test_string_pattern(self):
        param = ToolParameter(
            name="email",
            type=ParameterType.STRING,
            description="邮箱",
            constraints=ToolConstraint(pattern=r"^[\w\.-]+@[\w\.-]+\.\w+$"),
        )
        errors = param.validate_value("test@example.com")
        assert len(errors) == 0

        errors = param.validate_value("invalid")
        assert len(errors) == 1
        assert "不匹配正则" in errors[0]

    def test_enum_constraint(self):
        param = ToolParameter(
            name="color",
            type=ParameterType.ENUM,
            description="颜色",
            constraints=ToolConstraint(enum_values=["red", "green", "blue"]),
        )
        errors = param.validate_value("red")
        assert len(errors) == 0

        errors = param.validate_value("yellow")
        assert len(errors) == 1
        assert "不在枚举范围" in errors[0]

    def test_array_items_type(self):
        param = ToolParameter(
            name="scores",
            type=ParameterType.ARRAY,
            description="分数列表",
            constraints=ToolConstraint(items_type=ParameterType.FLOAT),
        )
        errors = param.validate_value([1.0, 2.5, 3.0])
        assert len(errors) == 0

        errors = param.validate_value([1, "two", 3])
        assert len(errors) >= 1


# ──────────────────────────────────────────────
# yolo_track_tool 集成测试 (mock)
# ──────────────────────────────────────────────

class TestYoloTrackIntegration:
    def test_tool_definition_schema(self):
        """验证 yolo_track_tool 的定义符合 schema"""
        from traffic_analyzer.tools.tool_registry import create_router

        router = create_router()
        definition = router.get_tool("yolo_track_tool")
        assert definition is not None
        assert definition.name == "yolo_track_tool"
        assert len(definition.parameters) >= 2  # video_path + 其他

        # 检查必填参数
        param_names = [p.name for p in definition.parameters]
        assert "video_path" in param_names

        # 检查参数约束
        video_param = next(p for p in definition.parameters if p.name == "video_path")
        assert video_param.constraints.required is True

    def test_tool_registration(self):
        """验证 yolo_track_tool 已注册到默认 router"""
        from traffic_analyzer.tools.tool_registry import get_default_router

        router = get_default_router()
        assert "yolo_track_tool" in router.list_tools()

    def test_mock_yolo_track_execution(self, monkeypatch):
        """mock YoloTrackTool.track 测试路由执行"""
        from traffic_analyzer.tools.tool_registry import create_router
        from traffic_analyzer.tools.yolo_track_tool import TrackResult

        router = create_router()

        # Mock track 方法
        def mock_track(*args, **kwargs):
            return TrackResult(
                success=True,
                total_frames=100,
                processed_frames=100,
                vehicle_count=5,
                video_width=1920,
                video_height=1080,
            )

        monkeypatch.setattr(
            "traffic_analyzer.tools.yolo_track_tool.YoloTrackTool.track",
            mock_track,
        )

        resp = router.route(
            json.dumps({
                "tool_name": "yolo_track_tool",
                "arguments": {
                    "video_path": "/data/test_videos/test.mp4",
                    "conf_threshold": 0.5,
                },
            })
        )

        assert resp.success is True
        assert resp.data["success"] is True
        assert resp.data["vehicle_count"] == 5

    def test_yolo_track_invalid_video_path(self):
        """测试无效视频路径参数校验"""
        from traffic_analyzer.tools.tool_registry import create_router

        router = create_router()
        resp = router.route(
            json.dumps({
                "tool_name": "yolo_track_tool",
                "arguments": {
                    "video_path": "not_an_absolute_path.avi",  # 不匹配正则
                },
            })
        )

        assert resp.success is False
        assert "不匹配正则" in resp.error

    def test_yolo_track_missing_required(self):
        """测试缺少必填参数"""
        from traffic_analyzer.tools.tool_registry import create_router

        router = create_router()
        resp = router.route(
            json.dumps({
                "tool_name": "yolo_track_tool",
                "arguments": {},
            })
        )

        assert resp.success is False
        assert "缺少必填参数" in resp.error


# ──────────────────────────────────────────────
# 异步测试
# ──────────────────────────────────────────────

class TestAsyncRouter:
    @pytest.mark.asyncio
    async def test_async_handler(self):
        router = ToolRouter()

        async def async_handler(x: int) -> int:
            return x * 3

        definition = ToolDefinition(
            name="triple",
            description="将输入数字乘以三，返回结果",
            parameters=[
                ToolParameter(
                    name="x",
                    type=ParameterType.INTEGER,
                    description="输入",
                    constraints=ToolConstraint(required=True),
                )
            ],
            returns=ToolReturn(type=ParameterType.INTEGER, description="输出"),
        )

        router.register(definition, async_handler)
        resp = await router.route_async('{"tool_name": "triple", "arguments": {"x": 4}}')
        assert resp.success is True
        assert resp.data == 12

    @pytest.mark.asyncio
    async def test_sync_handler_in_async_context(self):
        """同步 handler 在异步上下文中应在线程池执行"""
        router = ToolRouter()

        def sync_handler(x: int) -> int:
            return x + 10

        definition = ToolDefinition(
            name="add10",
            description="将输入数字加10，返回结果",
            parameters=[
                ToolParameter(
                    name="x",
                    type=ParameterType.INTEGER,
                    description="输入",
                    constraints=ToolConstraint(required=True),
                )
            ],
            returns=ToolReturn(type=ParameterType.INTEGER, description="输出"),
        )

        router.register(definition, sync_handler)
        resp = await router.route_async('{"tool_name": "add10", "arguments": {"x": 5}}')
        assert resp.success is True
        assert resp.data == 15


# ──────────────────────────────────────────────
# 工具注册表测试
# ──────────────────────────────────────────────

class TestToolRegistry:
    def test_duplicate_registration(self):
        registry = ToolRegistry()

        def handler():
            return 1

        definition = ToolDefinition(
            name="dup",
            description="测试重复注册时是否抛出异常",
            parameters=[],
            returns=ToolReturn(type=ParameterType.INTEGER, description="输出"),
        )

        registry.register(definition, handler)
        with pytest.raises(ValueError, match="已注册"):
            registry.register(definition, handler)

    def test_get_all_schemas(self):
        registry = ToolRegistry()

        def handler():
            return 1

        definition = ToolDefinition(
            name="test",
            description="测试工具注册表功能的基础工具",
            parameters=[],
            returns=ToolReturn(type=ParameterType.INTEGER, description="输出"),
        )

        registry.register(definition, handler)
        schemas = registry.get_all_schemas()
        assert len(schemas) == 1
        assert schemas[0]["type"] == "function"
