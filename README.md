[English](README.md) | [简体中文](README.zh-CN.md)

# Traffic Analyzer

VLM (vision-language model) based traffic event detection for highway surveillance
video. Input: a video clip. Output: a **10-bit binary event code** plus a structured
analysis report (JSON or Markdown). Event definitions, prompts, adjudication rules,
and thresholds are all YAML-driven — adding or tuning an event needs no code changes.

**Current version: 5.0.0** — adds a web UI (FastAPI backend + SPA frontend) with
workspace management, queued single/batch inference, per-video result cards with a
visual-evidence editor, and in-UI batch accuracy evaluation.

## Overview

Each video runs through a four-step pipeline:

1. **Video preprocessing** — metadata extraction, optional quality prefilter (may
   reject), then fixed-FPS frame extraction. Zero usable frames also rejects.
2. **Expert Agent layer** — one `ExpertAgent` per active event category, run in parallel
   (`ThreadPoolExecutor`); each reports only facts ("see it, report it"). Events with
   far-object enhancement use a multi-stage ROI pipeline; every candidate passes an
   optional reflection check.
3. **Adjudication** — a single VLM call rules on all candidates with business rules and
   the annotation spec, producing final per-event results plus an audit log (with a
   retry loop for missing results).
4. **Report generation** — per-event results, binary encoding, audit log, token usage,
   rendered as JSON or Markdown.

## Key Features

- **YAML-driven events** — definitions, detection mode, prompt references, thresholds,
  and adjudication rules live in `traffic_analyzer/config/event_categories.yaml`;
  prompts live in `traffic_analyzer/config/prompts/*.yaml`. Inactive events keep their
  bit in the encoding but are skipped (always report 0).
- **Expert-agent detection** — the only detection mode with an execution path. One
  specialist prompt per event; agents run in parallel and return structured
  `EventCandidate` objects (detected flag, summary, time-bounded instances).
- **Far-object enhancement** — events with `far_object_enhancement.enabled: true` in
  their prompt template (currently events **1, 3, 4, 6**) use a multi-stage ROI
  pipeline: ROI detection → evidence compositing → final classifier.
- **Reflection consistency check** — each candidate is re-checked by a text-only VLM
  pass (`expert_response_reflection` template) that corrects `detected` when it
  contradicts the summary/instances. Fail-open; on by default.
- **Multi-provider VLM with retry/failover** — Anthropic, Google, Aliyun; per-provider
  retry with backoff; sticky failover on rate-limit/auth/quota/5xx errors; total
  exhaustion aborts loudly (`FatalAPIError`) instead of emitting all-zero reports.
- **Two-layer response cache** — in-memory LRU plus optional SQLite disk cache, keyed by
  SHA-256 of prompt + images, filtered by provider + model; corrupt rows self-heal.
- **Reject paths for bad videos** — prefilter failures or undecodable videos produce a
  reject report, CLI exit code 2, and no output file.
- **`validate-config` guardrails** — fail-fast duplicate-ID detection plus
  cross-reference checks (see [Configuration](#configuration)).

## Quick Start

### Requirements

- Python 3.10+ (Docker image uses 3.11)
- `pip install -r requirements.txt` (runtime) and `pip install -r requirements-dev.txt` (tests)

### 1. Configure the VLM provider

```bash
cp traffic_analyzer/config/.env.example traffic_analyzer/config/.env
# edit .env: set API key(s) and model(s)
```

LLM settings are read **only from `.env`** (config directory, or repo root as a legacy
fallback) — shell environment variables for provider/key/model are ignored. See
[VLM Providers & Caching](#vlm-providers--caching) for the variable list.

### 2. Validate the configuration

```bash
python3 -m traffic_analyzer validate-config --config-dir ./traffic_analyzer/config
```

A pre-commit hook runs this check on config changes (`pip install pre-commit && pre-commit
install`, see `.pre-commit-config.yaml`).

### 3. Run analysis

```bash
# Markdown report written to a file
python3 -m traffic_analyzer analyze \
  --video ./path/to/video.mp4 \
  --format markdown \
  --output ./output/report.md

# JSON to stdout (default format), more frames per VLM call
python3 -m traffic_analyzer analyze \
  --video ./path/to/video.mp4 \
  --min-frames 20
```

`--min-frames N` sets both `SCENE_UNDERSTANDING_MIN_FRAMES` and `VLM_MAX_FRAMES` for the
run (default: 10). Other flags: `--config-dir`, `--scene-understanding <json>` (inject a
pre-computed `SceneInfo`), global `--log-level`.

#### Optional: SFT label mode (`--sft-label`)

```bash
python3 -m traffic_analyzer analyze \
  --video ./path/to/video.mp4 \
  --sft-label \
  --sft-output-dir ./output/sft_labels   # optional; this is the default
```

Appends a rewrite step after adjudication: one extra VLM call sees **only the raw
sampled frames plus the adjudicated verdicts** (privileged hints) and writes **one SFT
training-sample JSON per video** into `--sft-output-dir` (default `output/sft_labels`).
The main report is unaffected; the mode costs **+1 VLM call per video**. Samples whose
positive events cannot be grounded in the raw frames are quarantined — see
[SFT sample JSON](#sft-sample-json---sft-label).

#### Optional: Web UI (`traffic_analyzer web`)

```bash
python3 -m traffic_analyzer web            # defaults: http://127.0.0.1:8600
python3 -m traffic_analyzer web --host 0.0.0.0 --port 9000 --workspace ./workspace
```

The UI (FastAPI backend + SPA in `traffic_analyzer/web/`) provides:

- **Workspace selection** — videos and analysis results live under one workspace folder.
- **Single/batch inference** — a background job queue with per-job progress.
- **Per-video result cards** — SFT sample detail, the Markdown report, and a
  visual-evidence editor (polygon & box vertex edits saved back to
  `<stem>_evidence.json`).
- **Batch accuracy evaluation** — `scripts/batch_evaluate.py` merged into the UI,
  with per-event precision/recall/F1.

Inference jobs run the same `analyze` pipeline with `--sft-label` enabled, so each
job also exports `<stem>_evidence.json` — see
[Workspace results layout](#workspace-results-layout-web-ui).

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success; report written to `--output` or stdout |
| 1 | Error: video/config not found, analysis crash, fatal API exhaustion |
| 2 | Video rejected (prefilter failure or zero usable frames); **no report file is written** |

### Python API

```python
from traffic_analyzer.orchestrator.analysis_orchestrator import AnalysisOrchestrator

orch = AnalysisOrchestrator.from_config_dir('traffic_analyzer/config')
report = orch.analyze('path/to/video.mp4')
print(report.binary_encoding.encoding_string)   # e.g. 1_0_1_0_0_0_0_0_0_0
```

## Code Structure

```
traffic_analyzer/
├── cli.py                              # argparse CLI: analyze / validate-config / web, exit codes
├── __main__.py                         # enables `python -m traffic_analyzer`
├── __init__.py                         # __version__ = "5.0.0"
├── config/
│   ├── event_categories.yaml           # Event definitions + adjudication_rules
│   ├── annotation_spec.yaml            # Business annotation spec injected into adjudication
│   ├── .env.example                    # Template for LLM provider / inference settings
│   └── prompts/                        # 18 prompt templates in 11 YAML files
│       ├── common.yaml                 # scene_understanding / reflection / adjudication templates
│       └── event_0.yaml … event_9.yaml # Per-event expert prompts + ROI templates
├── core/
│   ├── config_manager.py               # Config loading (.env + YAML), validation, version selection
│   ├── pipeline_steps.py               # PipelineStep base, ExpertAgentLayer, AdjudicationStep
│   ├── expert_agent.py                 # ExpertAgent: single-event detection flow
│   ├── expert_agent_far_enhancement.py # Far-distance ROI enhancement pipelines (events 1/3/4/6)
│   ├── expert_agent_tools.py           # ToolCallExecutor (unused while registry is empty)
│   ├── video_preprocessor.py           # Metadata, prefilter, fixed-FPS sampling + dedup
│   ├── vlm_engine.py                   # VLMInferenceEngine: retry, failover, cache, usage stats
│   ├── vlm_provider_clients.py         # Provider-specific payload builders + API calls
│   ├── vlm_error_classifier.py         # Retryable / failover-trigger / fatal error rules
│   ├── vlm_cache.py                    # DiskCache (SQLite) + cache-key computation
│   ├── vlm_response_parser.py          # JSON extraction/repair from VLM text
│   ├── vlm_exceptions.py               # VLM exception types
│   ├── report_generator.py             # Report assembly + binary encoding
│   ├── report_markdown_renderer.py     # Markdown report rendering (Chinese UI)
│   ├── report_far_enhancement_renderer.py  # Far-enhancement evidence sections
│   ├── report_text_utils.py            # Report text formatting helpers
│   └── evidence_exporter.py            # <stem>_evidence.json export (schema_version 1)
├── models/
│   ├── config.py                       # SystemConfig, LLMProviderConfig, SamplingConfig
│   ├── event.py                        # EventCategory, EventCandidate, EventResult, AuditEntry
│   ├── video.py                        # VideoMetadata, Keyframe, KeyframeSequence
│   ├── scene.py                        # SceneInfo, RoadInfo, DirectionAnalysis, …
│   ├── llm.py                          # LLMResponse, PromptTemplate, LLMCallRecord, …
│   ├── report.py                       # Report, BinaryEncoding
│   ├── context.py                      # AnalysisContext (shared pipeline state)
│   ├── enums.py                        # DetectionMode, ConfidenceLevel
│   └── schemas.py                      # Re-exports all model modules
├── orchestrator/
│   ├── analysis_orchestrator.py        # Main 4-step pipeline wiring (analyze())
│   ├── video_meta_extractor.py         # Lightweight video metadata extraction
│   ├── reject_report_factory.py        # Reject report construction
│   ├── candidate_fallback.py           # Candidate → EventResult fallback conversion
│   └── orchestrator_exceptions.py      # Orchestrator exception types
├── tools/                              # RESERVED — schema/router exist, registry is empty
│   ├── tool_schema.py                  # Tool definition layer
│   ├── tool_router.py                  # Tool routing layer
│   └── tool_registry.py                # Default router factory (registers zero tools)
├── utils/
│   ├── event_detection.py              # Image selection, response parsing, reflection check
│   ├── emergency_lane_occupancy.py     # Event-1 evidence images (masks, boxes, zoom grids)
│   ├── far_non_motor_enhancer.py       # Non-motor vehicle enhancement helpers
│   ├── roi_composite.py                # ROI composite image generation
│   ├── roi_motion.py                   # Adjacent-frame ROI motion analysis
│   ├── construction_evidence_gallery.py# Event-6 multi-ROI evidence gallery
│   ├── bbox_geometry.py                # Bounding-box geometry helpers
│   ├── image_drawing.py                # Image annotation helpers
│   ├── annotation_spec_loader.py       # annotation_spec.yaml → prompt text
│   └── tool_call_logger.py             # tool_call trace logging
├── web/                                # FastAPI backend + SPA frontend (web/static/)
└── tests/                              # pytest suite (unit + pipeline-level, VLM mocked)
    ├── test_*.py                       # 16 test modules (CLI, config, engine, experts, reports)
    └── tools/test_tool_router.py       # Tool router tests

scripts/
├── analyze.sh / infer.sh               # Example single-video CLI invocations
├── batch_infer.py                      # Batch inference over a directory (see Testing)
└── batch_evaluate.py                   # Ground-truth evaluation → HTML/MD/JSON report

# Root files of note
requirements.txt / requirements-dev.txt # Runtime / dev dependencies
.pre-commit-config.yaml                 # Runs validate-config on config changes
Dockerfile{,.gpu,.cuda}, docker-compose{,.gpu}.yml  # Dev containers (CPU / GPU)
交通事件数据标注说明文档_v4.5.md          # Annotation authority document (Chinese)
```

## Analysis Pipeline

`AnalysisOrchestrator.analyze()` (`orchestrator/analysis_orchestrator.py`) runs:

1. **Metadata extraction** (`orchestrator/video_meta_extractor.py`) — duration, fps,
   resolution, bitrate; needed early so reject reports carry video info.
2. **Preprocessing** (`core/video_preprocessor.py`):
   - Optional **prefilter** (`PREFILTER_ENABLE`, code default `false`; the shipped
     `.env.example` sets `true`) checks duration, bitrate, and night-scene brightness.
     Failure → `VideoPrefilterError` → **reject report, exit 2, no file saved**.
   - Single-pass sampling at `SAMPLING_FPS` (default 1.0 fps) with quality scoring
     and deduplication; the returned precision frame list mirrors the coarse list
     (the motion-segment precision-sampling code is retained but not in the
     execution path).
   - If both frame lists end up empty (undecodable video) → **reject report, exit 2**.
3. **Expert Agent layer** (`core/pipeline_steps.py: ExpertAgentLayer`):
   - One `ExpertAgent` per active category in a `ThreadPoolExecutor` (4 workers).
     Frame selection: up to `VLM_MAX_FRAMES` (default 10) evenly spaced coarse frames.
   - There is no scene-understanding step: the `scene_understanding` template is
     injected into every expert prompt as prior knowledge, and a pre-computed
     `SceneInfo` can be supplied externally via `--scene-understanding`.
   - **Far-object enhancement** for templates that enable it (events 1, 3, 4, 6):
     - Event 1 (Emergency Lane Occupancy): lane calibration ROI + vehicle boxes +
       zoom-grid evidence (`utils/emergency_lane_occupancy.py`).
     - Events 3/4 (Person / Motorcycle): per-frame ROI detection → top-K scoring →
       dual composite (single-frame zoom + adjacent-frame motion) → final classifier,
       with evidence-based promotion/veto rules.
     - Event 6 (Road Construction): multi-ROI gallery (cone/worker/vehicle/barrier/sign)
       from the middle frame → gallery classifier + work-zone fallback.
     - If an enabled enhancement flow fails to produce evidence, the candidate is
       negative — raw frames are intentionally **not** fed to enhancement prompts.
   - **Reflection** (default on): `reflect_expert_candidate` re-checks each candidate's
     `detected` against its summary/instances; fail-open, disable with
     `EXPERT_ENABLE_REFLECTION=false`.
   - Expert errors degrade to a `detected=False` error candidate; `FatalAPIError`
     propagates and aborts the run.
4. **Adjudication** (`core/pipeline_steps.py: AdjudicationStep`):
   - One VLM call: all candidates + keyframes + `adjudication_rules` +
     `annotation_spec.yaml` text, with a JSON-schema-constrained response.
   - Up to **5 attempts**; missing events trigger re-runs of abnormal experts or a
     re-prompt listing the omissions; after the last attempt they are backfilled from
     the raw expert candidates.
   - Instance handling: negative rulings drop instances; the ruling layer cannot invent
     instances — it may only edit description/reasoning when the instance count matches.
   - On total adjudication failure, a fallback returns the raw candidates unfiltered.
5. **Report generation** (`core/report_generator.py` + renderers):
   - The **binary encoding** is produced here: width = number of configured categories
     (10), bit *i* = 1 iff event *i* was adjudicated as detected.
   - Output: the `Report` model as JSON (default) or Markdown. With `--output`,
     far-enhancement composites are saved under `<output_dir>/tmp_img/<video_stem>/`
     and referenced from the Markdown report.

## Configuration

### `config/event_categories.yaml`

Per category: `event_id`, `event_code`, `name`/`name_zh`, `detection_mode`,
`prompt_template_id`, `confidence_threshold`, `is_active`, `definition` (injected into
the expert prompt). Also holds `adjudication_rules` (`rule_id`, `name`, `description`,
`priority` 0–1000, descending). Currently one rule is configured: `emergency_parking_both`
(a vehicle stopped on the emergency lane triggers both Illegal Parking and Emergency
Lane Occupancy).

### `config/prompts/*.yaml`

18 prompt templates across 11 files (`common.yaml` + `event_0..9.yaml`): per-event
expert prompts, ROI templates for enhancement, `scene_understanding`,
`expert_response_reflection`, and `adjudication`. Multiple versions of a template are
supported; selection: env pin → A/B traffic split → latest version.

### `config/annotation_spec.yaml`

Machine-readable digest of the annotation authority document (`交通事件数据标注说明文档_v4.5.md`),
injected into the adjudication prompt. Its event IDs must exactly match `event_categories.yaml`.

### `.env` variables

Single provider (legacy style) or indexed multi-provider list (takes precedence when any
`LLM_PROVIDER_<i>_PROVIDER` is present):

| Variable | Default (code) | Description |
|---|---|---|
| `VLM_PROVIDER` / `LLM_PROVIDER` | `anthropic` | Provider: `anthropic` / `google` / `aliyun` |
| `LLM_API_KEY` | — | API key |
| `LLM_BASE_URL` | — | Custom endpoint |
| `LLM_MODEL` | `claude-sonnet-4-6` | Model name |
| `LLM_PROVIDER_<i>_PROVIDER` / `_API_KEY` / `_BASE_URL` / `_MODEL` | — | Indexed provider *i* (0 = primary) |
| `<PROVIDER>_API_KEY` / `_BASE_URL` / `_MODEL` | — | Provider-specific override (e.g. `ALIYUN_API_KEY`) |
| `LLM_MAX_TOKENS` / `LLM_TEMPERATURE` / `LLM_TIMEOUT` / `LLM_MAX_RETRIES` | `4096` / `0.2` / `300` / `3` | Inference settings |
| `LLM_ENABLE_CACHE` / `LLM_CACHE_MAX_SIZE` | `true` / `128` | In-memory response cache |
| `TRAFFIC_ANALYZER_DISK_CACHE` | — (disabled) | SQLite disk-cache path, e.g. `./output/.vlm_cache.db` |
| `TRAFFIC_ANALYZER_DISK_CACHE_MAX_ENTRIES` | `2000` | Disk cache capacity (LRU by last access) |
| `VLM_MAX_FRAMES` | `10` | Max frames per VLM call |
| `EXPERT_ENABLE_REFLECTION` | `true` | Reflection consistency check on/off |
| `SFT_LABEL_ENABLE` | `false` | SFT label rewrite step after adjudication (CLI: `--sft-label`) |
| `SFT_LABEL_OUTPUT_DIR` | `output/sft_labels` | Output directory for SFT sample JSON (CLI: `--sft-output-dir`) |
| `SAMPLING_FPS` | `1.0` | Coarse/precision sampling rate |
| `PREFILTER_ENABLE` + `PREFILTER_*` thresholds | `false` | Quality prefilter (`.env.example` enables it) |
| `PROMPT_VERSION_<TEMPLATE_ID>` | — | Pin a specific prompt version |
| `TRAFFIC_ANALYZER_TOOL_LOG_LEVEL` | `mid` | `off` / `macro` / `mid` / `fine` (`fine` reserved) |

### What `validate-config` enforces

- YAML syntax and required files; duplicate `event_id` / duplicate adjudication
  `rule_id` (both fail-fast at load).
- `annotation_spec.yaml` event IDs exactly match `event_categories.yaml`.
- Every expert category references an existing `prompt_template_id`.
- Event IDs are continuous from 0 (inactive categories included — they still occupy a bit).
- Active categories must use `expert_agent` mode — the other `DetectionMode` enum values
  (`direct_vlm`, `logic_chain`, `scene_tag`) have no execution path and are rejected.
- Active categories declaring tools are rejected (the tool registry is empty).
- Adjudication rule priorities within [0, 1000]; A/B prompt traffic percentages sum to 100%.

## VLM Providers & Caching

Supported providers (`VLMInferenceEngine.SUPPORTED_PROVIDERS`): **anthropic** (Claude),
**google** (Gemini), **aliyun** (Qwen-VL, via an OpenAI-compatible endpoint — the `openai`
SDK acts as its client, but `openai` is not a standalone provider value).

Retry and failover (`core/vlm_engine.py` + `core/vlm_error_classifier.py`):

- Per provider, up to `LLM_MAX_RETRIES` attempts with exponential backoff
  (`min(2**attempt, 30)`s) for retryable errors: rate limits, connection/timeout, 5xx.
- **Failover triggers** (retrying the same provider won't help): rate-limit, auth /
  permission / quota / billing errors, 5xx. The next indexed provider takes over.
- The serving-provider index is **sticky** and guarded by a lock — after a failover,
  subsequent calls start from the surviving provider.
- When the last provider is exhausted, `AllProvidersExhaustedError` → `FatalAPIError`:
  the analysis (and `batch_infer.py`, via stderr markers) aborts loudly instead of
  emitting all-zero reports.

Caching (`core/vlm_cache.py` + engine integration):

- Key = SHA-256 of system prompt + user prompt + image bytes. Only successful
  responses are cached.
- Layer 1: in-memory LRU (`LLM_CACHE_MAX_SIZE`). Layer 2: optional SQLite disk cache
  (`TRAFFIC_ANALYZER_DISK_CACHE`) for cross-process sharing in batch runs; disk hits
  are promoted to memory.
- A cached response is only returned when **provider and model match** the currently
  active provider, so a pre-failover response is never replayed for the new provider.
- Corrupt or stale-format disk rows are treated as a miss and deleted (self-heal).

## Output Format

### Binary encoding

`{bit_0_bit_1_..._bit_9}` — bit *i* corresponds to `event_id` *i*; width = number of
configured categories (10). Inactive categories keep their bit and always report 0.
Example: `{1_0_1_0_0_0_0_0_0_0}` = events 0 and 2 detected.

| Bit | Event | Code | Active |
|---|---|---|---|
| 0 | Illegal Parking (违法停车) | A | ✓ |
| 1 | Emergency Lane Occupancy (应急车道占用) | B | ✓ |
| 2 | Traffic Accident (交通事故) | C | ✓ |
| 3 | Person Presence in Highway (行人出现) | D | ✓ |
| 4 | Motorcycle Presence (摩托车出现) | E | ✓ |
| 5 | Heavy Congestion (拥堵) | F | ✓ |
| 6 | Road Construction (道路施工) | G | ✓ |
| 7 | Vehicle Reversing (车辆逆行/倒车) | H | ✓ |
| 8 | Thrown Objects (抛洒物) | J | ✗ (bit reserved, always 0) |
| 9 | Lane Change over Solid Line (实线变道) | K | ✗ (bit reserved, always 0) |

### Report

The `Report` model (`models/report.py`) contains: `video_info`, `scene_summary`,
`overall_traffic_description`, per-event `event_results` (detected flag, summary,
time-bounded instances with evidence frames), raw `expert_candidates`, `binary_encoding`,
`final_classification`, `disposal_recommendations`, `adjudication_reasoning` + per-event
`reasoning_chain`, `audit_log` (exclusions with reason and `rule_id`), `llm_usage_stats`,
`analysis_duration_sec`, and `rejected` / `reject_reason`.

The Markdown rendering (Chinese UI) sections: 视频信息 → 事件类别分析 (per-event expert
output, enhancement evidence, adjudicated result, instances) → 最终分类 → 裁决详情 →
处置建议 → 分析统计.

**Reject reports**: `rejected=true`, empty event results, encoding `0_0_0_0_0_0_0_0_0_0`,
`final_classification` = "视频被筛除/无法分析，未进行事件检测。". The CLI writes **no
output file** for rejected videos and exits with code 2.

### SFT sample JSON (`--sft-label`)

With `--sft-label` enabled, one training sample per video is written to
`<sft-output-dir>/<video_stem>.json`:

```json
{
  "chunk": "chunk #1",
  "idx": 1,
  "action": [2],
  "description": "<think>...</think>\n<answer>...</answer>",
  "start_timestamp": 0.0,
  "end_timestamp": 19.734,
  "chunk_name": "02_Event_129_1748049879151_1.mp4"
}
```

- `action` holds the annotation-doc action numbers of the detected events (empty list =
  normal sample). Mapping from `event_id` (action 9 is a "normal" placeholder in the
  annotation doc v4.5 and is intentionally skipped):

| event_id | Event | action |
|---|---|---|
| 0 | Illegal Parking (违法停车) | 1 |
| 1 | Emergency Lane Occupancy (应急车道占用) | 2 |
| 2 | Traffic Accident (交通事故) | 3 |
| 3 | Person Presence in Highway (行人出现) | 4 |
| 4 | Motorcycle Presence (摩托车出现) | 5 |
| 5 | Heavy Congestion (拥堵) | 6 |
| 6 | Road Construction (道路施工) | 7 |
| 7 | Vehicle Reversing (车辆逆行/倒车) | 8 |
| 8 | Thrown Objects (抛洒物) | 10 |
| 9 | Lane Change over Solid Line (实线变道) | 11 |

- `description` is assembled in code from the rewrite VLM response:
  - `<think>` — one thinking entry per event category (event_id 0–9, fixed order).
    Undetected events state "未发现" plus a one-sentence reason; detected events must
    cover the required description elements of the annotation spec v4.5 (location /
    lane type, incoming/outgoing direction, vehicle or object type, visual
    description, …).
  - `<answer>` — the final conclusion (`classN: 事件名` list, consistent with
    `action`) plus weather (晴天/雨天/雾天/雪天/阴天), time of day (白天/夜间/晨昏),
    and a basic traffic-scene description (ramp / gore area / toll gate, tunnel vs.
    highway, incoming/outgoing lanes, traffic volume 大/中/小) with no event content.
- **Quarantine**: if any adjudicated-positive event is flagged as not groundable in the
  raw frames (`ungrounded_event_ids`), the sample is written to
  `<sft-output-dir>/quarantine/<video_stem>.json` instead — such samples would teach
  the student to hallucinate.

### Workspace results layout (web UI)

Web UI inference jobs store per-video results under `<workspace>/analysis/<video_stem>/`:

- `report.md` — the Markdown report
- `<video_stem>.json` — the serialized `Report` model
- `<video_stem>_evidence.json` — the editable visual-evidence file (schema_version 1):
  calibration polygons, evidence regions, and gallery images with normalized [0,1]
  coordinates; the UI's evidence editor saves vertex edits back to this file
- `images/` — the evidence images referenced by the JSON

Batch evaluation output is written to `<workspace>/analysis/evaluation/latest.json`.

## Testing

```bash
python3 -m pytest traffic_analyzer/tests -q
```

The suite (currently 407 passed, 1 skipped — the skip is expected when the installed
anthropic SDK lacks `OverloadedError`) mocks all VLM calls and covers: config loading
and validation, CLI and exit codes, video preprocessing, the expert layer, far-enhancement
pipelines, reflection, adjudication, report generation, providers, retry/failover, caches.

Batch workflow helpers:

```bash
# Batch inference (4 workers by default; each video runs as a CLI subprocess)
python3 scripts/batch_infer.py \
  --video-dir ./test_videos \
  --output-dir ./output \
  --log-dir ./output/logs \
  --format markdown            # --min-frames default 10; --force to re-run existing

# Evaluation against ground truth (from filenames or an annotation file)
python3 scripts/batch_evaluate.py \
  --video-dir ./test_videos \
  --report-dir ./output \
  --output ./evaluation_report.html \
  --single-class               # only evaluate is_active=true events
```

`batch_infer.py` skips videos with existing reports unless `--force`, stops the batch
on fatal API errors, and treats exit code 2 as "rejected, no report expected".
`batch_evaluate.py` supports `--gt-mode filename|annotation_file`; output format
(`.html` / `.md` / `.json`) comes from the `--output` extension.

## Known Limitations

- **Only `expert_agent` mode is implemented.** The `DetectionMode` enum still carries
  `direct_vlm` / `logic_chain` / `scene_tag`, but they have no execution path and
  `validate-config` rejects active categories that use them.
- **The tool subsystem is an empty shell** — `tools/tool_schema.py` / `tool_router.py`
  are functional, but `tool_registry.py` registers zero tools; `validate-config`
  rejects active categories that declare tools.
- **Reflection is fail-open** — if the reflection call fails or returns unparseable
  output, the original candidate is kept; disable with `EXPERT_ENABLE_REFLECTION=false`.
- **`batch_infer.py --cv-tracks-dir` is currently broken**: it passes a `--cv-tracks`
  flag the CLI's `analyze` command does not accept; CV-track cross-validation is not
  wired into the CLI.
- **Far-enhancement failure means a negative candidate** — when an enabled enhancement
  flow cannot produce evidence, no raw-frame fallback is attempted for that event.
- **SFT label quarantine** — with `--sft-label`, positive events that cannot be
  grounded in the raw frames (e.g. far-distance small objects visible only in enhanced
  evidence) are quarantined under `quarantine/` and never emitted as training samples,
  so the student is not taught to hallucinate. SFT samples are **not class-balanced**;
  balancing is left to the training side.
- **Heuristic safeguards** — adjudication instance-count matching, enhancement
  promotion/veto rules, and prefilter thresholds can misjudge borderline cases.
- **Markdown reports are rendered in Chinese**, regardless of CLI language.
- Archived snapshots: tags `v1.1.0`, `v1.5.0-legacy` (branch `legacy/v1.5`),
  `v2.0.0-multi-agent`. All current development is on `main` (v5.0.0).
