# Multi-Agent Collaboration Architecture Design

**Date:** 2026-05-09  
**Status:** Draft — awaiting implementation  
**Related:** Plan A (Agent Context Memory), existing `logic_chain` detection mode

---

## 1. Background & Motivation

Current `traffic_analyzer` uses three detection modes (`direct_vlm`, `logic_chain`, `scene_tag`) with independent VLM calls. Complex events (E2 Traffic Accident, E7 Reversing/Wrong-way, E1 Emergency Lane) use `logic_chain` with fixed multi-step YAML definitions. Each VLM call re-uploads images from scratch.

**Goals of this redesign:**
- **Plan A:** Share a global conversation context across all VLM calls so images are uploaded only once
- **Multi-Agent:** Dedicate expert Agents to complex events, enabling dynamic negotiation and autonomous follow-up questions
- **YAML-driven:** All expert definitions live in configuration files, enabling easy addition of new experts
- **Domain transfer:** Swap the entire domain layer (`domains/<domain>/`) without touching framework code

---

## 2. High-Level Architecture

```
traffic_analyzer/
├── framework/                    # Domain-agnostic, written once
│   ├── supervisor.yaml           # Supervisor behavior definition
│   ├── expert_protocol.yaml      # Inter-Agent communication protocol
│   └── tools.yaml                # Tool layer configuration (optional)
│
├── domains/                      # Domain-specific, swappable
│   └── highway/                  # Current: highway traffic surveillance
│       ├── events.yaml           # Event definitions (replaces event_categories.yaml)
│       ├── experts/              # Expert Agent definitions
│       │   ├── reversing_expert.yaml
│       │   ├── emergency_expert.yaml
│       │   └── accident_expert.yaml
│       └── prompts/              # Domain-specific prompt templates
│
└── core/                         # Engine layer (becomes tool layer)
    ├── agent_context.py          # Shared global conversation memory
    ├── supervisor_agent.py       # SupervisorAgent implementation
    ├── expert_agent.py           # ExpertAgent base class
    ├── agent_engine.py           # Multi-Agent scheduling engine
    └── (existing engines)        # vlm_engine, logic_engine, ...
```

### 2.1 Three Layers

| Layer | Responsibility | Swappable? |
|---|---|---|
| **Framework** | Agent collaboration protocol, context management, tool interfaces | No |
| **Domain** | Event definitions, expert behaviors, prompt templates | Yes |
| **Core** | VLM calls, frame preprocessing, report generation | No (extends) |

---

## 3. Core Components

### 3.1 AgentContext — Shared Global Conversation

Maintains a single multi-turn conversation with the VLM. All frames are uploaded once; subsequent queries are text-only.

**Responsibilities:**
- Upload and index image groups (`coarse`, `precision`, `closeup`)
- Maintain `messages[]` history with sliding window compaction
- Tag messages with expert identity to prevent cross-expert interference
- Provide `query(expert_tag, prompt, require_new_images)` interface

**Sliding Window:** When message count exceeds `max_history_messages` (default 30), compress early non-critical history into a summary message.

```python
class AgentContext:
    def upload_frames(self, frames: list[Image], role: str) -> None
    def query(self, expert_tag: str, prompt: str,
              require_new_images: list[Image] | None = None) -> dict
    def compact_history(self, preserve_rounds: int = 5) -> None
```

### 3.2 SupervisorAgent

Central coordinator. Holds the `AgentContext`. Decides which experts to awaken based on preconditions.

**Workflow:**
1. **Round 1:** Upload all frames, run `scene_understanding`
2. **Evaluate preconditions:** For each expert in `domains/<domain>/experts/*.yaml`, evaluate `precondition.expression`
3. **Awaken experts:** Pass relevant context slices to awakened experts via `AgentContext`
4. **Handle `ask_supervisor`:** Respond to expert requests for additional frames or context
5. **Aggregate:** Collect `ExpertReport`s, run cross-event inference, generate final `AnalysisReport`

### 3.3 ExpertAgent

Per-event specialist. Loaded from YAML configuration.

**Behavior:**
1. Execute configured `behavior_chain` steps (vlm_call → condition → ...)
2. At any step, may issue `ask_supervisor` to request supplemental information
3. Return structured `ExpertReport` to Supervisor

**Autonomous capabilities:**
- `ask_supervisor(question, fallback_step)` — request help, with fallback if no response
- `get_frame_closeup(frame_indices)` — request specific frame close-ups
- `get_scene_field(field_path)` — request SceneInfo field values
- `query_other_expert(expert_id, question)` — consult another expert (future)

---

## 4. YAML Configuration Schema

### 4.1 Expert Definition

```yaml
# domains/highway/experts/reversing_expert.yaml
expert_id: "reversing_expert"
name: "Reversing/Wrong-way Detection Expert"
name_zh: "逆行/倒车检测专家"
target_events: [7]

precondition:
  expression: |
    scene_tags.get('静止车辆', '').startswith('有')
    or scene_tags.get('应急车道车辆', '').startswith('有')
    or event_results.get(0, {}).get('detected', False)
  description: "Only run when stopped/emergency-lane vehicles exist"

behavior_chain:
  - step_id: "detect"
    type: "vlm_call"
    prompt_template: "direct_reversing_detection"
    input_context:
      - "keyframes.coarse"
      - "scene_understanding"
    output_key: "reversing_result"
    response_schema:
      type: "object"
      required: ["vehicles", "detected", "confidence", "reasoning"]

  - step_id: "validate"
    type: "condition"
    condition: "reversing_result.detected"
    on_true: "build_result"
    on_false: "request_clarification"

  - step_id: "request_clarification"
    type: "ask_supervisor"
    question: |
      Pixel displacement is borderline ({reversing_result.pixel_displacement_estimate.magnitude_pct}%).
      Please provide close-up frames for frames #{start_frame} and #{end_frame}.
    fallback: "build_result"
    timeout_sec: 10

  - step_id: "build_result"
    type: "aggregate"
    mapping:
      detected: "${reversing_result.detected}"
      instances: "${reversing_result.vehicles}"
      confidence: "${reversing_result.confidence}"
      reasoning: "${reversing_result.reasoning}"
    output_key: "event_result"

available_tools:
  - "get_frame_closeup"
  - "get_scene_field"
  - "query_other_expert"

output_schema:
  type: "ExpertReport"
  fields:
    detected: "bool"
    instances: "List[EventInstance]"
    confidence: "float"
    reasoning: "string"
    expert_id: "string"
```

### 4.2 Supervisor Protocol

```yaml
# framework/supervisor.yaml
supervisor:
  scene_understanding:
    prompt_template: "scene_understanding"
    input_images: ["keyframes.coarse"]

  expert_dispatch_strategy: "conditional_awaken"  # vs "always_all" / "parallel_all"

  post_processing:
    cross_event_inference: true
    boolean_field_inference: true
    tag_based_inference: true

  context_sharing:
    mode: "global_shared"           # All experts share one AgentContext
    sliding_window:
      max_messages: 30
      preserve_last_n: 5
    expert_tag_prefix: "[Expert: {expert_id}]"
```

### 4.3 Expert Communication Protocol

```yaml
# framework/expert_protocol.yaml
protocol_version: "1.0"

message_types:
  - type: "awaken"
    from: "supervisor"
    to: "expert"
    payload:
      context_summary: "string"     # SceneInfo summary
      relevant_tags: "dict"         # Matched scene_tags that triggered precondition
      frame_roles: "list"           # Which frame groups are available

  - type: "ask_supervisor"
    from: "expert"
    to: "supervisor"
    payload:
      question: "string"
      requested_data_type: "enum[frame_closeup, scene_field, expert_opinion]"
      parameters: "dict"
      fallback_action: "string"     # Step ID to jump to if no response
      timeout_sec: "int"

  - type: "expert_report"
    from: "expert"
    to: "supervisor"
    payload:
      event_result: "EventResult"
      intermediate_steps: "list"    # For debugging/traceability
      token_usage: "dict"
```

---

## 5. Data Flow

```
┌──────────────────────────────────────────────────────────────┐
│  Phase 1: Initialization                                     │
│  Supervisor.upload_frames(coarse)                           │
│  Supervisor.upload_frames(precision)                        │
└──────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────┐
│  Phase 2: Scene Understanding (Round 1)                      │
│  AgentContext.query("[Supervisor]", scene_prompt)           │
│  → SceneInfo + scene_tags + direct_vlm/scene_tag results    │
└──────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────┐
│  Phase 3: Expert Dispatch                                    │
│  For each expert in domains/<domain>/experts/:              │
│    if evaluate(expert.precondition) == True:                │
│        expert = ExpertAgent.load(expert.yaml)               │
│        expert.awaken(context_summary, scene_tags)           │
└──────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────┐
│  Phase 4: Expert Execution (parallel where possible)         │
│  Expert 1: reversing_expert                                 │
│    → behavior_chain: detect → validate → [ask_supervisor]  │
│      → build_result → ExpertReport                          │
│  Expert 2: emergency_expert                                 │
│    → behavior_chain: ...                                    │
│  (All share same AgentContext, tagged messages)             │
└──────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────┐
│  Phase 5: Aggregation                                        │
│  Supervisor collects all ExpertReports                      │
│  → cross_event_inference                                    │
│  → report_generation (Markdown + Binary + JSON)             │
└──────────────────────────────────────────────────────────────┘
```

---

## 6. Domain Transfer

To switch from **highway** to **urban** traffic surveillance:

```bash
domains/
├── highway/                    # Existing
│   ├── events.yaml             # 10 events (E0-E9)
│   ├── experts/
│   │   ├── reversing_expert.yaml
│   │   ├── emergency_expert.yaml    # Emergency lane (highway only)
│   │   └── accident_expert.yaml
│   └── prompts/
│
└── urban/                      # New domain
    ├── events.yaml             # Different events (red light, illegal U-turn, ...)
    ├── experts/
    │   ├── reversing_expert.yaml      # Reuse with adapted prompt
    │   ├── red_light_expert.yaml      # New expert
    │   ├── illegal_u_turn_expert.yaml # New expert
    │   └── accident_expert.yaml       # Reuse
    └── prompts/
```

**Framework layer (`framework/`, `core/`) remains completely unchanged.**

---

## 7. Migration from Existing System

### 7.1 Backward Compatibility

- Existing `event_categories.yaml` → `domains/highway/events.yaml` (schema upgrade)
- Existing `logic_chains.yaml` → `domains/highway/experts/*.yaml` (one chain → one expert)
- Existing `prompt_templates.yaml` → `domains/highway/prompts/` (split by expert)
- `direct_vlm` and `scene_tag` events: remain as non-Agent detections handled by Supervisor directly

### 7.2 Migration Steps

1. Create `core/agent_context.py` (new)
2. Create `core/supervisor_agent.py` (new)
3. Create `core/expert_agent.py` (new)
4. Refactor `core/logic_engine.py` → extract chain execution logic for reuse by ExpertAgent
5. Create `framework/` and `domains/highway/` config directories
6. Migrate existing logic chains to expert YAMLs
7. Update `AnalysisOrchestrator` to delegate to `SupervisorAgent`
8. Add tests for AgentContext sliding window, expert dispatch, ask_supervisor protocol

---

## 8. Error Handling

| Scenario | Handling |
|---|---|
| Expert `ask_supervisor` timeout | Use `fallback` step; log warning |
| VLM returns malformed JSON | Retry with schema hint (existing retry logic) |
| Expert precondition throws | Log error, skip expert, continue with others |
| AgentContext history too long | Trigger `compact_history()`, preserve last 5 rounds |
| All experts fail | Return `detected=false` for their events with `confidence=0` |
| Domain config missing | Raise at startup with clear error message |

---

## 9. Testing Strategy

1. **Unit tests:** `AgentContext` message tagging, sliding window, frame indexing
2. **Unit tests:** `ExpertAgent` behavior chain execution, `ask_supervisor` emission
3. **Integration tests:** Full pipeline with mock VLM (recorded responses)
4. **Domain transfer test:** Load `domains/urban/` and verify correct expert set awakened
5. **Regression tests:** Existing event detection accuracy unchanged vs old logic_chain

---

## 10. Open Questions (for implementation phase)

1. Should `ask_supervisor` be synchronous (blocks expert) or asynchronous (expert continues with fallback)?
2. What is the `query_other_expert` latency budget? Should experts talk directly or through Supervisor?
3. Should we support expert-level `max_retries` and `timeout` overrides in YAML?
