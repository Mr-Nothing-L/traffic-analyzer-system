"""
Unit tests for traffic_analyzer.core.config_manager.ConfigManager.

Covers:
- Normal loading of YAML configs and .env overrides
- Validation pass / fail scenarios
- Hot reload semantics
- Graceful error handling for missing files
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from typing import Generator

import pytest
import yaml

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.models.schemas import DetectionMode, SystemConfig


@pytest.fixture
def temp_config_dir(tmp_path: Path) -> Path:
    """Return a temporary directory pre-populated with valid config files."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    event_categories = {
        "event_categories": [
            {
                "event_id": 0,
                "event_code": "A",
                "name": "Illegal Parking",
                "name_zh": "违法停车",
                "description": "Vehicle stopped illegally.",
                "detection_mode": "expert_agent",
                "prompt_template_id": "illegal_parking",
                "confidence_threshold": 0.7,
                "is_active": True,
            },
            {
                "event_id": 1,
                "event_code": "B",
                "name": "Emergency Lane Occupancy",
                "name_zh": "应急车道占用",
                "description": "Vehicle in emergency lane.",
                "detection_mode": "expert_agent",
                "prompt_template_id": "emergency_lane",
                "confidence_threshold": 0.7,
                "is_active": True,
            },
        ]
    }

    prompt_templates = {
        "prompt_templates": [
            {
                "template_id": "illegal_parking",
                "name": "Illegal Parking",
                "system_prompt": "Detect illegal parking.",
                "user_prompt": "Find illegally parked vehicles.",
            },
            {
                "template_id": "emergency_lane",
                "name": "Emergency Lane",
                "system_prompt": "Detect emergency lane usage.",
                "user_prompt": "Find vehicles in emergency lanes.",
            },
        ]
    }

    (config_dir / "event_categories.yaml").write_text(
        yaml.safe_dump(event_categories), encoding="utf-8"
    )
    (config_dir / "prompt_templates.yaml").write_text(
        yaml.safe_dump(prompt_templates), encoding="utf-8"
    )

    return config_dir


@pytest.fixture
def manager(temp_config_dir: Path) -> ConfigManager:
    """Return a ConfigManager instance backed by the temp config dir."""
    return ConfigManager(str(temp_config_dir))


# ---------------------------------------------------------------------------
# Loading tests
# ---------------------------------------------------------------------------


class TestLoadAll:
    def test_load_all_returns_system_config(self, manager: ConfigManager) -> None:
        config = manager.load_all()
        assert isinstance(config, SystemConfig)
        assert config.llm_provider.provider == "anthropic"

    def test_event_categories_loaded(self, manager: ConfigManager) -> None:
        manager.load_all()
        cats = manager.get_event_categories()
        assert len(cats) == 2
        assert cats[0].event_id == 0
        assert cats[0].detection_mode == DetectionMode.EXPERT_AGENT

    def test_prompt_template_lookup(self, manager: ConfigManager) -> None:
        manager.load_all()
        tmpl = manager.get_prompt_template("illegal_parking")
        assert tmpl.template_id == "illegal_parking"
        assert "Detect illegal parking" in tmpl.system_prompt

    def test_missing_prompt_template_raises_key_error(self, manager: ConfigManager) -> None:
        manager.load_all()
        with pytest.raises(KeyError, match="nonexistent_template"):
            manager.get_prompt_template("nonexistent_template")

    def test_missing_yaml_file_raises_file_not_found(self, tmp_path: Path) -> None:
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        mgr = ConfigManager(str(empty_dir))
        with pytest.raises(FileNotFoundError):
            mgr.load_all()


# ---------------------------------------------------------------------------
# .env parsing tests
# ---------------------------------------------------------------------------


class TestEnvParsing:
    @pytest.fixture(autouse=True)
    def _clear_env(self) -> Generator[None, None, None]:
        """Clear LLM_* environment variables before each test."""
        keys = [
            "VLM_PROVIDER",
            "LLM_PROVIDER",
            "LLM_API_KEY",
            "LLM_BASE_URL",
            "LLM_MODEL",
            "LLM_MAX_TOKENS",
            "LLM_TEMPERATURE",
            "LLM_TIMEOUT",
            "LLM_MAX_RETRIES",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_BASE_URL",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "GOOGLE_API_KEY",
            "ALIYUN_API_KEY",
            "ALIYUN_BASE_URL",
        ]
        preserved = {k: os.environ.pop(k, None) for k in keys}
        yield
        for k, v in preserved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_env_file_overrides_defaults(self, temp_config_dir: Path) -> None:
        env_content = textwrap.dedent(
            """\
            LLM_PROVIDER=openai
            LLM_API_KEY=sk-test-key
            LLM_BASE_URL=https://api.openai.com/v1
            LLM_MODEL=gpt-4o
            LLM_MAX_TOKENS=2048
            LLM_TEMPERATURE=0.5
            LLM_TIMEOUT=60.0
            LLM_MAX_RETRIES=5
            """
        )
        (temp_config_dir / ".env").write_text(env_content, encoding="utf-8")

        mgr = ConfigManager(str(temp_config_dir))
        config = mgr.load_all()

        assert config.llm_provider.provider == "openai"
        assert config.llm_provider.api_key == "sk-test-key"
        assert config.llm_provider.base_url == "https://api.openai.com/v1"
        assert config.llm_provider.model == "gpt-4o"
        assert config.llm_provider.max_tokens == 2048
        assert config.llm_provider.temperature == 0.5
        assert config.llm_provider.timeout == 60.0
        assert config.llm_provider.max_retries == 5

    def test_invalid_numeric_env_ignored(self, temp_config_dir: Path) -> None:
        (temp_config_dir / ".env").write_text(
            "LLM_MAX_TOKENS=not_a_number\n", encoding="utf-8"
        )
        mgr = ConfigManager(str(temp_config_dir))
        config = mgr.load_all()
        # Should fall back to default
        assert config.llm_provider.max_tokens == 4096


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestValidateConfig:
    def test_valid_config_returns_empty_errors(self, manager: ConfigManager) -> None:
        manager.load_all()
        errors = manager.validate_config()
        assert errors == []

    def test_missing_prompt_template_reference(self, temp_config_dir: Path) -> None:
        cats = {
            "event_categories": [
                {
                    "event_id": 0,
                    "event_code": "A",
                    "name": "Bad Category",
                    "name_zh": "错误类别",
                    "description": "Desc",
                    "detection_mode": "expert_agent",
                    "prompt_template_id": "missing_template",
                    "confidence_threshold": 0.7,
                    "is_active": True,
                }
            ]
        }
        (temp_config_dir / "event_categories.yaml").write_text(
            yaml.safe_dump(cats), encoding="utf-8"
        )

        mgr = ConfigManager(str(temp_config_dir))
        mgr.load_all()
        errors = mgr.validate_config()
        assert any("missing_template" in e for e in errors)


# ---------------------------------------------------------------------------
# Reload tests
# ---------------------------------------------------------------------------


class TestReload:
    def test_reload_picks_up_new_content(self, manager: ConfigManager, temp_config_dir: Path) -> None:
        manager.load_all()
        assert len(manager.get_event_categories()) == 2

        # Append a new category
        cats = {
            "event_categories": [
                {
                    "event_id": 0,
                    "event_code": "A",
                    "name": "Illegal Parking",
                    "name_zh": "违法停车",
                    "description": "Vehicle stopped illegally.",
                    "detection_mode": "expert_agent",
                    "prompt_template_id": "illegal_parking",
                    "confidence_threshold": 0.7,
                    "is_active": True,
                },
                {
                    "event_id": 1,
                    "event_code": "B",
                    "name": "Emergency Lane Occupancy",
                    "name_zh": "应急车道占用",
                    "description": "Vehicle in emergency lane.",
                    "detection_mode": "expert_agent",
                    "prompt_template_id": "emergency_lane",
                    "confidence_threshold": 0.7,
                    "is_active": True,
                },
                {
                    "event_id": 2,
                    "event_code": "C",
                    "name": "Traffic Accident",
                    "name_zh": "交通事故",
                    "description": "Collision.",
                    "detection_mode": "expert_agent",
                    "prompt_template_id": "accident",
                    "confidence_threshold": 0.7,
                    "is_active": True,
                },
            ]
        }
        (temp_config_dir / "event_categories.yaml").write_text(
            yaml.safe_dump(cats), encoding="utf-8"
        )

        manager.reload()
        assert len(manager.get_event_categories()) == 3

    def test_unloaded_manager_raises_on_getters(self, temp_config_dir: Path) -> None:
        mgr = ConfigManager(str(temp_config_dir))
        with pytest.raises(RuntimeError, match="load_all"):
            mgr.get_event_categories()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_yaml_lists(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        for fname in ("event_categories.yaml", "prompt_templates.yaml"):
            (config_dir / fname).write_text(
                yaml.safe_dump({fname.replace(".yaml", ""): []}), encoding="utf-8"
            )

        mgr = ConfigManager(str(config_dir))
        config = mgr.load_all()
        assert isinstance(config, SystemConfig)
        assert mgr.get_event_categories() == []

    def test_top_level_not_mapping_raises(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "event_categories.yaml").write_text(
            "- just\n- a\n- list\n", encoding="utf-8"
        )
        (config_dir / "prompt_templates.yaml").write_text(
            yaml.safe_dump({"prompt_templates": []}), encoding="utf-8"
        )

        mgr = ConfigManager(str(config_dir))
        with pytest.raises(ValueError, match="must be a mapping"):
            mgr.load_all()
