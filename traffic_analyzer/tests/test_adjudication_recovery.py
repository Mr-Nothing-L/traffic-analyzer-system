"""Unit tests for AdjudicationRecovery decision logic.

[文件说明]
作用:验证 adjudication_recovery.py 的纯决策逻辑,覆盖无缺失、全缺失、部分缺失、
异常候选混合以及达到最大重试次数等场景。
"""

from __future__ import annotations

import pytest

from traffic_analyzer.core.adjudication_recovery import AdjudicationRecovery
from traffic_analyzer.models.schemas import EventCandidate, EventInstance


class TestAdjudicationRecovery:
    @pytest.fixture
    def recovery(self) -> AdjudicationRecovery:
        return AdjudicationRecovery(max_retries=3)

    def test_no_missing_events_returns_complete(self, recovery: AdjudicationRecovery) -> None:
        expected = {1, 2, 3}
        present = {1, 2, 3}
        candidates = {
            1: EventCandidate(event_id=1, event_name="A", detected=True, summary="yes"),
            2: EventCandidate(event_id=2, event_name="B", detected=False, summary="no"),
            3: EventCandidate(event_id=3, event_name="C", detected=False, summary="no"),
        }

        decision = recovery.decide(expected, present, candidates, attempt=1)

        assert decision.action == "complete"
        assert decision.event_ids == []
        assert decision.missing_event_ids == []

    def test_all_missing_normal_candidates_returns_retry(self, recovery: AdjudicationRecovery) -> None:
        expected = {1, 2}
        present: set[int] = set()
        candidates = {
            1: EventCandidate(
                event_id=1,
                event_name="A",
                detected=True,
                summary="yes",
                raw_vlm_text="raw",
                instances=[EventInstance(event_id=1, event_name="A")],
            ),
            2: EventCandidate(
                event_id=2,
                event_name="B",
                detected=False,
                summary="no",
                raw_vlm_text="raw",
            ),
        }

        decision = recovery.decide(expected, present, candidates, attempt=1)

        assert decision.action == "retry"
        assert decision.missing_event_ids == [1, 2]

    def test_partial_missing_returns_retry_with_only_missing_ids(
        self, recovery: AdjudicationRecovery
    ) -> None:
        expected = {1, 2, 3}
        present = {1}
        candidates = {
            1: EventCandidate(
                event_id=1,
                event_name="A",
                detected=True,
                summary="yes",
                raw_vlm_text="raw",
                instances=[EventInstance(event_id=1, event_name="A")],
            ),
            2: EventCandidate(event_id=2, event_name="B", detected=False, summary="no", raw_vlm_text="raw"),
            3: EventCandidate(
                event_id=3,
                event_name="C",
                detected=True,
                summary="yes",
                raw_vlm_text="raw",
                instances=[EventInstance(event_id=3, event_name="C")],
            ),
        }

        decision = recovery.decide(expected, present, candidates, attempt=1)

        assert decision.action == "retry"
        assert decision.missing_event_ids == [2, 3]

    def test_abnormal_missing_candidate_returns_rerun(self, recovery: AdjudicationRecovery) -> None:
        expected = {1, 2}
        present: set[int] = set()
        candidates = {
            1: EventCandidate(
                event_id=1,
                event_name="A",
                detected=False,
                summary="ExpertAgent error: something failed",
            ),
            2: EventCandidate(
                event_id=2,
                event_name="B",
                detected=True,
                summary="yes",
                raw_vlm_text="raw",
                instances=[EventInstance(event_id=2, event_name="B")],
            ),
        }

        decision = recovery.decide(expected, present, candidates, attempt=1)

        assert decision.action == "rerun"
        assert decision.event_ids == [1]
        assert decision.missing_event_ids == []

    def test_mixed_abnormal_and_normal_missing_prefers_rerun(
        self, recovery: AdjudicationRecovery
    ) -> None:
        """When some missing candidates are abnormal, only the abnormal ones are
        selected for re-run; the normal missing candidates will be retried after
        the re-run refreshs the candidate table."""
        expected = {1, 2}
        present: set[int] = set()
        candidates = {
            1: EventCandidate(
                event_id=1,
                event_name="A",
                detected=False,
                summary="",
                raw_vlm_text="",
            ),
            2: EventCandidate(
                event_id=2,
                event_name="B",
                detected=True,
                summary="yes",
                raw_vlm_text="raw",
                instances=[EventInstance(event_id=2, event_name="B")],
            ),
        }

        decision = recovery.decide(expected, present, candidates, attempt=1)

        assert decision.action == "rerun"
        assert decision.event_ids == [1]

    def test_missing_candidate_with_none_value_is_abnormal(
        self, recovery: AdjudicationRecovery
    ) -> None:
        expected = {1}
        present: set[int] = set()
        candidates: dict[int, EventCandidate] = {}

        decision = recovery.decide(expected, present, candidates, attempt=1)

        assert decision.action == "rerun"
        assert decision.event_ids == [1]

    def test_reaches_max_retries_returns_fallback(
        self, recovery: AdjudicationRecovery
    ) -> None:
        expected = {1}
        present: set[int] = set()
        candidates = {
            1: EventCandidate(
                event_id=1,
                event_name="A",
                detected=True,
                summary="yes",
                raw_vlm_text="raw",
                instances=[EventInstance(event_id=1, event_name="A")],
            ),
        }

        decision = recovery.decide(expected, present, candidates, attempt=3)

        assert decision.action == "fallback"
        assert decision.missing_event_ids == [1]

    def test_abnormal_at_max_retries_still_reruns(self, recovery: AdjudicationRecovery) -> None:
        """Abnormal candidates are re-run even on the last attempt, giving the
        pipeline a final chance to recover before fallback."""
        expected = {1}
        present: set[int] = set()
        candidates = {
            1: EventCandidate(
                event_id=1,
                event_name="A",
                detected=False,
                summary="ExpertAgent error: timeout",
            ),
        }

        decision = recovery.decide(expected, present, candidates, attempt=3)

        assert decision.action == "rerun"
        assert decision.event_ids == [1]


class TestEventCandidateIsAbnormal:
    def test_expert_agent_error_summary_is_abnormal(self) -> None:
        candidate = EventCandidate(
            event_id=1,
            event_name="A",
            detected=False,
            summary="ExpertAgent error: timeout",
        )
        assert candidate.is_abnormal() is True

    def test_no_raw_response_is_abnormal(self) -> None:
        candidate = EventCandidate(
            event_id=1,
            event_name="A",
            detected=False,
            summary="no",
        )
        assert candidate.is_abnormal() is True

    def test_detected_without_summary_is_abnormal(self) -> None:
        candidate = EventCandidate(
            event_id=1,
            event_name="A",
            detected=True,
            summary="",
            raw_vlm_text="raw",
            instances=[EventInstance(event_id=1, event_name="A")],
        )
        assert candidate.is_abnormal() is True

    def test_detected_without_instances_is_abnormal(self) -> None:
        candidate = EventCandidate(
            event_id=1,
            event_name="A",
            detected=True,
            summary="yes",
            raw_vlm_text="raw",
        )
        assert candidate.is_abnormal() is True

    def test_normal_negative_candidate_is_not_abnormal(self) -> None:
        candidate = EventCandidate(
            event_id=1,
            event_name="A",
            detected=False,
            summary="no",
            raw_vlm_text="raw",
        )
        assert candidate.is_abnormal() is False

    def test_normal_positive_candidate_is_not_abnormal(self) -> None:
        candidate = EventCandidate(
            event_id=1,
            event_name="A",
            detected=True,
            summary="yes",
            raw_vlm_text="raw",
            instances=[EventInstance(event_id=1, event_name="A")],
        )
        assert candidate.is_abnormal() is False
