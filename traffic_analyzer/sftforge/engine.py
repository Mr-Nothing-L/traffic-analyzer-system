"""SFT Forge 编排引擎。

[文件说明]
作用:SftForgeEngine.run(video_path) 编排多轮纠错流程——forward 盲推(不注入
  真值)→ 与文件名真值 diff → 漏检事件逐个 correct_missing 定向重推、误检事件
  逐个 review_extra 复核剔除 → merger 段落级合并;重复至多 max_rounds(默认 2)
  轮,仍不一致则 match=False 并记 quarantine 原因。VLM 调用经注入的 caller
  (签名 caller(system_prompt, user_prompt, video_path) -> str),测试可 fake。
上游:scripts/sft_forge.py;后续 M2 web 层。
下游:sftforge/{gt,parser,merger,models}.py、core/vlm_engine.py 的
  render_prompt 静态方法(Jinja2 渲染复用)、config/prompts/sft_forge.yaml。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from traffic_analyzer.core.vlm_engine import VLMInferenceEngine
from traffic_analyzer.core.vlm_exceptions import (
    AllProvidersExhaustedError,
    FatalAPIError,
)
from traffic_analyzer.evaluation import NORMAL_EVENT_ID, event_names
from traffic_analyzer.models.schemas import EventCategory, PromptTemplate
from traffic_analyzer.sftforge.gt import diff_predictions, gt_for_video
from traffic_analyzer.sftforge.merger import merge_output
from traffic_analyzer.sftforge.models import CallerResult, RoundTrace, VideoForgeResult
from traffic_analyzer.sftforge.parser import (
    DEFAULT_EVENT_ORDER,
    OutputParseError,
    parse_correction_json,
    parse_event_segment,
    parse_forward_json,
    parse_output,
    parse_review_verdict,
)

logger = logging.getLogger(__name__)

TEMPLATE_FORWARD = "sft_forge_forward"
TEMPLATE_CORRECT_MISSING = "sft_forge_correct_missing"
TEMPLATE_REVIEW_EXTRA = "sft_forge_review_extra"

# caller 协议:渲染后的 system/user prompt + 视频路径 → 响应文本。
VideoCaller = Callable[[str, str, Union[str, Path]], str]

# 流式增量回调:(info, field, text);info 含 video/round_no/kind,
# field ∈ reasoning/response/reset(reset 时前端清空该轮流式缓冲)。
DeltaHook = Callable[[Dict[str, object], str, str], None]


class SftForgeEngine:
    """单视频多轮纠错 SFT label 生成引擎(线程安全:状态全部在栈上)。"""

    def __init__(
        self,
        caller: VideoCaller,
        templates: Mapping[str, PromptTemplate],
        categories: Sequence[EventCategory],
        max_rounds: int = 2,
        names: Optional[Dict[int, str]] = None,
        on_delta: Optional[DeltaHook] = None,
        draft_provider: Optional[Callable[..., Any]] = None,
        on_draft_step: Optional[Callable[[Dict[str, object]], None]] = None,
    ) -> None:
        missing = {
            tid for tid in (TEMPLATE_FORWARD, TEMPLATE_CORRECT_MISSING, TEMPLATE_REVIEW_EXTRA)
            if tid not in templates
        }
        if missing:
            raise KeyError(f"缺少 prompt 模板: {sorted(missing)}")
        self._caller = caller
        self._templates = templates
        self._max_rounds = max(0, max_rounds)
        self._names = names if names is not None else event_names()
        self._on_delta = on_delta
        # 初稿来源:None=单次 forward prompt(快速);注入时=批量推理链路
        # (sftforge/draft.py PipelineDraftProvider),初稿 prompt 与生产同源。
        self._draft_provider = draft_provider
        self._on_draft_step = on_draft_step
        active = sorted(
            (c for c in categories if c.is_active), key=lambda c: c.event_id
        )
        self._event_order: Tuple[int, ...] = (
            tuple(c.event_id for c in active) or DEFAULT_EVENT_ORDER
        )
        self._definitions: Dict[int, str] = {
            c.event_id: (c.definition or c.description or "").strip() for c in active
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, video_path: Union[str, Path]) -> VideoForgeResult:
        """对单个视频执行 forward→diff→定向纠错→merge 全流程。"""
        path = Path(video_path)
        result = VideoForgeResult(video=str(path))

        gt = gt_for_video(path.name, self._names)
        if gt is None:
            result.skipped = True
            result.reason = "文件名无法解析真值或09与其他事件混标,跳过"
            logger.info("[sftforge] SKIP | video=%s", path.name)
            return result
        result.gt = sorted(gt)
        normal_ground_truth = gt == {NORMAL_EVENT_ID}

        # ---- Round 0: 初稿(draft_provider 给出时走批量推理链路,否则单次
        # forward prompt 盲推;两者均不注入真值) ----
        forward_resp = self._forward(result, path)
        if forward_resp is None:
            result.reason = (
                "draft_failed" if self._draft_provider is not None else "forward_call_failed"
            )
            return result
        # 主路径为 JSON 响应(端点可靠输出);文本 <think>/<answer> 格式保留为回退。
        try:
            current = parse_forward_json(forward_resp, self._names)
        except OutputParseError:
            try:
                current = parse_output(forward_resp, self._names)
            except OutputParseError as exc:
                result.reason = f"forward_parse_failed: {exc}"
                logger.warning(
                    "[sftforge] FORWARD_PARSE_ERROR | video=%s | %s", path.name, exc
                )
                return result
        # 最终输出始终由 merger 代码重建(模型可能省略字面 <think>/<answer>
        # 标签,标签是机械结构,代码包裹保证训练格式逐字节一致;段落正文保持
        # 模型原文,思维链真实性不受影响)。
        final_output = merge_output(
            current, {}, current.classes, self._names, self._event_order
        )
        # R0 一致性:区分「直接一致」与「纠错后一致」(初稿失败已在上方返回,
        # 保持 None)。
        r0_missing, r0_extra = diff_predictions(set(current.classes), gt)
        result.r0_match = not r0_missing and not r0_extra

        # ---- Round 1..max_rounds: 双向纠错 ----
        for round_no in range(1, self._max_rounds + 1):
            missing, extra = diff_predictions(set(current.classes), gt)
            if not missing and not extra:
                break
            logger.info(
                "[sftforge] DIFF | video=%s round=%d missing=%s extra=%s",
                path.name, round_no, missing, extra,
            )
            patches: Dict[int, Optional[str]] = {}
            added, removed = set(), set()

            for eid in missing:
                segment = self._correct_missing(result, round_no, eid, path)
                if segment:
                    patches[eid] = segment
                    added.add(eid)

            for eid in extra:
                negative_segment = self._review_extra(
                    result, round_no, eid, current, path, normal_ground_truth
                )
                if negative_segment is not None:
                    # 误报:以否定段落替换原段落(保持 10 事件段落完整),并从结论剔除
                    patches[eid] = negative_segment
                    removed.add(eid)

            if not patches:
                logger.warning(
                    "[sftforge] NO_PROGRESS | video=%s round=%d 补丁为空,终止纠错",
                    path.name, round_no,
                )
                break

            final_classes = (set(current.classes) | added) - removed
            final_output = merge_output(
                current, patches, sorted(final_classes),
                self._names, self._event_order,
            )
            current = parse_output(final_output, self._names)

        # ---- 终判 ----
        missing, extra = diff_predictions(set(current.classes), gt)
        result.final_output = final_output
        if not missing and not extra:
            result.match = True
            logger.info("[sftforge] MATCH | video=%s gt=%s", path.name, result.gt)
        else:
            result.match = False
            result.reason = f"final_mismatch: missing={missing} extra={extra}"
            logger.warning(
                "[sftforge] QUARANTINE | video=%s | %s", path.name, result.reason
            )
        return result

    # ------------------------------------------------------------------
    # Correction rounds
    # ------------------------------------------------------------------

    def _correct_missing(
        self, result: VideoForgeResult, round_no: int, event_id: int, path: Path
    ) -> str:
        """漏检补段落:告知事件确实存在,要求重推该事件段落;返回段落正文。"""
        response = self._call(
            result,
            round_no=round_no,
            kind="correct_missing",
            event_ids=[event_id],
            template_id=TEMPLATE_CORRECT_MISSING,
            context_vars={
                "event_id": event_id,
                "event_name": self._names.get(event_id, f"event_{event_id}"),
                "event_definition": self._definitions.get(event_id, ""),
            },
            video_path=path,
        )
        if response is None:
            return ""
        # 主路径 JSON {"thinking": ...};回退文本段落提取。
        data = parse_correction_json(response)
        if data is not None:
            segment = data["thinking"]
        else:
            segment = parse_event_segment(response, event_id, self._names)
        if segment:
            result.rounds[-1].patched = True
        else:
            logger.warning(
                "[sftforge] CORRECT_MISSING_UNPARSEABLE | video=%s event=%d",
                path.name, event_id,
            )
        return segment

    def _review_extra(
        self,
        result: VideoForgeResult,
        round_no: int,
        event_id: int,
        current,
        path: Path,
        normal_ground_truth: bool = False,
    ) -> Optional[str]:
        """误报复核:确认误报时返回否定段落(替换原段落并从结论剔除),否则 None。"""
        response = self._call(
            result,
            round_no=round_no,
            kind="review_extra",
            event_ids=[event_id],
            template_id=TEMPLATE_REVIEW_EXTRA,
            context_vars={
                "event_id": event_id,
                "event_name": self._names.get(event_id, f"event_{event_id}"),
                "event_definition": self._definitions.get(event_id, ""),
                "first_round_segment": current.paragraphs.get(event_id, ""),
                "normal_ground_truth": normal_ground_truth,
            },
            video_path=path,
        )
        if response is None:
            return None
        # 主路径 JSON {"verdict": "confirm"|"false_positive", "thinking": ...};
        # 回退文本「复核结论」标记解析。标记缺失或确认时都保守保留,不剔除。
        data = parse_correction_json(response)
        if data is not None and data["verdict"] in ("confirm", "false_positive"):
            drop = data["verdict"] == "false_positive"
            segment = data["thinking"] if drop else ""
        else:
            verdict = parse_review_verdict(response)
            drop = verdict is False
            segment = (
                parse_event_segment(response, event_id, self._names) if drop else ""
            )
        if not drop:
            return None
        result.rounds[-1].patched = True
        if not segment:
            name = self._names.get(event_id, f"event_{event_id}")
            segment = f"经复核，画面内容不满足{name}的判定条件，原检出为误报。"
        return segment

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _forward(self, result: VideoForgeResult, video_path: Path) -> Optional[str]:
        """Round 0 初稿:draft_provider 注入时走批量推理链路,否则单次 forward
        prompt 盲推。返回初稿文本(规范 <think>/<answer> 或 forward JSON),
        失败记 trace 返回 None。"""
        if self._draft_provider is None:
            return self._call(
                result,
                round_no=0,
                kind="forward",
                event_ids=[],
                template_id=TEMPLATE_FORWARD,
                context_vars={"event_definitions": self._event_definitions_text()},
                video_path=video_path,
            )
        trace = RoundTrace(
            round_no=0,
            kind="forward",
            prompt="(批量推理链路: 抽帧预处理→事件专家→裁决→SFT改写)",
        )
        result.rounds.append(trace)
        on_step = None
        if self._on_draft_step is not None:
            on_step = lambda payload: self._on_draft_step(  # noqa: E731
                {**payload, "video": str(video_path)}
            )
        try:
            draft = self._draft_provider(video_path, on_step=on_step)
        except Exception as exc:  # noqa: BLE001
            trace.response = f"[ERROR] {type(exc).__name__}: {exc}"
            logger.error(
                "[sftforge] DRAFT_ERROR | video=%s | %s", video_path.name, exc
            )
            return None
        trace.response = draft.output
        return draft.output

    def _call(
        self,
        result: VideoForgeResult,
        round_no: int,
        kind: str,
        event_ids: List[int],
        template_id: str,
        context_vars: Dict[str, object],
        video_path: Path,
    ) -> Optional[str]:
        """渲染模板并调用注入的 caller;失败记 trace 返回 None(FatalAPIError 上抛)。"""
        system, user = VLMInferenceEngine.render_prompt(
            self._templates[template_id], context_vars
        )
        trace = RoundTrace(
            round_no=round_no, kind=kind, event_ids=list(event_ids), prompt=user
        )
        result.rounds.append(trace)
        try:
            if self._on_delta is not None:
                info: Dict[str, object] = {
                    "video": str(video_path),
                    "round_no": round_no,
                    "kind": kind,
                }
                raw = self._caller(
                    system,
                    user,
                    video_path,
                    on_delta=lambda field, text: self._on_delta(info, field, text),
                )
            else:
                raw = self._caller(system, user, video_path)
        except (FatalAPIError, AllProvidersExhaustedError):
            # 全停型错误(API 不可用)必须上抛终止批处理,不能让整批被静默
            # quarantine 并被断点续跑永久跳过(AllProvidersExhaustedError
            # 并非 FatalAPIError 子类,需单独捕获)。
            raise
        except Exception as exc:  # noqa: BLE001
            trace.response = f"[ERROR] {type(exc).__name__}: {exc}"
            logger.error(
                "[sftforge] CALL_ERROR | video=%s kind=%s events=%s | %s",
                video_path.name, kind, event_ids, exc,
            )
            return None
        # caller 协议:CallerResult(text, reasoning);兼容裸 str 返回的旧 caller
        if isinstance(raw, CallerResult):
            response, trace.reasoning = raw.text, raw.reasoning
        else:
            response = raw
        trace.response = response
        return response

    def _event_definitions_text(self) -> str:
        """forward 模板用的事件定义清单(按 event_order,不含真值信息)。"""
        lines = []
        for eid in self._event_order:
            name = self._names.get(eid, f"event_{eid}")
            definition = self._definitions.get(eid, "")
            lines.append(f"### class{eid} {name}\n{definition}")
        return "\n\n".join(lines)
