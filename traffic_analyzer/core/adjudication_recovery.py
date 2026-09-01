"""Adjudication recovery decision logic.

[文件说明]
作用:将裁决层 VLM 返回缺失事件时的补偿策略决策独立为可单测的纯决策模块。
输入为期望事件 ID 集合、本轮返回的 ID 集合、专家候选表、当前 attempt 与最大重试次数;
输出为下一步动作(完成 / 重跑异常专家 / 再次请求 VLM / 降级回填)及附带的事件 ID 列表。
不包含任何 VLM 调用或专家执行逻辑——这些仍由 AdjudicationStep 驱动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from traffic_analyzer.models.schemas import EventCandidate


@dataclass
class RecoveryDecision:
    """Decision produced by AdjudicationRecovery for one attempt."""

    action: str  # "complete", "rerun", "retry", "fallback"
    event_ids: List[int] = field(default_factory=list)
    missing_event_ids: List[int] = field(default_factory=list)


class AdjudicationRecovery:
    """Pure decision engine for adjudication missing-event recovery.

    Implements the ADR-0003 compensation semantics:
    1. If no events are missing, the adjudication response is complete.
    2. If any missing candidate is abnormal, re-run those expert agents.
    3. Otherwise, retry the adjudication VLM call (up to max_retries).
    4. After exhausting retries, fall back to filling from candidates.
    """

    def __init__(self, max_retries: int = 5) -> None:
        self.max_retries = max_retries

    def decide(
        self,
        expected_event_ids: Set[int],
        present_event_ids: Set[int],
        candidates: Dict[int, EventCandidate],
        attempt: int,
    ) -> RecoveryDecision:
        """Decide what to do after an adjudication VLM response.

        Args:
            expected_event_ids: Event IDs that should have been adjudicated.
            present_event_ids: Event IDs actually present in the VLM response.
            candidates: Current expert candidate map (may be updated by re-runs).
            attempt: 1-based attempt number for this VLM call.

        Returns:
            RecoveryDecision describing the next recovery action.
        """
        missing_event_ids = sorted(expected_event_ids - present_event_ids)

        if not missing_event_ids:
            return RecoveryDecision(action="complete")

        abnormal_event_ids = [
            eid
            for eid in missing_event_ids
            if self._is_abnormal_candidate(candidates.get(eid))
        ]

        if abnormal_event_ids:
            return RecoveryDecision(
                action="rerun",
                event_ids=sorted(abnormal_event_ids),
            )

        if attempt >= self.max_retries:
            return RecoveryDecision(
                action="fallback",
                missing_event_ids=missing_event_ids,
            )

        return RecoveryDecision(
            action="retry",
            missing_event_ids=missing_event_ids,
        )

    @staticmethod
    def _is_abnormal_candidate(candidate: Optional[EventCandidate]) -> bool:
        """Return True if the candidate is missing or looks malformed."""
        if candidate is None:
            return True
        return candidate.is_abnormal()
