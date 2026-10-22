"""定向跟踪包的共享异常。

[文件说明]
作用:TrackingFailure——视频元信息不可读等致命失败(端点侧捕获并转成
failed:true 响应)。独立小模块:sampling 与 windows 都要抛,定义在
任一方都会造成循环导入。
上游:toolserver/tracking/sampling.py、windows.py。
下游:toolserver/server.py(track_suspects 端点捕获)。
"""

from __future__ import annotations


class TrackingFailure(Exception):
    """视频元信息不可读等致命失败:端点侧捕获并转成 failed:true 响应。"""
