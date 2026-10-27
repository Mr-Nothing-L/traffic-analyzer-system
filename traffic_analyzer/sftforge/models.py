"""SFT Forge 数据模型(Pydantic)。

[文件说明]
作用:定义 RoundTrace(单轮/单次 VLM 调用轨迹)与 VideoForgeResult(单视频
  完整生成结果)。trace JSON 落盘供 Web 面板逐轮可视化(M2/M3)。
上游:sftforge/engine.py 产出;exporter.py 序列化。
下游:pydantic。
"""

from __future__ import annotations

from typing import List, NamedTuple, Optional

from pydantic import BaseModel, Field


class CallerResult(NamedTuple):
    """caller 返回协议:响应文本 + 隐藏 reasoning 通道文本。

    engine 同时兼容裸 str 返回(旧 fake/简单 caller),见 engine._call。
    """

    text: str
    reasoning: str = ""


class RoundTrace(BaseModel):
    """单次 VLM 调用轨迹(forward 一次 + 每个纠错事件各一次)。"""

    round_no: int = Field(description="0=正向盲推;1..max_rounds=纠错轮次")
    kind: str = Field(description="forward / correct_missing / review_extra")
    event_ids: List[int] = Field(default_factory=list)
    prompt: str = Field(default="", description="渲染后的 user prompt(system 固定从略)")
    response: str = Field(default="", description="VLM 原始响应文本")
    reasoning: str = Field(
        default="", description="隐藏 reasoning 通道文本(thinking 开启且端点返回时)"
    )
    patched: bool = Field(default=False, description="本次调用是否产生了段落级修改")


class VideoForgeResult(BaseModel):
    """单视频 SFT label 生成结果。"""

    video: str = Field(description="视频绝对路径")
    gt: List[int] = Field(default_factory=list, description="文件名前缀真值(升序)")
    final_output: str = Field(default="", description="最终 <think>/<answer> 文本")
    rounds: List[RoundTrace] = Field(default_factory=list)
    match: bool = Field(default=False, description="最终结论集合与真值是否一致")
    r0_match: Optional[bool] = Field(
        default=None,
        description="首轮(R0)初稿与真值是否一致:True=直接一致,False=纠错后一致或终不一致;初稿失败/跳过为 None",
    )
    skipped: bool = Field(default=False, description="无法解析真值或 09 与其他事件混标时跳过")
    reason: Optional[str] = Field(default=None, description="skip 或 quarantine 原因")
