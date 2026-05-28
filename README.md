[English](README.md) | [简体中文](README.zh-CN.md)

# Traffic Analyzer

A multi-modal large vision model (VLM) based traffic event detection framework for highway surveillance video. Supports **10 event categories**, outputs a 10-bit binary encoding plus a detailed Markdown analysis report. All event definitions, prompt templates, and adjudication rules are driven by YAML configuration — adding a new event requires zero code changes.

> **Current version: v2.0.0** — Multi-agent expert + adjudication architecture (see [Version Tags](#version-tags)).

---

## Architecture Overview (v2.0.0)

```
Video Input
    |
    v
1. Video Preprocessing
   - Coarse sampling + precision keyframe extraction
   - Two-stage sampling (dense early + uniform late)
    |
    v
2. ExpertAgentLayer (10 parallel ExpertAgents)
   Each ExpertAgent: single-event VLM call -> EventCandidate
   - Only fact identification (see it, report it)
   - No filtering or exclusion logic
    |
    v
3. AdjudicationStep (single VLM call)
   Input: all EventCandidates + keyframes + business rules
   Output: final EventResults + AuditLog
   - Resolves conflicts (e.g. accident suppresses parking)
   - Applies business rules from YAML
    |
    v
4. Report Generation
   - Markdown report (human-readable, per-step timing)
   - Binary encoding {bit_0_bit_1_..._bit_9}
   - Audit log of every inclusion / exclusion decision
```

**Key improvement over v1.5**: Instead of a single ~30s scene-understanding bottleneck followed by mixed detection modes, all 10 events are detected in parallel by dedicated expert agents, then a single adjudication call resolves conflicts using explicit business rules. This is more accurate, more auditable, and easier to tune.

---

## Supported Events

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
| 8 | J | Thrown Objects | true |
| 9 | K | Lane Change over Solid Line | false |

All events use `detection_mode: "expert_agent"` in v2.0.0.

---

## Key Features

### 1. Expert Agent Layer

Each active event gets its own **ExpertAgent** — a dedicated VLM call with a specialized prompt. Agents run in parallel via `ThreadPoolExecutor`. Each agent only performs **fact identification** (what it sees) without any filtering. This separation of concerns makes the system modular and debuggable.

### 2. Adjudication Step

A **single VLM call** receives all expert candidates, keyframes, and business rules, then outputs:
- Final `EventResult` for each event (detected / not detected)
- `AuditLog` recording every inclusion / exclusion decision with reasoning
- `adjudication_reasoning` explaining the overall decision process

Business rules are defined in `event_categories.yaml` under `adjudication_rules:`. Example rules:
- **Accident suppresses parking** — stationary vehicles in an accident scene are part of the accident, not illegal parking
- **Construction excludes emergency lane** — vehicles inside a construction zone are not emergency lane violations
- **Motorcycle excludes emergency lane** — a motorcycle on the shoulder is tagged as "motorcycle presence", not "emergency lane occupancy"

### 3. Audit Log

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

### 4. Config-Driven Design

All of the following are defined in YAML — no code changes needed:
- Event definitions (`event_categories.yaml`)
- Prompt templates (`prompt_templates.yaml`)
- Adjudication rules (`event_categories.yaml`)
- Legacy logic chains (`logic_chains.yaml`, kept for reference)

---

## Project Structure

```
traffic_analyzer/
├── config/
│   ├── event_categories.yaml      # Event definitions + adjudication_rules
│   ├── logic_chains.yaml          # Legacy logic chains (kept for reference)
│   └── prompt_templates.yaml      # VLM Prompt templates + adjudication template
├── core/
│   ├── config_manager.py          # Config loading, validation
│   ├── expert_agent.py            # Single-event detection agent
│   ├── logic_engine.py            # Logic chain execution engine
│   ├── pipeline_steps.py          # ExpertAgentLayer + AdjudicationStep
│   ├── report_generator.py        # Report generation
│   ├── video_preprocessor.py      # Video frame extraction
│   └── vlm_engine.py              # VLM wrapper (multi-provider + cache)
├── models/
│   └── schemas.py                 # Pydantic models (EventCandidate, AdjudicationResult, AuditEntry)
├── orchestrator/
│   └── analysis_orchestrator.py   # 4-step pipeline orchestrator
├── utils/
│   └── event_detection.py         # Image selection + response parsing helpers
└── config/
    └── .env                       # LLM provider config (API Key, etc.)
```

---

## Quick Start

### 1. Configure LLM Provider

```bash
cp traffic_analyzer/config/.env.example traffic_analyzer/config/.env
# Edit .env, set API Key and model
```

Supported environment variables:

| Variable | Description | Default |
|---|---|---|
| `LLM_PROVIDER` | VLM provider (`anthropic` / `google` / `aliyun`) | `anthropic` |
| `LLM_API_KEY` | API Key | - |
| `LLM_MODEL` | Model name | `claude-sonnet-4-6` |
| `LLM_MAX_TOKENS` | Max output tokens | `4096` |
| `LLM_TEMPERATURE` | Sampling temperature | `0.2` |
| `LLM_TIMEOUT` | API timeout (seconds) | `120` |
| `LLM_MAX_RETRIES` | Max retry count | `3` |
| `LLM_ENABLE_CACHE` | Enable VLM result cache | `true` |
| `LLM_CACHE_MAX_SIZE` | Max cache entries | `128` |
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
# Basic usage (default 30 frames)
python3 -m traffic_analyzer analyze \
  --video ./path/to/video.mp4 \
  --format markdown \
  --output ./report.md

# Fast mode (10 frames, for short videos / testing)
python3 -m traffic_analyzer analyze \
  --video ./path/to/video.mp4 \
  --format markdown \
  --output ./report.md \
  --min-frames 10

# With CV track cross-validation
python3 -m traffic_analyzer analyze \
  --video ./path/to/video.mp4 \
  --cv-tracks ./tracks.json \
  --format markdown \
  --output ./report.md
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
| `--cv-tracks-dir` | CV track JSON directory (optional) | - |
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

Configure provider and API Key in `.env`.

---

## Tool-Call Style Logging

Runtime output follows modern AI Agent tool-call trace style:

```
[INFO] 14:30:00 🔧 tool_call: video_preprocessor.process(video='clip.mp4')
[INFO] 14:30:03   ↳ result: coarse=20, precision=41 | elapsed=3.0s
[INFO] 14:30:03 🔧 tool_call: expert_agent.detect(event='Illegal Parking')
[INFO] 14:30:15   ↳ result: detected=true, confidence=0.92 | elapsed=12.0s
[INFO] 14:30:15 🔧 tool_call: adjudication.resolve(candidates=10)
[INFO] 14:30:28   ↳ result: events=3, audit_entries=2 | elapsed=13.0s
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
| `v2.0.0-multi-agent` | `main` | **Current**. Multi-agent expert + adjudication architecture. All 10 events use `expert_agent` mode. Parallel detection + single VLM adjudication with business rules. |
| `v1.5.0-legacy` | `legacy/v1.5` | Monolithic architecture. SceneUnderstandingStep (~30s bottleneck) + mixed detection modes (direct_vlm parallel, logic_chain sequential, scene_tag zero-VLM) + PostProcessStep with cross-event inference. |

The `legacy/v1.5` branch preserves the old architecture for reference and comparison. All new development happens on `main` (v2.0.0).

---

## Custom Tool Development Guide

The system supports extending custom tools via the **Tool Definition Layer (Tool Schema)** + **Tool Router Layer (Tool Router)** architecture, for use by Expert Agents.

### Architecture Overview

```
Model outputs ToolRequest (JSON/Markdown/XML)
        ↓
ToolRouter.route() → Parse and validate parameters
        ↓
Match ToolDefinition → Execute handler
        ↓
Return ToolResponse (success/data/error + elapsed time)
```

### Adding a New Tool (3 Steps)

#### Step 1: Implement the Tool Function

Create a new file under `traffic_analyzer/tools/`:

```python
# traffic_analyzer/tools/my_new_tool.py

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def my_new_tool(
    video_path: str,
    param1: float = 0.5,
    param2: Optional[str] = None,
) -> dict:
    """Tool implementation. Returns dict, wrapped by ToolResponse."""
    logger.info(f"my_new_tool: video={video_path}, param1={param1}")
    
    # ... your logic ...
    
    return {
        "success": True,
        "result": "something",
        "detail": {"param1": param1, "param2": param2},
    }
```

**Key constraints**:
- Parameter names use snake_case, must exactly match `ToolParameter.name` at registration
- Return value must be JSON-serializable (dict/list/str/int/float/bool/None)
- Function can be sync or async (async def), Router auto-detects

#### Step 2: Register in Tool Router Layer

Edit `traffic_analyzer/tools/tool_registry.py`, add registration in `create_router()`:

```python
def create_router() -> ToolRouter:
    router = ToolRouter()
    
    # Existing tools
    _register_yolo_track_tool(router)
    
    # New tool
    _register_my_new_tool(router)
    
    return router
```

Then implement the registration function:

```python
from .my_new_tool import my_new_tool
from .tool_schema import ParameterType, ToolConstraint, ToolDefinition, ToolParameter, ToolReturn

def _register_my_new_tool(router: ToolRouter) -> None:
    definition = ToolDefinition(
        name="my_new_tool",  # Model calls by this name
        description="Detailed description of what the tool does, for the model. At least 10 chars. Explain purpose, applicable scenarios, input/output.",
        parameters=[
            ToolParameter(
                name="video_path",
                type=ParameterType.STRING,
                description="Absolute path to input video (container path)",
                constraints=ToolConstraint(
                    required=True,
                    pattern=r"^/.*\.(mp4|avi|mov|mkv)$",
                ),
            ),
            ToolParameter(
                name="param1",
                type=ParameterType.FLOAT,
                description="Description of param1",
                constraints=ToolConstraint(
                    required=False,
                    min_value=0.0,
                    max_value=1.0,
                ),
                default=0.5,
            ),
            ToolParameter(
                name="param2",
                type=ParameterType.STRING,
                description="Description of param2",
                constraints=ToolConstraint(required=False),
                default=None,
            ),
        ],
        returns=ToolReturn(
            type=ParameterType.OBJECT,
            description="Detailed description of return value, helping the model understand how to use the result",
        ),
    )
    
    router.register(definition, my_new_tool)
    logger.info("my_new_tool registered")
```

**Supported parameter constraints**:

| Constraint | Description | Applicable Types |
|---|---|---|
| `required` | Whether the parameter is required | All |
| `min_value` / `max_value` | Numeric range | integer, float |
| `min_length` / `max_length` | Length limit | string, array |
| `pattern` | Regex pattern | string |
| `enum_values` | Enumerated values | All |
| `items_type` | Array element type | array |

#### Step 3: Write Tests

Add tests under `tests/tools/`:

```python
# tests/tools/test_my_new_tool.py

import pytest
from traffic_analyzer.tools.tool_registry import create_router


def test_my_new_tool_registration():
    """Verify tool is registered"""
    router = create_router()
    assert "my_new_tool" in router.list_tools()
    
    # Verify parameter definitions
    definition = router.get_tool("my_new_tool")
    param_names = [p.name for p in definition.parameters]
    assert "video_path" in param_names


def test_my_new_tool_execution():
    """Verify tool executes correctly"""
    router = create_router()
    resp = router.route(
        '{"tool_name": "my_new_tool", "arguments": {"video_path": "/data/test.mp4", "param1": 0.8}}'
    )
    assert resp.success is True
    assert resp.data["success"] is True


def test_my_new_tool_validation_error():
    """Verify parameter validation works"""
    router = create_router()
    # Missing required parameter
    resp = router.route('{"tool_name": "my_new_tool", "arguments": {}}')
    assert resp.success is False
    assert "Missing required parameter" in resp.error
```

Run tests:

```bash
docker-compose exec traffic-agent python3 -m pytest tests/tools/test_my_new_tool.py -v
```

### How the Model Calls Tools

The Expert Agent's Prompt is injected with available tool JSON Schemas (via `ToolRouter.get_tool_descriptions(format="json")`). The model outputs a call request in this format:

```json
{
    "tool_name": "my_new_tool",
    "arguments": {
        "video_path": "/data/test_videos/clip.mp4",
        "param1": 0.8
    }
}
```

Markdown code blocks and XML tags are also supported; the Router parses them automatically.

### Full Example: Video Frame Extraction Tool

See `traffic_analyzer/tools/yolo_track_tool.py` (YOLOv8 + ByteTrack tracking tool) for a complete reference implementation, including:
- 6 parameter definitions (video_path, conf_threshold, stationary_threshold, etc.)
- Pixel-level direction description (`dx=+150px, dy=-80px (toward bottom-right)`)
- Detailed error fallback logging
- 12 unit tests, all passing

### Tool Layer File Reference

| File | Description |
|---|---|
| `traffic_analyzer/tools/tool_schema.py` | Tool Definition Layer: ToolDefinition, ToolParameter, ToolConstraint, ToolRegistry |
| `traffic_analyzer/tools/tool_router.py` | Tool Router Layer: ToolRequest, ToolResponse, ToolRouter (sync/async/batch) |
| `traffic_analyzer/tools/tool_registry.py` | Registration Integration: Default Router singleton, registers all built-in tools |
| `traffic_analyzer/tools/yolo_track_tool.py` | Example Tool: YOLOv8 + ByteTrack object tracking |
| `tests/tools/test_tool_router.py` | Router Tests: 30 tests covering parsing/validation/execution/error handling |
| `tests/tools/test_yolo_track_tool.py` | YOLO Tool Tests: 12 tests covering tracking/displacement/direction/integration |
