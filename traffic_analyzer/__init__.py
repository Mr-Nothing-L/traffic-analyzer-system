"""
Traffic Analyzer - LLM/VLM based traffic event detection framework.

A configuration-driven, extensible framework for analyzing traffic events
in surveillance videos using Large Vision-Language Models.

[文件说明]
作用:包顶层标识文件,仅定义 __version__ 与 __author__;版本号供 CLI --version 展示。
上游:traffic_analyzer/cli.py(读取 __version__);所有 import traffic_analyzer 的调用方。
下游:无(纯元数据,不导入任何模块)。
"""

__version__ = "5.0.0"
__author__ = "Traffic Analyzer Team"
