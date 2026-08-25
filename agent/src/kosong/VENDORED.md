# Vendored from MoonshotAI/kimi-code

- Source: https://github.com/MoonshotAI/kimi-code/tree/main/packages/kosong
- Version: 0.5.5 (shallow clone, 2026-08-24)
- License: MIT (see LICENSE in this directory)
- Internal `#/*` imports resolve to this directory via package.json `imports` + tsconfig `paths`.
- Do not edit casually; sync from upstream when needed.

## Local modifications

- `providers/openai-legacy.ts` (2026-08-25, traffic-agent Wave 1): tool-result
  `video_url` content parts are now reattached as a follow-up user message
  (same mechanism as `image_url`), serialized as
  `{type:'video_url', video_url:{url}}` — the local vLLM qwen endpoint accepts
  it. Upstream dropped video parts with a `(video omitted: ...)` placeholder;
  that placeholder was removed for video (audio behavior unchanged).
  `toolResultImageParts` renamed to `toolResultMediaParts`.
