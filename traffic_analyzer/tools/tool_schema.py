"""
工具定义层 (Tool Definition Layer)

定义工具的元数据、参数约束、返回值结构。
供模型理解可用工具，供路由层校验和执行。

所有类使用 Pydantic v2 BaseModel 保证类型安全和序列化。
"""

from __future__ import annotations

import logging
import re
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Type, Union

from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 参数类型枚举
# ──────────────────────────────────────────────

class ParameterType(str, Enum):
    """工具参数支持的类型。"""
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"
    ENUM = "enum"


# ──────────────────────────────────────────────
# 约束规则
# ──────────────────────────────────────────────

class ToolConstraint(BaseModel):
    """
    参数约束规则。

    支持以下约束:
    - min_value / max_value: 数值范围
    - min_length / max_length: 字符串/数组长度
    - pattern: 正则匹配 (字符串)
    - enum_values: 枚举值列表
    - items_type: 数组元素类型
    - required: 是否必填 (默认 True)
    """
    min_value: Optional[Union[int, float]] = None
    max_value: Optional[Union[int, float]] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    pattern: Optional[str] = None
    enum_values: Optional[List[Any]] = None
    items_type: Optional[ParameterType] = None
    required: bool = True

    @field_validator("pattern")
    @classmethod
    def _validate_pattern(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            try:
                re.compile(v)
            except re.error as e:
                raise ValueError(f"无效的正则表达式: {v}, 错误: {e}")
        return v

    @field_validator("enum_values")
    @classmethod
    def _validate_enum(cls, v: Optional[List[Any]]) -> Optional[List[Any]]:
        if v is not None and len(v) == 0:
            raise ValueError("enum_values 不能为空列表")
        return v

    @model_validator(mode="after")
    def _check_range_consistency(self) -> "ToolConstraint":
        if self.min_value is not None and self.max_value is not None:
            if self.min_value > self.max_value:
                raise ValueError(
                    f"min_value ({self.min_value}) 不能大于 max_value ({self.max_value})"
                )
        if self.min_length is not None and self.max_length is not None:
            if self.min_length > self.max_length:
                raise ValueError(
                    f"min_length ({self.min_length}) 不能大于 max_length ({self.max_length})"
                )
        return self

    def validate_value(self, name: str, value: Any) -> List[str]:
        """
        校验单个值是否符合约束。
        返回错误列表 (空列表表示通过)。
        """
        errors: List[str] = []

        # 必填检查
        if self.required and value is None:
            errors.append(f"参数 '{name}' 为必填项")
            return errors

        if value is None:
            return errors

        # 类型检查
        if self.enum_values is not None and value not in self.enum_values:
            errors.append(
                f"参数 '{name}' 的值 '{value}' 不在枚举范围内: {self.enum_values}"
            )

        # 数值范围
        if isinstance(value, (int, float)):
            if self.min_value is not None and value < self.min_value:
                errors.append(
                    f"参数 '{name}' 的值 {value} 小于最小值 {self.min_value}"
                )
            if self.max_value is not None and value > self.max_value:
                errors.append(
                    f"参数 '{name}' 的值 {value} 大于最大值 {self.max_value}"
                )

        # 长度约束 (字符串/列表)
        if isinstance(value, (str, list)):
            length = len(value)
            if self.min_length is not None and length < self.min_length:
                errors.append(
                    f"参数 '{name}' 的长度 {length} 小于最小长度 {self.min_length}"
                )
            if self.max_length is not None and length > self.max_length:
                errors.append(
                    f"参数 '{name}' 的长度 {length} 大于最大长度 {self.max_length}"
                )

        # 正则匹配
        if self.pattern is not None and isinstance(value, str):
            if not re.match(self.pattern, value):
                errors.append(
                    f"参数 '{name}' 的值 '{value}' 不匹配正则模式: {self.pattern}"
                )

        # 数组元素类型
        if self.items_type is not None and isinstance(value, list):
            type_map = {
                ParameterType.STRING: str,
                ParameterType.INTEGER: int,
                ParameterType.FLOAT: (int, float),
                ParameterType.BOOLEAN: bool,
            }
            expected = type_map.get(self.items_type)
            if expected:
                for i, item in enumerate(value):
                    if not isinstance(item, expected):
                        errors.append(
                            f"参数 '{name}' 的第 {i} 个元素类型错误: "
                            f"期望 {self.items_type.value}, 得到 {type(item).__name__}"
                        )

        return errors


# ──────────────────────────────────────────────
# 工具参数定义
# ──────────────────────────────────────────────

class ToolParameter(BaseModel):
    """
    单个工具参数的定义。

    Attributes:
        name: 参数名 (英文, 用于匹配)
        type: 参数类型
        description: 给模型看的参数说明
        constraints: 约束规则
        default: 默认值 (可选)
    """
    name: str = Field(..., min_length=1, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    type: ParameterType
    description: str = Field(..., min_length=1)
    constraints: ToolConstraint = Field(default_factory=ToolConstraint)
    default: Any = None

    def validate_value(self, value: Any) -> List[str]:
        """校验参数值。"""
        return self.constraints.validate_value(self.name, value)

    def to_json_schema(self) -> Dict[str, Any]:
        """转换为 JSON Schema 格式 (供模型使用)。"""
        schema: Dict[str, Any] = {
            "type": self.type.value,
            "description": self.description,
        }
        c = self.constraints
        if c.min_value is not None:
            schema["minimum"] = c.min_value
        if c.max_value is not None:
            schema["maximum"] = c.max_value
        if c.min_length is not None:
            schema["minLength"] = c.min_length
        if c.max_length is not None:
            schema["maxLength"] = c.max_length
        if c.pattern is not None:
            schema["pattern"] = c.pattern
        if c.enum_values is not None:
            schema["enum"] = c.enum_values
        if self.default is not None:
            schema["default"] = self.default
        if not c.required:
            schema["nullable"] = True
        return schema


# ──────────────────────────────────────────────
# 工具返回值定义
# ──────────────────────────────────────────────

class ToolReturn(BaseModel):
    """
    工具返回值的定义。

    Attributes:
        type: 返回值类型
        description: 返回值说明
        schema: 详细的 JSON Schema (可选)
    """
    type: ParameterType
    description: str = Field(..., min_length=1)
    json_schema: Optional[Dict[str, Any]] = Field(default=None, alias="schema")


# ──────────────────────────────────────────────
# 工具定义
# ──────────────────────────────────────────────

class ToolDefinition(BaseModel):
    """
    工具的完整定义。

    包含名称、描述、参数列表、返回值、以及执行处理器。
    这是模型理解工具的权威来源。

    Attributes:
        name: 工具名称 (英文, 唯一标识)
        description: 给模型看的工具功能说明
        parameters: 参数列表
        returns: 返回值定义
        examples: 使用示例 (可选, 帮助模型理解)
        is_async: 处理器是否为异步函数
    """
    name: str = Field(..., min_length=1, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    description: str = Field(..., min_length=10)
    parameters: List[ToolParameter] = Field(default_factory=list)
    returns: ToolReturn
    examples: Optional[List[Dict[str, Any]]] = None
    is_async: bool = False

    @field_validator("parameters")
    @classmethod
    def _check_unique_param_names(cls, v: List[ToolParameter]) -> List[ToolParameter]:
        names = [p.name for p in v]
        if len(names) != len(set(names)):
            from collections import Counter
            dupes = [n for n, c in Counter(names).items() if c > 1]
            raise ValueError(f"参数名重复: {dupes}")
        return v

    def validate_arguments(self, arguments: Dict[str, Any]) -> List[str]:
        """
        校验参数字典是否符合工具定义。
        返回所有错误 (空列表表示通过)。
        """
        errors: List[str] = []
        provided_names = set(arguments.keys())
        required_names = {
            p.name for p in self.parameters if p.constraints.required
        }

        # 检查必填参数缺失
        missing = required_names - provided_names
        for name in missing:
            errors.append(f"缺少必填参数: '{name}'")

        # 检查未知参数
        known_names = {p.name for p in self.parameters}
        unknown = provided_names - known_names
        for name in unknown:
            errors.append(f"未知参数: '{name}'")

        # 校验每个提供的参数
        param_map = {p.name: p for p in self.parameters}
        for name, value in arguments.items():
            if name in param_map:
                param_errors = param_map[name].validate_value(value)
                errors.extend(param_errors)

        return errors

    def to_json_schema(self) -> Dict[str, Any]:
        """
        生成 JSON Schema 格式的工具定义 (供模型使用)。
        遵循 OpenAI Function Calling 格式。
        """
        properties: Dict[str, Any] = {}
        required: List[str] = []

        for param in self.parameters:
            properties[param.name] = param.to_json_schema()
            if param.constraints.required:
                required.append(param.name)

        schema: Dict[str, Any] = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

        if self.examples:
            schema["function"]["examples"] = self.examples

        return schema

    def to_markdown(self) -> str:
        """生成 Markdown 格式的工具说明 (供人类阅读)。"""
        lines = [
            f"### `{self.name}`",
            "",
            self.description,
            "",
            "**参数:**",
        ]
        for param in self.parameters:
            req = "必填" if param.constraints.required else "可选"
            default = f", 默认: {param.default}" if param.default is not None else ""
            lines.append(f"- `{param.name}` ({param.type.value}, {req}{default}): {param.description}")
        lines.extend([
            "",
            f"**返回:** {self.returns.type.value} — {self.returns.description}",
        ])
        if self.examples:
            lines.extend(["", "**示例:**", "```json", str(self.examples), "```"])
        return "\n".join(lines)


# ──────────────────────────────────────────────
# 工具注册表 (供路由层使用)
# ──────────────────────────────────────────────

class ToolRegistry:
    """
    工具注册表。

    管理 ToolDefinition 到 handler 函数的映射。
    线程安全 (假设注册在启动时完成，运行时只读)。
    """

    def __init__(self) -> None:
        self._definitions: Dict[str, ToolDefinition] = {}
        self._handlers: Dict[str, Callable] = {}
        logger.debug("ToolRegistry 初始化完成")

    def register(
        self,
        definition: ToolDefinition,
        handler: Callable,
    ) -> None:
        """
        注册工具。

        Args:
            definition: 工具定义
            handler: 执行函数 (同步或异步)

        Raises:
            ValueError: 工具名已存在
        """
        if definition.name in self._definitions:
            raise ValueError(f"工具 '{definition.name}' 已注册")

        self._definitions[definition.name] = definition
        self._handlers[definition.name] = handler
        logger.info(f"工具注册成功: {definition.name} (参数: {len(definition.parameters)}个)")

    def unregister(self, name: str) -> None:
        """注销工具。"""
        if name not in self._definitions:
            raise ValueError(f"工具 '{name}' 未注册")
        del self._definitions[name]
        del self._handlers[name]
        logger.info(f"工具注销: {name}")

    def get_definition(self, name: str) -> Optional[ToolDefinition]:
        """获取工具定义。"""
        return self._definitions.get(name)

    def get_handler(self, name: str) -> Optional[Callable]:
        """获取工具处理器。"""
        return self._handlers.get(name)

    def list_tools(self) -> List[str]:
        """列出所有已注册工具名称。"""
        return list(self._definitions.keys())

    def get_all_schemas(self) -> List[Dict[str, Any]]:
        """获取所有工具的 JSON Schema。"""
        return [d.to_json_schema() for d in self._definitions.values()]

    def get_all_markdown(self) -> str:
        """获取所有工具的 Markdown 说明。"""
        sections = [d.to_markdown() for d in self._definitions.values()]
        return "\n\n---\n\n".join(sections)

    def __contains__(self, name: str) -> bool:
        return name in self._definitions

    def __len__(self) -> int:
        return len(self._definitions)
