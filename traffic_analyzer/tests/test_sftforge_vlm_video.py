"""sftforge.vlm_video 错误处理单测(fake client,无网络)。

覆盖 review R1 两项 P2:
- P2-1:末位 provider 的 failover-trigger 错误(401 认证)与重试耗尽均升级为
  AllProvidersExhaustedError;非末位 provider 的同类错误触发 failover;
- P2-2:空响应在 _execute_once 内做 1 次有界即时重试,仍空才抛 RuntimeError。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import List

import httpx
import openai
import pytest

from traffic_analyzer.core.vlm_exceptions import AllProvidersExhaustedError
from traffic_analyzer.models.schemas import LLMProviderConfig
from traffic_analyzer.sftforge.vlm_video import VideoVLMCaller


def _provider(**overrides) -> LLMProviderConfig:
    base = dict(
        provider="aliyun",
        api_key="test-key",
        base_url="http://localhost:1/v1",
        model="test-model",
        max_retries=1,
        timeout=1.0,
    )
    base.update(overrides)
    return LLMProviderConfig(**base)


class _FakeCompletions:
    """按队列返回响应或抛出异常。"""

    def __init__(self, outcomes: List[object]) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        assert self._outcomes, "unexpected extra call"
        item = self._outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _fake_client(outcomes: List[object]) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeCompletions(outcomes))
    )


def _response(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


def _empty_response() -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=""))]
    )


def _auth_error() -> openai.AuthenticationError:
    request = httpx.Request("POST", "http://localhost:1/v1/chat/completions")
    return openai.AuthenticationError(
        "invalid api key", response=httpx.Response(401, request=request), body=None
    )


def _timeout_error() -> openai.APITimeoutError:
    request = httpx.Request("POST", "http://localhost:1/v1/chat/completions")
    return openai.APITimeoutError(request=request)


def _caller(providers: List[LLMProviderConfig], clients: List[SimpleNamespace]) -> VideoVLMCaller:
    caller = VideoVLMCaller(providers, disk_cache_path=None)
    caller._clients = clients
    return caller


@pytest.fixture()
def video(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"fake-video-bytes")
    return path


class TestFailoverEscalation:
    """P2-1:全停型错误必须升级为 AllProvidersExhaustedError 终止批处理。"""

    def test_auth_error_on_last_provider_escalates(self, video) -> None:
        caller = _caller([_provider()], [_fake_client([_auth_error()])])
        with pytest.raises(AllProvidersExhaustedError):
            caller("sys", "user", video)

    def test_auth_error_on_first_provider_fails_over(self, video) -> None:
        second = _fake_client([_response("ok")])
        caller = _caller(
            [_provider(), _provider(model="backup-model")],
            [_fake_client([_auth_error()]), second],
        )
        assert caller("sys", "user", video) == "ok"
        assert second.chat.completions.calls == 1

    def test_retryable_exhausted_on_last_provider_escalates(self, video) -> None:
        caller = _caller(
            [_provider(max_retries=1)], [_fake_client([_timeout_error()])]
        )
        with pytest.raises(AllProvidersExhaustedError):
            caller("sys", "user", video)

    def test_plain_client_error_propagates_unchanged(self, video) -> None:
        request = httpx.Request("POST", "http://localhost:1/v1/chat/completions")
        bad_request = openai.BadRequestError(
            "bad payload", response=httpx.Response(400, request=request), body=None
        )
        caller = _caller([_provider()], [_fake_client([bad_request])])
        with pytest.raises(openai.BadRequestError):
            caller("sys", "user", video)


class TestEmptyResponseRetry:
    """P2-2:空响应有界即时重试(1 次),仍空才抛错。"""

    def test_empty_then_success(self, video) -> None:
        completions_client = _fake_client([_empty_response(), _response("ok")])
        caller = _caller([_provider()], [completions_client])
        assert caller("sys", "user", video) == "ok"
        assert completions_client.chat.completions.calls == 2

    def test_persistent_empty_raises_after_bounded_retry(self, video) -> None:
        completions_client = _fake_client([_empty_response(), _empty_response()])
        caller = _caller([_provider()], [completions_client])
        with pytest.raises(RuntimeError, match="empty response content"):
            caller("sys", "user", video)
        assert completions_client.chat.completions.calls == 2
