"""
工具路由层 (Tool Router Layer)

负责解析专家 Agent 的结构化调用请求，匹配并执行工具，返回结果。

核心流程:
1. 模型输出 ToolRequest (JSON)
2. ToolRouter 解析并校验
3. 匹配工具定义，执行 handler
4. 封装为 ToolResponse 返回
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import traceback
from typing import Any, Callable, Dict, List, Optional, Union

from pydantic import BaseModel, Field, field_validator

from .tool_schema import ToolDefinition, ToolRegistry

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 工具调用请求
# ──────────────────────────────────────────────

class ToolRequest(BaseModel):
    """
    模型输出的工具调用请求。

    期望模型输出如下 JSON 结构:
    ```json
    {
        "tool_name": "yolo_track_tool",
        "arguments": {
            "video_path": "/data/test_videos/test.mp4",
            "conf_threshold": 0.5
        }
    }
    ```

    Attributes:
        tool_name: 要调用的工具名称
        arguments: 参数字典
        request_id: 可选的请求标识 (用于追踪)
    """
    tool_name: str = Field(..., min_length=1)
    arguments: Dict[str, Any] = Field(default_factory=dict)
    request_id: Optional[str] = None

    @field_validator("tool_name")
    @classmethod
    def _validate_tool_name(cls, v: str) -> str:
        if not v.replace("_", "").isalnum():
            raise ValueError(f"工具名称只能包含字母、数字和下划线: {v}")
        return v

    @classmethod
    def from_json(cls, json_str: str) -> "ToolRequest":
        """
        从 JSON 字符串解析 ToolRequest。

        支持以下格式:
        - 纯 JSON: {"tool_name": "...", "arguments": {...}}
        - Markdown 代码块: ```json\n{...}\n```
        - XML 标签: <tool_call>{...}</tool_call>

        Args:
            json_str: JSON 字符串或包含 JSON 的文本

        Returns:
            ToolRequest 实例

        Raises:
            ValueError: 解析失败
        """
        # 尝试从 Markdown 代码块提取
        text = json_str.strip()
        if "```" in text:
            # 提取 ```json 和 ``` 之间的内容
            parts = text.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{") and part.endswith("}"):
                    text = part
                    break

        # 尝试从 XML 标签提取
        if "<tool_call>" in text and "</tool_call>" in text:
            start = text.index("<tool_call>") + len("<tool_call>")
            end = text.index("</tool_call>")
            text = text[start:end].strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析失败: {e}\n原文: {json_str[:500]}")
            raise ValueError(f"无效的 JSON 格式: {e}") from e

        # 兼容不同字段名
        tool_name = data.get("tool_name") or data.get("name") or data.get("function")
        if not tool_name:
            raise ValueError("缺少 tool_name 字段")

        arguments = data.get("arguments") or data.get("args") or data.get("parameters") or {}
        request_id = data.get("request_id") or data.get("id")

        return cls(
            tool_name=tool_name,
            arguments=arguments,
            request_id=request_id,
        )

    def to_json(self) -> str:
        """序列化为 JSON 字符串。"""
        return self.model_dump_json(indent=2)


# ──────────────────────────────────────────────
# 工具执行结果
# ──────────────────────────────────────────────

class ToolResponse(BaseModel):
    """
    工具执行结果。

    Attributes:
        success: 是否成功
        data: 成功时的返回数据
        error: 失败时的错误信息
        tool_name: 调用的工具名称
        request_id: 对应请求的标识
        execution_time_ms: 执行耗时 (毫秒)
    """
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    tool_name: str
    request_id: Optional[str] = None
    execution_time_ms: Optional[float] = None

    @classmethod
    def success_response(
        cls,
        tool_name: str,
        data: Any,
        request_id: Optional[str] = None,
        execution_time_ms: Optional[float] = None,
    ) -> "ToolResponse":
        """创建成功响应。"""
        return cls(
            success=True,
            data=data,
            tool_name=tool_name,
            request_id=request_id,
            execution_time_ms=execution_time_ms,
        )

    @classmethod
    def error_response(
        cls,
        tool_name: str,
        error: str,
        request_id: Optional[str] = None,
        execution_time_ms: Optional[float] = None,
    ) -> "ToolResponse":
        """创建错误响应。"""
        return cls(
            success=False,
            error=error,
            tool_name=tool_name,
            request_id=request_id,
            execution_time_ms=execution_time_ms,
        )

    def to_json(self) -> str:
        """序列化为 JSON 字符串。"""
        return self.model_dump_json(indent=2, exclude_none=True)

    def to_markdown(self) -> str:
        """转换为 Markdown 格式 (供模型阅读)。"""
        if self.success:
            lines = [
                f"**工具 `{self.tool_name}` 执行成功**",
                "",
                "```json",
                json.dumps(self.data, ensure_ascii=False, indent=2, default=str),
                "```",
            ]
        else:
            lines = [
                f"**工具 `{self.tool_name}` 执行失败**",
                "",
                f"错误: {self.error}",
            ]
        if self.execution_time_ms is not None:
            lines.append(f"\n耗时: {self.execution_time_ms:.2f}ms")
        return "\n".join(lines)


# ──────────────────────────────────────────────
# 工具路由器
# ──────────────────────────────────────────────

class ToolRouter:
    """
    工具路由器。

    管理工具注册表，负责:
    1. 注册工具 (定义 + handler)
    2. 解析模型输出的 ToolRequest
    3. 校验参数
    4. 路由到对应 handler 执行
    5. 封装为 ToolResponse 返回

    支持同步和异步 handler。
    """

    def __init__(self, registry: Optional[ToolRegistry] = None) -> None:
        self._registry = registry or ToolRegistry()
        logger.info(f"ToolRouter 初始化完成 (注册工具: {len(self._registry)}个)")

    # ── 注册 / 查询 ──

    def register(
        self,
        definition: ToolDefinition,
        handler: Callable,
    ) -> None:
        """
        注册工具。

        Args:
            definition: 工具定义
            handler: 执行函数

        Raises:
            ValueError: 工具名已存在或 handler 签名不匹配
        """
        # 检查 handler 是否为可调用对象
        if not callable(handler):
            raise ValueError(f"handler 必须是可调用对象, 得到 {type(handler)}")

        # 检查 handler 参数签名 (简单检查: 至少接受 **kwargs 或有对应参数)
        sig = inspect.signature(handler)
        param_names = set(sig.parameters.keys())
        arg_names = {p.name for p in definition.parameters}

        # 允许 handler 有额外的参数 (如 context, logger 等)
        # 但至少要能接收定义的参数
        has_kwargs = any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in sig.parameters.values()
        )
        if not has_kwargs:
            missing = arg_names - param_names
            if missing:
                logger.warning(
                    f"工具 '{definition.name}' 的 handler 缺少参数: {missing}. "
                    f"handler 签名: {list(param_names)}"
                )

        # 检查是否为异步函数
        definition.is_async = asyncio.iscoroutinefunction(handler)

        self._registry.register(definition, handler)

    def unregister(self, name: str) -> None:
        """注销工具。"""
        self._registry.unregister(name)

    def get_tool(self, name: str) -> Optional[ToolDefinition]:
        """获取工具定义。"""
        return self._registry.get_definition(name)

    def list_tools(self) -> List[str]:
        """列出所有已注册工具。"""
        return self._registry.list_tools()

    def get_tool_descriptions(self, format: str = "json") -> Union[str, List[Dict]]:
        """
        获取给模型看的工具描述。

        Args:
            format: "json" | "markdown"

        Returns:
            JSON Schema 列表或 Markdown 字符串
        """
        if format == "json":
            return self._registry.get_all_schemas()
        elif format == "markdown":
            return self._registry.get_all_markdown()
        else:
            raise ValueError(f"不支持的格式: {format}")

    # ── 路由执行 ──

    def route(self, request: Union[ToolRequest, str, Dict]) -> ToolResponse:
        """
        路由并执行工具调用。

        Args:
            request: ToolRequest 实例、JSON 字符串或字典

        Returns:
            ToolResponse
        """
        import time
        start_time = time.perf_counter()

        # 统一转换为 ToolRequest
        if isinstance(request, str):
            try:
                request = ToolRequest.from_json(request)
            except ValueError as e:
                return ToolResponse.error_response(
                    tool_name="unknown",
                    error=f"请求解析失败: {e}",
                    execution_time_ms=_elapsed_ms(start_time),
                )
        elif isinstance(request, dict):
            try:
                request = ToolRequest(**request)
            except Exception as e:
                return ToolResponse.error_response(
                    tool_name=request.get("tool_name", "unknown"),
                    error=f"请求构造失败: {e}",
                    execution_time_ms=_elapsed_ms(start_time),
                )

        tool_name = request.tool_name
        logger.info(f"路由请求: {tool_name} (request_id={request.request_id})")

        # 检查工具是否存在
        definition = self._registry.get_definition(tool_name)
        if definition is None:
            available = self.list_tools()
            error_msg = (
                f"工具 '{tool_name}' 未注册. "
                f"可用工具: {available if available else '无'}"
            )
            logger.error(error_msg)
            return ToolResponse.error_response(
                tool_name=tool_name,
                error=error_msg,
                request_id=request.request_id,
                execution_time_ms=_elapsed_ms(start_time),
            )

        # 参数校验
        validation_errors = definition.validate_arguments(request.arguments)
        if validation_errors:
            error_msg = "参数校验失败:\n" + "\n".join(f"  - {e}" for e in validation_errors)
            logger.error(f"[{tool_name}] {error_msg}")
            return ToolResponse.error_response(
                tool_name=tool_name,
                error=error_msg,
                request_id=request.request_id,
                execution_time_ms=_elapsed_ms(start_time),
            )

        # 获取 handler
        handler = self._registry.get_handler(tool_name)
        if handler is None:
            error_msg = f"工具 '{tool_name}' 没有对应的 handler"
            logger.error(error_msg)
            return ToolResponse.error_response(
                tool_name=tool_name,
                error=error_msg,
                request_id=request.request_id,
                execution_time_ms=_elapsed_ms(start_time),
            )

        # 执行 handler
        try:
            if definition.is_async:
                # 异步 handler: 创建事件循环执行
                logger.debug(f"[{tool_name}] 执行异步 handler")
                result = asyncio.run(self._execute_async(handler, request.arguments))
            else:
                logger.debug(f"[{tool_name}] 执行同步 handler")
                result = handler(**request.arguments)

            execution_time_ms = _elapsed_ms(start_time)
            logger.info(
                f"[{tool_name}] 执行成功 (耗时: {execution_time_ms:.2f}ms)"
            )
            return ToolResponse.success_response(
                tool_name=tool_name,
                data=result,
                request_id=request.request_id,
                execution_time_ms=execution_time_ms,
            )

        except Exception as e:
            execution_time_ms = _elapsed_ms(start_time)
            error_msg = f"执行异常: {type(e).__name__}: {e}"
            logger.exception(f"[{tool_name}] {error_msg}")
            return ToolResponse.error_response(
                tool_name=tool_name,
                error=f"{error_msg}\n\n详细 traceback:\n{traceback.format_exc()}",
                request_id=request.request_id,
                execution_time_ms=execution_time_ms,
            )

    async def route_async(self, request: Union[ToolRequest, str, Dict]) -> ToolResponse:
        """
        异步路由执行。

        如果 handler 是异步函数，直接 await；
        如果是同步函数，在线程池中执行避免阻塞。
        """
        import time
        start_time = time.perf_counter()

        # 统一转换为 ToolRequest
        if isinstance(request, str):
            try:
                request = ToolRequest.from_json(request)
            except ValueError as e:
                return ToolResponse.error_response(
                    tool_name="unknown",
                    error=f"请求解析失败: {e}",
                    execution_time_ms=_elapsed_ms(start_time),
                )
        elif isinstance(request, dict):
            try:
                request = ToolRequest(**request)
            except Exception as e:
                return ToolResponse.error_response(
                    tool_name=request.get("tool_name", "unknown"),
                    error=f"请求构造失败: {e}",
                    execution_time_ms=_elapsed_ms(start_time),
                )

        tool_name = request.tool_name
        definition = self._registry.get_definition(tool_name)
        if definition is None:
            available = self.list_tools()
            return ToolResponse.error_response(
                tool_name=tool_name,
                error=f"工具 '{tool_name}' 未注册. 可用: {available}",
                request_id=request.request_id,
                execution_time_ms=_elapsed_ms(start_time),
            )

        # 参数校验
        validation_errors = definition.validate_arguments(request.arguments)
        if validation_errors:
            error_msg = "参数校验失败:\n" + "\n".join(f"  - {e}" for e in validation_errors)
            return ToolResponse.error_response(
                tool_name=tool_name,
                error=error_msg,
                request_id=request.request_id,
                execution_time_ms=_elapsed_ms(start_time),
            )

        handler = self._registry.get_handler(tool_name)
        if handler is None:
            return ToolResponse.error_response(
                tool_name=tool_name,
                error=f"工具 '{tool_name}' 没有 handler",
                request_id=request.request_id,
                execution_time_ms=_elapsed_ms(start_time),
            )

        try:
            if definition.is_async:
                result = await handler(**request.arguments)
            else:
                # 同步 handler 在线程池中执行
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, lambda: handler(**request.arguments)
                )

            execution_time_ms = _elapsed_ms(start_time)
            return ToolResponse.success_response(
                tool_name=tool_name,
                data=result,
                request_id=request.request_id,
                execution_time_ms=execution_time_ms,
            )

        except Exception as e:
            execution_time_ms = _elapsed_ms(start_time)
            logger.exception(f"[{tool_name}] 执行异常")
            return ToolResponse.error_response(
                tool_name=tool_name,
                error=f"{type(e).__name__}: {e}\n{traceback.format_exc()}",
                request_id=request.request_id,
                execution_time_ms=execution_time_ms,
            )

    async def _execute_async(self, handler: Callable, arguments: Dict[str, Any]) -> Any:
        """执行异步 handler。"""
        return await handler(**arguments)

    # ── 批量路由 ──

    def route_batch(
        self,
        requests: List[Union[ToolRequest, str, Dict]],
    ) -> List[ToolResponse]:
        """批量同步路由。"""
        return [self.route(req) for req in requests]

    async def route_batch_async(
        self,
        requests: List[Union[ToolRequest, str, Dict]],
    ) -> List[ToolResponse]:
        """批量异步路由 (并行执行)。"""
        tasks = [self.route_async(req) for req in requests]
        return await asyncio.gather(*tasks)


# ──────────────────────────────────────────────
# 辅助函数
# ──────────────────────────────────────────────

def _elapsed_ms(start_time: float) -> float:
    """计算从 start_time 到当前的毫秒数。"""
    import time
    return (time.perf_counter() - start_time) * 1000
