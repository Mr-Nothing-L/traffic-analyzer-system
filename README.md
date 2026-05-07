# Traffic Analyzer

A video-based traffic event detection system powered by Vision-Language Models (VLM). The system analyzes video clips to detect traffic incidents across multiple event categories using a configurable multi-stage pipeline.

## Supported Events

| ID | Event Name | Detection Mode |
|---|---|---|
| 0 | 违法停车 (Illegal Parking) | `direct_vlm` |
| 1 | 应急车道占用 (Emergency Lane Occupancy) | `logic_chain` |
| 2 | 交通事故 (Traffic Accident) | `logic_chain` |
| 3 | 高速公路行人出现 (Pedestrian on Highway) | `scene_tag` |
| 4 | 摩托车出现 (Motorcycle on Highway) | `scene_tag` |
| 5 | 严重拥堵 (Severe Congestion) | `direct_vlm` |
| 6 | 道路施工 (Road Construction) | `direct_vlm` |
| 7 | 车辆逆行/倒车 (Reversing/Wrong-Way Driving) | `logic_chain` |
| 8 | 抛洒物 (Thrown Object on Road) | `direct_vlm` |
| 9 | 实线变道 (Solid Line Lane Change) | `logic_chain` |

## Three Detection Modes

### 1. `direct_vlm` — Direct VLM Detection

**Events:** 违法停车 (0), 严重拥堵 (5), 道路施工 (6), 抛洒物 (8)

A single direct VLM call is made with the event definition and visual indicators. The VLM examines the video frames and returns whether the event is present.

- **Pros:** Simple, one-shot detection
- **Cons:** Can misinterpret scene elements (e.g., construction vehicles as accident scenes), prone to false positives

### 2. `logic_chain` — Multi-Step Logic Chain Detection

**Events:** 应急车道占用 (1), 交通事故 (2), 车辆逆行/倒车 (7), 实线变道 (9)

A configurable YAML-defined logic chain with multiple steps (`vlm_call`, `compute`, `condition`, `aggregate`). The chain may call the VLM multiple times for different sub-tasks.

- **Example:** Reversing detection uses a 3-step chain (VLM call → condition check → aggregate result)
- **Pros:** Structured reasoning, can incorporate prior knowledge, multi-step verification
- **Cons:** More VLM calls = higher latency, potential for parse errors at each step

### 3. `scene_tag` — Scene Tag Inference (No VLM Call)

**Events:** 高速公路行人出现 (3), 摩托车出现 (4)

No VLM call is made at all. The result is determined entirely from the `scene_understanding` output, specifically:

- Structured boolean fields: `pedestrian_present`, `non_motor_vehicle_present`, `thrown_object_present`
- Structured tags in `scene_description`: e.g., `{行人：无}`, `{非机动车：有}`, `{交通事故：无}`

The `scene_understanding` step has already spent significant tokens analyzing the scene comprehensively, so these structured fields are highly reliable.

- **Pros:** Zero additional VLM calls, fastest, most reliable for clear presence/absence events
- **Cons:** Only works for events that can be determined from overall scene analysis

## Mode Selection Guide

| Scenario | Recommended Mode |
|---|---|
| Unambiguous presence/absence events (pedestrian, motorcycle, thrown objects, accident yes/no) | `scene_tag` |
| Events requiring multi-step reasoning or spatial tracking (reversing, lane change, emergency lane occupancy) | `logic_chain` |
| Events requiring direct visual evidence evaluation (illegal parking, congestion, construction) | `direct_vlm` |

## Configuration

Detection mode is set per event category in `traffic_analyzer/config/event_categories.yaml`:

```yaml
detection_mode: "direct_vlm"  # or "logic_chain" or "scene_tag"
```

For `logic_chain` mode, also specify `logic_chain_id` referencing a chain defined in `traffic_analyzer/config/logic_chains.yaml`.

## Architecture Overview

The pipeline consists of five stages:

```
Video Input
    |
    v
1. Video Preprocessing
   - Coarse frame extraction (uniform sampling)
   - Precision frame extraction (adaptive sampling for key moments)
    |
    v
2. Scene Understanding
   - Single comprehensive VLM call
   - Extracts structured tags: pedestrian_present, non_motor_vehicle_present,
     thrown_object_present, and scene_description tags
    |
    v
3. Event Detection (per category)
   - direct_vlm: One-shot VLM call with event-specific prompt
   - logic_chain: Multi-step chain from logic_chains.yaml
   - scene_tag: Direct inference from scene_understanding output
    |
    v
4. Post-Processing
   - Scene tag inference as fallback/refinement
   - Confidence scoring and result consolidation
    |
    v
5. Report Generation
   - Markdown report (human-readable)
   - JSON report (machine-readable)
```

## Project Structure

```
traffic_analyzer/
├── config/
│   ├── event_categories.yaml   # Event definitions and detection modes
│   └── logic_chains.yaml       # Multi-step logic chain definitions
├── core/
│   ├── preprocessing.py        # Frame extraction
│   ├── scene_understanding.py  # Comprehensive scene analysis
│   ├── detection/
│   │   ├── direct_vlm.py       # Direct VLM detection
│   │   ├── logic_chain.py      # Multi-step logic chain engine
│   │   └── scene_tag.py        # Scene tag inference
│   └── post_processing.py      # Result refinement
└── reports/
    ├── markdown_generator.py   # Markdown report generation
    └── json_generator.py       # JSON report generation
```
