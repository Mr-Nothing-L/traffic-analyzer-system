"""Backward-compatibility shim for the old single-module ``evidence_api``.

The implementation was split into the :mod:`traffic_analyzer.web.evidence`
package (locks.py / results.py / evidence_put.py / sft_api.py, aggregated by
the package ``__init__``). On import this module replaces itself in
``sys.modules`` with the package module, so every legacy reference —
``from traffic_analyzer.web import evidence_api``,
``traffic_analyzer.web.evidence_api.XXX`` and test monkeypatch paths such as
``traffic_analyzer.web.evidence_api._EVENT_CATEGORIES_YAML`` — resolves to
one and the same module object.

[文件说明]
作用:兼容跳板。原单文件 evidence_api.py 已拆分为 web/evidence/ 包;本
模块在导入时用包模块替换 sys.modules 中的自身,使
``from traffic_analyzer.web import evidence_api`` 与
``traffic_analyzer.web.evidence_api.XXX``(含测试 monkeypatch 路径)全部
落到同一模块对象上,既有引用/monkeypatch 路径无需修改。
上游:web/app.py、web/dashboard.py、web/jobs/routes.py 及测试的老路径
导入。
下游:web/evidence/ 包(全部实现,[文件说明] 见其 __init__.py)。
"""

from __future__ import annotations

import sys as _sys

from traffic_analyzer.web import evidence as _evidence_pkg

# 自我替换:此后 import 系统(from-import、getattr(父包, ...)、
# importlib.import_module、monkeypatch 的字符串路径解析)一律返回包模块。
_sys.modules[__name__] = _evidence_pkg
