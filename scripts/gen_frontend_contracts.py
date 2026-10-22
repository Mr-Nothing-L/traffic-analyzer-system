#!/usr/bin/env python3
"""生成前端核心契约类型 frontend/src/api/contracts.gen.ts。

权威来源:traffic_analyzer/web/evidence_schema.py 的 Pydantic 模型
(SftSample / SftKeyframe)。脚本内省 model_fields 生成 TypeScript interface,
后端 schema 变更(增删字段/改类型)后重新生即可,前端类型自动跟随——
SFT 样本契约不再靠注释人工对齐(仿 gen_agent_event_contract 的守护模式)。

用法(项目根目录):
  python3 scripts/gen_frontend_contracts.py           # 生成(幂等)
  python3 scripts/gen_frontend_contracts.py --check   # 校验生成物与 schema 同步,漂移则退出码 1

修改 evidence_schema.py 后必须重新运行本脚本;
traffic_analyzer/tests/test_frontend_contracts.py 会用 --check 防漂移。
"""

from __future__ import annotations

import argparse
import inspect
import sys
from pathlib import Path
from typing import Any, Union, get_args, get_origin

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic import BaseModel  # noqa: E402

from traffic_analyzer.web.evidence_schema import SftKeyframe, SftSample  # noqa: E402

OUTPUT_TS = ROOT / "frontend" / "src" / "api" / "contracts.gen.ts"

HEADER = """\
/**
 * 生成物,禁止手工编辑(AGENTS.md「生成物是下游契约,不是手改文档」)。
 * 来源:scripts/gen_frontend_contracts.py ← traffic_analyzer/web/evidence_schema.py
 * 修改后端 schema 后运行 `python3 scripts/gen_frontend_contracts.py` 重新生成;
 * 同步由 traffic_analyzer/tests/test_frontend_contracts.py 守护(漂移即 fail)。
 */
"""


def _ts_type(ann: Any) -> str:
    """Python 类型注解 → TypeScript 类型(支持的封闭集合,遇新形状显式报错)。"""
    origin = get_origin(ann)
    if origin is None:
        if ann is Any:
            return "unknown"
        if ann is str:
            return "string"
        if ann in (int, float):
            return "number"
        if ann is bool:
            return "boolean"
        if inspect.isclass(ann) and issubclass(ann, BaseModel):
            return ann.__name__
        raise ValueError(f"不支持的类型注解: {ann!r}")
    if origin is Union:
        args = [a for a in get_args(ann) if a is not type(None)]
        if len(args) != 1:
            raise ValueError(f"仅支持 Optional[X],不支持其他 Union: {ann!r}")
        return f"{_ts_type(args[0])} | null"
    if origin is list:
        (item,) = get_args(ann)
        return f"{_ts_type(item)}[]"
    if origin is dict:
        key, val = get_args(ann)
        if key is not str:
            raise ValueError(f"仅支持 str 键的 dict: {ann!r}")
        return f"Record<string, {_ts_type(val)}>"
    raise ValueError(f"不支持的类型注解: {ann!r}")


def _render_model(model: type[BaseModel]) -> list[str]:
    """单个 Pydantic 模型 → interface 文本行(JSDoc 取类 docstring 首段)。"""
    doc = inspect.getdoc(model) or model.__name__
    first_para = doc.split("\n\n")[0].strip()
    lines = ["/**", f" * {first_para}", " */", f"export interface {model.__name__} {{"]
    for name, field in model.model_fields.items():
        optional = "" if field.is_required() else "?"
        lines.append(f"  {name}{optional}: {_ts_type(field.annotation)}")
    lines.append("}")
    return lines


def render() -> str:
    body: list[str] = []
    for model in (SftKeyframe, SftSample):  # 依赖方在前(字段引用后无需前向声明)
        if body:
            body.append("")
        body.extend(_render_model(model))
    return HEADER + "\n" + "\n".join(body) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="校验 contracts.gen.ts 与 evidence_schema.py 同步(漂移退出码 1)",
    )
    args = parser.parse_args()

    expected = render()

    if not args.check:
        OUTPUT_TS.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_TS.write_text(expected, encoding="utf-8")
        print(f"[gen_frontend_contracts] 已写入 {OUTPUT_TS.relative_to(ROOT)}")
        return 0

    if not OUTPUT_TS.is_file():
        print(
            f"[gen_frontend_contracts] 缺少 {OUTPUT_TS.relative_to(ROOT)};"
            "请运行 python3 scripts/gen_frontend_contracts.py 生成"
        )
        return 1
    actual = OUTPUT_TS.read_text(encoding="utf-8")
    if actual != expected:
        print(
            "[gen_frontend_contracts] 生成物与 schema 不同步;"
            "请运行 python3 scripts/gen_frontend_contracts.py 重新生成"
        )
        return 1
    print(f"[gen_frontend_contracts] {OUTPUT_TS.relative_to(ROOT)} 与 schema 同步")
    return 0


if __name__ == "__main__":
    sys.exit(main())
