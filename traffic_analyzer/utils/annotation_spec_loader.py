"""Load annotation specification from YAML and generate prompt text.

The annotation spec (converted from XLSX) drives the adjudication prompt.
Editing the YAML is easier than editing the xlsx document.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


class AnnotationSpecLoader:
    """Loads annotation_spec.yaml and generates prompt-ready text."""

    def __init__(self, yaml_path: str) -> None:
        self.yaml_path = Path(yaml_path)
        self._data: Optional[Dict[str, Any]] = None
        self._load()

    def _load(self) -> None:
        if not self.yaml_path.exists():
            self._data = {"annotation_spec": {}}
            return
        with open(self.yaml_path, "r", encoding="utf-8") as f:
            self._data = yaml.safe_load(f) or {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def to_prompt_text(self) -> str:
        """Generate a single prompt-ready text block from the annotation spec."""
        parts: List[str] = []
        spec = self._data.get("annotation_spec", {}) if self._data else {}

        # Global guidelines
        guidelines = spec.get("global_guidelines", [])
        if guidelines:
            parts.append("=" * 60)
            parts.append("全局标注原则")
            parts.append("=" * 60)
            for g in guidelines:
                parts.append(f"- {g}")
            parts.append("")

        # Per-event definitions
        events = spec.get("events", [])
        if events:
            parts.append("=" * 60)
            parts.append("事件定义与边界条件")
            parts.append("=" * 60)
            parts.append("")

            for ev in events:
                eid = ev.get("event_id", "?")
                label = ev.get("action_label", "未知")
                desc = ev.get("description", "").strip()
                boundaries = ev.get("boundary_conditions", [])

                parts.append(f"--- 事件 {eid}: {label} ---")
                if desc:
                    parts.append(f"定义：{desc}")
                if boundaries:
                    parts.append("边界条件：")
                    for b in boundaries:
                        parts.append(f"  - {b}")
                parts.append("")

        return "\n".join(parts)

    def get_event_boundary_conditions(self, event_id: int) -> List[str]:
        """Return boundary conditions for a specific event."""
        spec = self._data.get("annotation_spec", {}) if self._data else {}
        for ev in spec.get("events", []):
            if ev.get("event_id") == event_id:
                return ev.get("boundary_conditions", [])
        return []

    def get_event_description(self, event_id: int) -> str:
        """Return description for a specific event."""
        spec = self._data.get("annotation_spec", {}) if self._data else {}
        for ev in spec.get("events", []):
            if ev.get("event_id") == event_id:
                return ev.get("description", "").strip()
        return ""
