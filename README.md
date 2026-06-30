[English](README.md) | [简体中文](README.zh-CN.md)

# Traffic Analyzer

A multi-modal large vision model (VLM) based traffic event detection framework for highway surveillance video. Supports **10 event categories** (currently **8 active**: 0-7), outputs a 10-bit binary encoding plus a detailed Markdown analysis report. All event definitions, prompt templates, and adjudication rules are driven by YAML configuration — adding a new event requires zero code changes.

> **Current version: v4.0.0** — VLM multi-agent expert + adjudication architecture, with far-distance ROI evidence enhancement for pedestrians, non-motor vehicles, and road construction. The tool-layer framework is retained but currently has no built-in tools.

---

## Architecture Overview (v4.0.0)

```
Video Input
    |
    v
1. Video Preprocessing
   - Coarse sampling + precision keyframe extraction
   - Two-stage sampling (dense early + uniform late)
    |
    v
2. ExpertAgentLayer (parallel ExpertAgents for active events)
   Each ExpertAgent: single-event VLM call -> EventCandidate
   - Only fact identification (see it, report it)
   - event_id=3 (Person Presence), event_id=4 (Motorcycle Presence), and
     event_id=6 (Road Construction) use far-distance ROI evidence
     enhancement when enabled in the prompt template:
       * event_id=3/4: per-frame ROI detection -> dual composite
         (single-frame + motion comparison) -> final classifier
       * event_id=6: middle-frame multi-ROI gallery -> final classifier
    |
    v
3. AdjudicationStep (single VLM call, with retry loop)
   Input: all EventCandidates + keyframes + business rules + annotation spec
   Output: final EventResults + AuditLog
   - Resolves conflicts (e.g. accident suppresses parking)
   - Applies business rules from YAML
   - Retries up to 5 times if event_results are incomplete
    |
    v
4. Report Generation
   - Markdown report (human-readable, per-step timing)
   - JSON report
   - Binary encoding {bit_0_bit_1_..._bit_9}
   - Audit log of every inclusion / exclusion decision
```

The default inference pipeline is **VLM-driven**. The tool-layer framework (Tool Schema + Tool Router) is retained for future extensions, but currently has no built-in tools.

---

## Supported Events

The following events are `is_active=true`. Event slots 8 and 9 are preserved in the binary encoding but are skipped during inference.

| ID | Code | Name | is_active |
|---|---|---|---|
| 0 | A | Illegal Parking | true |
| 1 | B | Emergency Lane Occupancy | true |
| 2 | C | Traffic Accident | true |
| 3 | D | Person Presence in Highway | true |
| 4 | E | Motorcycle Presence | true |
| 5 | F | Heavy Congestion | true |
| 6 | G | Road Construction | true |
| 7 | H | Vehicle Reversing | true |

Events 8 (Thrown Objects) and 9 (Lane Change over Solid Line) are currently inactive.

---

## Key Features

### 1. Expert Agent Layer

Each active event gets its own **ExpertAgent** — a dedicated VLM call with a specialized prompt. Agents run in parallel via `ThreadPoolExecutor`. Each agent only performs **fact identification** (what it sees) without any filtering. This separation of concerns makes the system modular and debuggable.

### 2. Far-Distance ROI Evidence Enhancement

Events with `far_object_enhancement.enabled: true` in their prompt template use a dedicated ROI-driven enhancement flow. This is currently enabled for:

- **event_id=3 (Person Presence in Highway)** — per-frame ROI detection returns a normalized bbox, occlusion flag, and a continuous `confidence` in `[0.0, 1.0]`. Top-K candidates are scored by confidence, area, aspect ratio, occlusion, and adjacent-frame motion, then passed through dual composites (single-frame zoom + adjacent-frame motion comparison) to a final classifier that returns a full expert response.
- **event_id=4 (Motorcycle Presence)** — same per-frame ROI and dual-composite pipeline as event_id=3, specialized for motorcycles/electric bikes/bicycles/tricycles. The final classifier uses a minimal `{detected, reason}` schema and includes a "no identifiable vehicle structure" veto to avoid false positives from dark spots or glare.
- **event_id=6 (Road Construction)** — uses a **multi-ROI gallery** from the middle frame. The ROI detector returns multiple evidence regions (`cone`, `worker`, `vehicle`, `barrier`, `sign`) with confidence and `on_ground` flags. Up to four regions are arranged into an annotated gallery composite, which is then classified. A construction-specific fallback promotes the candidate when the detected regions satisfy the work-zone definition even if the classifier is negative.

For event_id=3 and event_id=4, a high-confidence, non-occluded candidate can be promoted when the classifier is negative but the ROI evidence is strong. The stage-2 motion-comparison prompt has been refined: if the target is no longer visible in adjacent frames, that absence actually supports a moving non-motor vehicle; static dark spots or glare points should be excluded.

### 3. Adjudication Step

A **single VLM call** receives all expert candidates, keyframes, and business rules, then outputs:
- Final `EventResult` for each event (detected / not detected)
- `AuditLog` recording every inclusion / exclusion decision with reasoning
- `adjudication_reasoning` explaining the overall decision process

Business rules are defined in `event_categories.yaml` under `adjudication_rules:`. Example rules:
- **Accident suppresses parking** — stationary vehicles in an accident scene are part of the accident, not illegal parking
- **Construction excludes emergency lane** — vehicles inside a construction zone are not emergency lane violations
- **Motorcycle excludes emergency lane** — a motorcycle on the shoulder is tagged as "motorcycle presence", not "emergency lane occupancy"

### 4. Audit Log

Every event that is excluded during adjudication is recorded with a reason and the triggering rule ID. This makes the system transparent and helps debug false negatives.

```json
{
  "event_id": 0,
  "event_name": "Illegal Parking",
  "action": "excluded",
  "reason": "Vehicle is part of an accident scene",
  "rule_id": "accident_suppresses_parking"
}
```

### 5. Config-Driven Design

All of the following are defined in YAML — no code changes needed:
- Event definitions (`event_categories.yaml`)
- Prompt templates (`prompts/*.yaml`)
- Adjudication rules (`event_categories.yaml`)
- Annotation spec (`annotation_spec.yaml`)

### 6. Adjudication Retry Loop

The adjudication step runs in a loop of up to **5 attempts**. If the adjudicator's `event_results` are incomplete, the system:
- Checks whether the corresponding expert outputs are abnormal; if so, re-runs only those experts.
- Otherwise, re-runs adjudication with a prompt hint listing the events it previously omitted.
- After 5 attempts, any still-missing events are backfilled from the original expert candidates.

This makes the pipeline robust against sporadic VLM omissions without discarding good expert signals.

### 7. JSON Repair & Sanitization

VLM outputs are automatically hardened before parsing:
- `_repair_json` in `vlm_response_parser.py` fixes common syntax errors such as missing commas and trailing commas.
- `_sanitize_candidate` in `event_detection.py` reconciles inconsistent expert outputs (e.g. `detected=true` paired with content that denies the event).

---

## Project Structure

```
traffic_analyzer/
├── config/
│   ├── annotation_spec.yaml       # Annotation spec injected into adjudication prompt
│   ├── event_categories.yaml      # Event definitions + adjudication_rules
│   ├── prompts/                   # VLM prompt templates (event_*.yaml + common.yaml)
│   └── .env.example               # Example LLM provider config
├── core/
│   ├── config_manager.py          # Config loading, validation
│   ├── expert_agent.py            # Compatibility shim for single-event detection agents
│   ├── expert_agent_far_enhancement.py  # Far-distance ROI evidence enhancement
│   ├── expert_agent_tools.py      # Tool helpers for expert agents
│   ├── pipeline_steps.py          # ExpertAgentLayer + AdjudicationStep (with retry loop)
│   ├── report_generator.py        # Compatibility shim for report generation
│   ├── report_markdown_renderer.py      # Markdown report rendering
│   ├── report_far_enhancement_renderer.py  # Far-enhancement section rendering
│   ├── report_text_utils.py       # Report text formatting utilities
│   ├── video_preprocessor.py      # Video frame extraction
│   ├── vlm_engine.py              # Compatibility shim for VLM wrapper
│   ├── vlm_cache.py               # In-memory + disk VLM result cache
│   ├── vlm_response_parser.py     # VLM response parsing + JSON repair
│   ├── vlm_provider_clients.py    # Provider-specific API clients
│   ├── vlm_error_classifier.py    # Classify API errors for failover decisions
│   └── vlm_exceptions.py          # VLM-related exceptions
├── models/
│   ├── schemas.py                 # Compatibility shim re-exporting all Pydantic models
│   ├── enums.py                   # DetectionMode, ConfidenceLevel
│   ├── video.py                   # VideoMetadata, Keyframe, KeyframeSequence
│   ├── scene.py                   # SceneInfo, RoadInfo, DirectionAnalysis, ...
│   ├── event.py                   # EventCategory, EventCandidate, EventResult, AuditEntry, ...
│   ├── llm.py                     # LLMResponse, LLMCallRecord, PromptTemplate, ...
│   ├── report.py                  # Report, BinaryEncoding
│   ├── config.py                  # SystemConfig, LLMProviderConfig, SamplingConfig
│   └── context.py                 # AnalysisContext
├── orchestrator/
│   ├── analysis_orchestrator.py   # Main 4-step pipeline orchestrator
│   ├── orchestrator_exceptions.py # Orchestrator-specific exceptions
│   ├── video_meta_extractor.py    # Video metadata extraction
│   ├── reject_report_factory.py   # Reject report generation
│   └── candidate_fallback.py      # Candidate fallback helpers
├── tools/
│   ├── tool_schema.py             # Tool Definition Layer
│   ├── tool_router.py             # Tool Router Layer
│   └── tool_registry.py           # Default router registration (currently no built-in tools)
├── utils/
│   ├── event_detection.py         # Image selection + response parsing + candidate sanitization
│   ├── far_non_motor_enhancer.py  # Far-distance non-motor vehicle enhancement utilities
│   ├── roi_composite.py           # ROI composite image generation
│   ├── roi_motion.py              # ROI motion analysis
│   ├── bbox_geometry.py           # Bounding-box geometry helpers
│   ├── image_drawing.py           # Image annotation helpers
│   ├── annotation_spec_loader.py  # Annotation spec loading
│   ├── construction_evidence_gallery.py  # Construction-event evidence gallery
│   └── tool_call_logger.py        # Tool-call style logging
├── cli.py                         # CLI entry point
└── __main__.py                    # `python -m traffic_analyzer`
```

---

## Quick Start

### 1. Configure LLM Provider

```bash
cp traffic_analyzer/config/.env.example traffic_analyzer/config/.env
# Edit .env, set API Key and model
```

LLM settings are read **only from `.env`** in the config directory, not from the shell environment. Two configuration styles are supported:

**Single provider (backward-compatible):**

| Variable | Description | Default |
|---|---|---|
| `VLM_PROVIDER` / `LLM_PROVIDER` | VLM provider (`anthropic` / `google` / `aliyun`) | `anthropic` |
| `LLM_API_KEY` | API Key | - |
| `LLM_MODEL` | Model name | `claude-sonnet-4-6` |

**Multi-provider failover (recommended):**

| Variable | Description | Example |
|---|---|---|
| `LLM_PROVIDER_0_PROVIDER` | Primary provider | `anthropic` |
| `LLM_PROVIDER_0_API_KEY` | Primary API key | - |
| `LLM_PROVIDER_0_MODEL` | Primary model | `claude-sonnet-4-6` |
| `LLM_PROVIDER_1_PROVIDER` | Fallback provider | `aliyun` |
| `LLM_PROVIDER_1_API_KEY` | Fallback API key | - |
| `LLM_PROVIDER_1_MODEL` | Fallback model | `qwen-vl-max` |

When indexed `LLM_PROVIDER_N_*` variables are present, they take precedence over the single-provider variables. The orchestrator uses provider 0 first; on quota, authentication, rate-limit, or 5xx errors it automatically fails over to provider 1 (and any additional numbered providers).

Shared inference settings:

| Variable | Description | Default |
|---|---|---|
| `LLM_MAX_TOKENS` | Max output tokens | `4096` |
| `LLM_TEMPERATURE` | Sampling temperature | `0.2` |
| `LLM_TIMEOUT` | API timeout (seconds) | `120` |
| `LLM_MAX_RETRIES` | Max retry count per provider | `3` |
| `LLM_ENABLE_CACHE` | Enable in-memory VLM result cache (per-process) | `true` |
| `LLM_CACHE_MAX_SIZE` | Max in-memory cache entries | `128` |
| `TRAFFIC_ANALYZER_DISK_CACHE` | Path to SQLite disk cache (cross-process) | - |
| `TRAFFIC_ANALYZER_DISK_CACHE_MAX_ENTRIES` | Max disk cache entries | `2000` |
| `VLM_MAX_FRAMES` | Max frames per VLM call | `10` |
| `PROMPT_VERSION_{TEMPLATE_ID}` | Force a specific prompt version | - |

### 2. Install pre-commit hook (recommended)

```bash
pip install pre-commit
pre-commit install
```

Automatically validates config changes on commit to prevent invalid YAML from being committed.

### 3. Validate Configuration

```bash
python3 -m traffic_analyzer validate-config \
  --config-dir ./traffic_analyzer/config
```

### 4. Run Analysis

```bash
# Basic usage (default 10 frames)
python3 -m traffic_analyzer analyze \
  --video ./path/to/video.mp4 \
  --format markdown \
  --output ./report.md

# More frames (better accuracy, slower)
python3 -m traffic_analyzer analyze \
  --video ./path/to/video.mp4 \
  --format markdown \
  --output ./report.md \
  --min-frames 30
```

### 5. Python API

```python
from traffic_analyzer.orchestrator.analysis_orchestrator import AnalysisOrchestrator

orch = AnalysisOrchestrator.from_config_dir('traffic_analyzer/config')
report = orch.analyze('path/to/video.mp4')
print(report.binary_encoding.encoding_string)
print(report.event_results)
```

---

## Batch Inference & Evaluation

### Batch Inference (`scripts/batch_infer.py`)

```bash
python3 scripts/batch_infer.py \
  --video-dir ./videos \
  --output-dir ./reports \
  --log-dir ./logs \
  --workers 4 \
  --format markdown \
  --min-frames 30
```

| Parameter | Description | Default |
|---|---|---|
| `--video-dir` / `-v` | Input video directory (required) | - |
| `--output-dir` / `-o` | Output report directory (required) | - |
| `--config-dir` / `-c` | Config directory | `./traffic_analyzer/config` |
| `--format` / `-f` | Output format (`markdown` / `json`) | `markdown` |
| `--min-frames` / `-m` | VLM max input frames | `30` |
| `--workers` / `-w` | Parallel workers (ProcessPoolExecutor) | CPU cores |
| `--log-dir` / `-l` | Per-video log directory | - |
| `--skip-existing` | Skip videos with existing reports (default) | `true` |
| `--no-skip-existing` | Force reprocess all videos | - |

### Batch Evaluation (`scripts/batch_evaluate.py`)

```bash
# Default: interactive HTML report
python3 scripts/batch_evaluate.py \
  --video-dir ./videos \
  --report-dir ./reports \
  --output evaluation_report.html

# With standalone annotation file
python3 scripts/batch_evaluate.py \
  --video-dir ./videos \
  --report-dir ./reports \
  --gt-mode annotation_file \
  --annotation-file ./annotations.json \
  --output evaluation_report.html

# Markdown table report
python3 scripts/batch_evaluate.py \
  --video-dir ./videos \
  --report-dir ./reports \
  --output evaluation_report.md

# Single-class mode (only evaluate is_active=true events)
python3 scripts/batch_evaluate.py \
  --video-dir ./videos \
  --report-dir ./reports \
  --single-class \
  --config-dir ./traffic_analyzer/config \
  --output evaluation_report.html
```

| Parameter | Description | Default |
|---|---|---|
| `--video-dir` / `-v` | Video directory (for ground-truth extraction) | - |
| `--report-dir` / `-r` | Report directory (`.md` or `.json`) | - |
| `--output` | Output path (`.html` / `.md` / `.json`, auto-detected by extension) | `evaluation_report.html` |
| `--gt-mode` | Ground-truth source (`filename` / `annotation_file`) | `filename` |
| `--annotation-file` | Annotation file path (JSON or CSV) | - |
| `--single-class` | Only evaluate `is_active=true` events | - |
| `--config-dir` / `-c` | Config directory (for `--single-class`) | `./traffic_analyzer/config` |

**HTML Interactive Report Features:**
- Left panel: event statistics table + per-video results table (filterable by pass/fail)
- Right panel: video player + Markdown report preview
- Click a table row to play the video, click a report link to preview Markdown
- All data is inline-embedded using `file://` absolute paths — open directly in a browser, no HTTP server needed

**Full Batch Workflow:**

```bash
# 1. Batch inference (4 parallel workers, save logs)
python3 scripts/batch_infer.py \
  --video-dir ./test_videos \
  --output-dir ./output \
  --log-dir ./log \
  --workers 4 \
  --format markdown

# 2. Generate HTML evaluation report
python3 scripts/batch_evaluate.py \
  --video-dir ./test_videos \
  --report-dir ./output \
  --output ./evaluation_report.html \
  --single-class

# 3. (Optional) Generate Markdown table report
python3 scripts/batch_evaluate.py \
  --video-dir ./test_videos \
  --report-dir ./output \
  --output ./evaluation_report.md \
  --single-class
```

---

## Supported VLM Providers

- **Anthropic** (Claude) — default recommended
- **Google** (Gemini)
- **Aliyun** (Tongyi Qianwen)

Configure provider and API Key in `.env`. Multiple providers can be configured for automatic failover.

---

## Tool-Call Style Logging

Runtime output follows modern AI Agent tool-call trace style:

```
[INFO] 14:30:00 🔧 tool_call: video_preprocessor.process(video='clip.mp4')
[INFO] 14:30:03   ↳ result: coarse=20, precision=41 | elapsed=3.0s
[INFO] 14:30:03 🔧 tool_call: expert_agent.detect(event='Person Presence in Highway')
[INFO] 14:30:15   ↳ result: detected=true | elapsed=12.0s
[INFO] 14:30:15 🔧 tool_call: adjudication.resolve(candidates=4)
[INFO] 14:30:28   ↳ result: events=2, audit_entries=1 | elapsed=13.0s
```

Control granularity via `TRAFFIC_ANALYZER_TOOL_LOG_LEVEL`:

| Value | Behavior |
|---|---|
| `off` | No tool_call logs |
| `macro` | Top-level calls only |
| `mid` | Top-level + nested (default) |
| `fine` | Reserved for future expansion |

```bash
TRAFFIC_ANALYZER_TOOL_LOG_LEVEL=off python -m traffic_analyzer ...    # silent
TRAFFIC_ANALYZER_TOOL_LOG_LEVEL=macro python -m traffic_analyzer ...  # top only
```

This logging is a **pure display layer** — it does not affect parallelism, performance, or results. The binary encoding output is identical regardless of log level.

---

## Version Tags

| Tag | Branch | Description |
|---|---|---|
| `v4.0.0-far-enhancement` | `main` | **Current**. VLM multi-agent expert + adjudication architecture. Events 0-7 are active. Adds far-distance ROI evidence enhancement for event_id=3 (pedestrian), event_id=4 (non-motor vehicle), and event_id=6 (road construction) with continuous 0-1 ROI confidence. The tool-layer framework is retained but currently has no built-in tools. |
| `v2.0.0-multi-agent` | `legacy/v2.0` | Previous stable multi-agent architecture with 8 of 10 events active and a pure-VLM pipeline. |
| `v1.5.0-legacy` | `legacy/v1.5` | Monolithic architecture. SceneUnderstandingStep (~30s bottleneck) + mixed detection modes (direct_vlm parallel, logic_chain sequential, scene_tag zero-VLM) + PostProcessStep with cross-event inference. |

All new development happens on `main` (v4.0.0).
