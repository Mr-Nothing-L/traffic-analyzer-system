"""SFT Forge 数据模型(Pydantic)。

[文件说明]
作用:定义 RoundTrace(单轮/单次 VLM 调用轨迹)与 VideoForgeResult(单视频
  完整生成结果)。trace JSON 落盘供 Web 面板逐轮可视化(M2/M3)。
上游:sftforge/engine.py 产出;exporter.py 序列化。
下游:pydantic。
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class RoundTrace(BaseModel):
    """单次 VLM 调用轨迹(forward 一次 + 每个纠错事件各一次)。"""

    round_no: int = Field(description="0=正向盲推;1..max_rounds=纠错轮次")
    kind: str = Field(description="forward / correct_missing / review_extra")
    event_ids: List[int] = Field(default_factory=list)
    prompt: str = Field(default="", description="渲染后的 user prompt(system 固定从略)")
    response: str = Field(default="", description="VLM 原始响应文本")
    patched: bool = Field(default=False, description="本次调用是否产生了段落级修改")


class VideoForgeResult(BaseModel):
    """单视频 SFT label 生成结果。"""

    video: str = Field(description="视频绝对路径")
    gt: List[int] = Field(default_factory=list, description="文件名前缀真值(升序)")
    final_output: str = Field(default="", description="最终 <think>/<answer> 文本")
    rounds: List[RoundTrace] = Field(default_factory=list)
    match: bool = Field(default=False, description="最终结论集合与真值是否一致")
    skipped: bool = Field(default=False, description="09 正常/无法解析真值时跳过")
    reason: Optional[str] = Field(default=None, description="skip 或 quarantine 原因")
