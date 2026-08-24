"""Heuristic token estimation for chat context compaction.

[文件说明]
作用:快速对话上下文长度的启发式估计。estimate_tokens:中文字符≈1 token,
ASCII 单词≈1.3 token;estimate_request 叠加图片估计(每张 IMAGE_TOKENS_EST)。
注意:这只是估计,误差由 CONTEXT_LIMIT(256k) 与 COMPACT_AT(200k) 之间的
余量吸收,不追求精确。
上游:web/chat/qa.py(maybe_compact 判断是否压缩)。
下游:无(纯函数)。
"""

from __future__ import annotations

import re
from typing import List

CONTEXT_LIMIT = 256_000
COMPACT_AT = 200_000
# 1080p 帧 ≈ (1920/28)×(1080/28) ≈ 68×39 ≈ 2600 patch tokens(vLLM 按 28×28 patch 计数)。
IMAGE_TOKENS_EST = 2600

_ASCII_RUN = re.compile(r"[\x00-\x7f]+")


def estimate_tokens(text: str) -> int:
    """Estimate token count: CJK chars ≈ 1 each, ASCII words ≈ 1.3 each."""
    if not text:
        return 0
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    ascii_words = 0
    for run in _ASCII_RUN.findall(text):
        ascii_words += len(run.split())
    return int(non_ascii + ascii_words * 1.3)


def estimate_request(messages: List[str], n_images: int) -> int:
    """Estimated total tokens for a request: texts plus per-image estimate."""
    return sum(estimate_tokens(m) for m in messages) + n_images * IMAGE_TOKENS_EST
