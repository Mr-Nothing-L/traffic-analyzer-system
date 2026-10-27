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
    """按队列返回响应或抛出异常,并记录每次 create 的 kwargs。"""

    def __init__(self, outcomes: List[object]) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0
        self.kwargs: List[dict] = []

    def create(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
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
        assert caller("sys", "user", video).text == "ok"
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
        assert caller("sys", "user", video).text == "ok"
        assert completions_client.chat.completions.calls == 2

    def test_persistent_empty_raises_after_bounded_retry(self, video) -> None:
        completions_client = _fake_client([_empty_response(), _empty_response()])
        caller = _caller([_provider()], [completions_client])
        with pytest.raises(RuntimeError, match="empty response content"):
            caller("sys", "user", video)
        assert completions_client.chat.completions.calls == 2


def _thinking_response(text: str, reasoning: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text, reasoning_content=reasoning)
            )
        ]
    )


class TestThinkingKwargs:
    """thinking 档位 → chat_template_kwargs 与 max_tokens 下限。"""

    def _kwargs(self, client: SimpleNamespace) -> dict:
        return client.chat.completions.kwargs[0]

    def test_off_disables_thinking(self, video) -> None:
        client = _fake_client([_response("ok")])
        caller = VideoVLMCaller([_provider(max_tokens=4096)], disk_cache_path=None, thinking="off")
        caller._clients = [client]
        caller("sys", "user", video)
        kwargs = self._kwargs(client)
        assert kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
        assert kwargs["max_tokens"] == 4096

    def test_medium_default_effort_and_token_floor(self, video) -> None:
        client = _fake_client([_response("ok")])
        caller = _caller([_provider(max_tokens=4096)], [client])
        assert caller._thinking == "medium"
        caller("sys", "user", video)
        kwargs = self._kwargs(client)
        assert kwargs["extra_body"] == {
            "chat_template_kwargs": {"enable_thinking": True, "reasoning_effort": "medium"}
        }
        assert kwargs["max_tokens"] == 8192

    def test_high_maps_to_xhigh(self, video) -> None:
        client = _fake_client([_response("ok")])
        caller = VideoVLMCaller([_provider()], disk_cache_path=None, thinking="high")
        caller._clients = [client]
        caller("sys", "user", video)
        assert self._kwargs(client)["extra_body"]["chat_template_kwargs"]["reasoning_effort"] == "xhigh"

    def test_invalid_mode_rejected(self) -> None:
        with pytest.raises(ValueError, match="thinking"):
            VideoVLMCaller([_provider()], disk_cache_path=None, thinking="extreme")


class TestReasoningCapture:
    """reasoning_content 捕获进 CallerResult,并经磁盘缓存 round-trip。"""

    def test_reasoning_returned(self, video) -> None:
        client = _fake_client([_thinking_response("ok", "隐藏推理过程")])
        caller = _caller([_provider()], [client])
        result = caller("sys", "user", video)
        assert result.text == "ok"
        assert result.reasoning == "隐藏推理过程"

    def test_reasoning_absent_gives_empty(self, video) -> None:
        client = _fake_client([_response("ok")])
        caller = _caller([_provider()], [client])
        assert caller("sys", "user", video).reasoning == ""

    def test_disk_cache_round_trip(self, video, tmp_path) -> None:
        cache_db = str(tmp_path / "cache.db")
        client = _fake_client([_thinking_response("ok", "缓存的推理")])
        caller = VideoVLMCaller([_provider()], disk_cache_path=cache_db)
        caller._clients = [client]
        first = caller("sys", "user", video)
        assert first.reasoning == "缓存的推理"

        # 新实例(无网络 client 队列已空)命中缓存,text 与 reasoning 均还原
        cached_caller = VideoVLMCaller([_provider()], disk_cache_path=cache_db)
        cached_caller._clients = [_fake_client([])]
        second = cached_caller("sys", "user", video)
        assert second.text == "ok"
        assert second.reasoning == "缓存的推理"

    def test_cache_key_includes_thinking_mode(self, video, tmp_path) -> None:
        cache_db = str(tmp_path / "cache.db")
        off_caller = VideoVLMCaller([_provider()], disk_cache_path=cache_db, thinking="off")
        off_caller._clients = [_fake_client([_response("off-text")])]
        off_caller("sys", "user", video)

        # 档位不同不得命中上一档位的缓存
        mid_client = _fake_client([_response("mid-text")])
        mid_caller = VideoVLMCaller([_provider()], disk_cache_path=cache_db, thinking="medium")
        mid_caller._clients = [mid_client]
        assert mid_caller("sys", "user", video).text == "mid-text"
        assert mid_client.chat.completions.calls == 1


def _chunk(reasoning: str = "", content: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(reasoning_content=reasoning, content=content)
            )
        ]
    )


class TestStreaming:
    """on_delta 流式:逐 chunk 回调增量并聚合;重试前 reset。"""

    def test_deltas_forwarded_and_aggregated(self, video) -> None:
        chunks = [
            _chunk(reasoning="思1"),
            _chunk(reasoning="思2", content="答1"),
            _chunk(content="答2"),
        ]
        client = _fake_client([chunks])
        caller = _caller([_provider()], [client])
        deltas: List[tuple] = []
        result = caller("sys", "user", video, on_delta=lambda f, t: deltas.append((f, t)))
        assert result.text == "答1答2"
        assert result.reasoning == "思1思2"
        assert deltas == [
            ("reasoning", "思1"),
            ("reasoning", "思2"),
            ("response", "答1"),
            ("response", "答2"),
        ]
        # 流式路径带 stream=True
        assert client.chat.completions.kwargs[0]["stream"] is True

    def test_reset_before_empty_retry(self, video) -> None:
        # 第一遍只流 reasoning 无 content(空响应)→ reset → 第二遍完整
        client = _fake_client([
            [_chunk(reasoning="作废")],
            [_chunk(reasoning="思", content="答")],
        ])
        caller = _caller([_provider()], [client])
        deltas: List[tuple] = []
        result = caller("sys", "user", video, on_delta=lambda f, t: deltas.append((f, t)))
        assert result.text == "答"
        assert ("reset", "") in deltas
        assert deltas[-2:] == [("reasoning", "思"), ("response", "答")]

    def test_no_delta_hook_uses_non_stream(self, video) -> None:
        client = _fake_client([_thinking_response("ok", "r")])
        caller = _caller([_provider()], [client])
        result = caller("sys", "user", video)
        assert result.text == "ok"
        assert "stream" not in client.chat.completions.kwargs[0]
