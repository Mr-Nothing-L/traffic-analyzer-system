"""
Unit tests for traffic_analyzer.core.config_manager.ConfigManager.

Covers:
- Normal loading of YAML configs and .env overrides
- Validation pass / fail scenarios
- Hot reload semantics
- Graceful error handling for missing files

[文件说明]
作用:测试 ConfigManager 的 YAML 加载、.env 覆盖、配置校验通过/失败、热重载及缺失文件容错。
上游:pytest 自动发现并执行本文件测试。
下游:traffic_analyzer/core/config_manager.py(被测模块)。
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from typing import Generator

import pytest
import yaml

from traffic_analyzer.core.config_manager import ConfigManager, ConfigValidationError
from traffic_analyzer.models.schemas import DetectionMode, LLMProviderConfig, SystemConfig


@pytest.fixture
def temp_config_dir(tmp_path: Path) -> Path:
    """Return a temporary directory pre-populated with valid config files."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    event_categories = {
        "event_categories": [
            {
                "event_id": 1,
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
                "event_id": 2,
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

    annotation_spec = {
        "annotation_spec": {
            "version": "1.0",
            "events": [
                {
                    "event_id": 1,
                    "action_label": "机动车违停",
                    "description": "车辆在道路上静止。",
                    "boundary_conditions": ["只针对机动车"],
                },
                {
                    "event_id": 2,
                    "action_label": "机动车占用应急车道",
                    "description": "车辆占用应急车道。",
                    "boundary_conditions": ["只针对机动车"],
                },
            ],
        }
    }

    (config_dir / "event_categories.yaml").write_text(
        yaml.safe_dump(event_categories), encoding="utf-8"
    )
    prompts_dir = config_dir / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "default.yaml").write_text(
        yaml.safe_dump(prompt_templates), encoding="utf-8"
    )
    (config_dir / "annotation_spec.yaml").write_text(
        yaml.safe_dump(annotation_spec), encoding="utf-8"
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
        assert cats[0].event_id == 1
        assert cats[0].detection_mode == DetectionMode.EXPERT_AGENT

    def test_active_event_categories_and_total(self, tmp_path: Path) -> None:
        """Active-only helpers must respect is_active and preserve total count."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        event_categories = {
            "event_categories": [
                {
                    "event_id": 1,
                    "event_code": "A",
                    "name": "Active A",
                    "name_zh": "活跃A",
                    "description": "Active event.",
                    "detection_mode": "expert_agent",
                    "prompt_template_id": "tpl",
                    "is_active": True,
                },
                {
                    "event_id": 2,
                    "event_code": "B",
                    "name": "Inactive B",
                    "name_zh": "未激活B",
                    "description": "Inactive event.",
                    "detection_mode": "expert_agent",
                    "prompt_template_id": "tpl",
                    "is_active": False,
                },
                {
                    "event_id": 3,
                    "event_code": "C",
                    "name": "Active C",
                    "name_zh": "活跃C",
                    "description": "Active event.",
                    "detection_mode": "expert_agent",
                    "prompt_template_id": "tpl",
                    "is_active": True,
                },
            ]
        }
        prompt_templates = {
            "prompt_templates": [
                {
                    "template_id": "tpl",
                    "name": "TPL",
                    "system_prompt": "s",
                    "user_prompt": "u",
                }
            ]
        }
        (config_dir / "event_categories.yaml").write_text(
            yaml.safe_dump(event_categories), encoding="utf-8"
        )
        prompts_dir = config_dir / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "default.yaml").write_text(
            yaml.safe_dump(prompt_templates), encoding="utf-8"
        )

        mgr = ConfigManager(str(config_dir))
        mgr.load_all()

        all_cats = mgr.get_event_categories()
        active_cats = mgr.get_active_event_categories()

        assert len(all_cats) == 3
        assert len(active_cats) == 2
        assert [cat.event_id for cat in active_cats] == [1, 3]
        assert mgr.get_total_event_categories() == 3

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
            "LLM_ENABLE_CACHE",
            "LLM_CACHE_MAX_SIZE",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_MODEL",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_MODEL",
            "GOOGLE_API_KEY",
            "ALIYUN_API_KEY",
            "ALIYUN_BASE_URL",
            "ALIYUN_MODEL",
            "LLM_PROVIDER_0_PROVIDER",
            "LLM_PROVIDER_0_API_KEY",
            "LLM_PROVIDER_0_BASE_URL",
            "LLM_PROVIDER_0_MODEL",
            "LLM_PROVIDER_0_MAX_TOKENS",
            "LLM_PROVIDER_0_TEMPERATURE",
            "LLM_PROVIDER_0_TIMEOUT",
            "LLM_PROVIDER_0_MAX_RETRIES",
            "LLM_PROVIDER_1_PROVIDER",
            "LLM_PROVIDER_1_API_KEY",
            "LLM_PROVIDER_1_BASE_URL",
            "LLM_PROVIDER_1_MODEL",
            "LLM_PROVIDER_1_MAX_TOKENS",
            "LLM_PROVIDER_1_TEMPERATURE",
            "LLM_PROVIDER_1_TIMEOUT",
            "LLM_PROVIDER_1_MAX_RETRIES",
            "SCENE_UNDERSTANDING_MIN_FRAMES",
            "VLM_MAX_FRAMES",
            "SFT_LABEL_ENABLE",
            "SFT_LABEL_OUTPUT_DIR",
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

    def test_legacy_single_env_becomes_first_provider(self, temp_config_dir: Path) -> None:
        """Legacy LLM_* variables must be available as llm_providers[0]."""
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

        assert len(config.llm_providers) == 1
        assert config.llm_providers[0].provider == "openai"
        assert config.llm_providers[0].api_key == "sk-test-key"
        assert config.llm_providers[0].base_url == "https://api.openai.com/v1"
        assert config.llm_providers[0].model == "gpt-4o"
        assert config.llm_providers[0].max_tokens == 2048
        assert config.llm_providers[0].temperature == 0.5
        assert config.llm_providers[0].timeout == 60.0
        assert config.llm_providers[0].max_retries == 5

        # Backwards-compatible accessors
        assert config.llm_provider.provider == "openai"
        assert mgr.get_llm_provider().provider == "openai"
        assert len(mgr.get_llm_providers()) == 1

    def test_multi_provider_env_parsing(self, temp_config_dir: Path) -> None:
        """Indexed LLM_PROVIDER_N_* variables must create multiple providers."""
        env_content = textwrap.dedent(
            """\
            LLM_PROVIDER_0_PROVIDER=openai
            LLM_PROVIDER_0_API_KEY=sk-openai
            LLM_PROVIDER_0_BASE_URL=https://api.openai.com/v1
            LLM_PROVIDER_0_MODEL=gpt-4o
            LLM_PROVIDER_0_MAX_TOKENS=2048
            LLM_PROVIDER_0_TEMPERATURE=0.1
            LLM_PROVIDER_0_TIMEOUT=30.0
            LLM_PROVIDER_0_MAX_RETRIES=2
            LLM_PROVIDER_1_PROVIDER=anthropic
            LLM_PROVIDER_1_API_KEY=sk-anthropic
            LLM_PROVIDER_1_BASE_URL=https://api.anthropic.com/v1
            LLM_PROVIDER_1_MODEL=claude-sonnet-4
            LLM_PROVIDER_1_MAX_TOKENS=8192
            LLM_PROVIDER_1_TEMPERATURE=0.3
            LLM_PROVIDER_1_TIMEOUT=120.0
            LLM_PROVIDER_1_MAX_RETRIES=4
            """
        )
        (temp_config_dir / ".env").write_text(env_content, encoding="utf-8")

        mgr = ConfigManager(str(temp_config_dir))
        config = mgr.load_all()

        assert len(config.llm_providers) == 2

        p0 = config.llm_providers[0]
        assert p0.provider == "openai"
        assert p0.api_key == "sk-openai"
        assert p0.base_url == "https://api.openai.com/v1"
        assert p0.model == "gpt-4o"
        assert p0.max_tokens == 2048
        assert p0.temperature == 0.1
        assert p0.timeout == 30.0
        assert p0.max_retries == 2

        p1 = config.llm_providers[1]
        assert p1.provider == "anthropic"
        assert p1.api_key == "sk-anthropic"
        assert p1.base_url == "https://api.anthropic.com/v1"
        assert p1.model == "claude-sonnet-4"
        assert p1.max_tokens == 8192
        assert p1.temperature == 0.3
        assert p1.timeout == 120.0
        assert p1.max_retries == 4

        # Primary provider is the first one
        assert config.llm_provider == p0
        assert mgr.get_llm_provider() == p0

    def test_sparse_provider_indices_create_no_phantom(self, temp_config_dir: Path) -> None:
        """Only LLM_PROVIDER_1_* set must not fabricate a phantom index-0 provider."""
        env_content = textwrap.dedent(
            """\
            LLM_PROVIDER_1_PROVIDER=openai
            LLM_PROVIDER_1_API_KEY=sk-only
            LLM_PROVIDER_1_BASE_URL=https://api.openai.com/v1
            LLM_PROVIDER_1_MODEL=gpt-4o
            """
        )
        (temp_config_dir / ".env").write_text(env_content, encoding="utf-8")

        mgr = ConfigManager(str(temp_config_dir))
        config = mgr.load_all()

        assert len(config.llm_providers) == 1
        assert config.llm_providers[0].provider == "openai"
        assert config.llm_providers[0].api_key == "sk-only"
        assert config.llm_provider.provider == "openai"

    def test_invalid_frame_count_env_falls_back_to_default(self, temp_config_dir: Path) -> None:
        """Non-integer SCENE_UNDERSTANDING_MIN_FRAMES/VLM_MAX_FRAMES must not crash load."""
        (temp_config_dir / ".env").write_text(
            "SCENE_UNDERSTANDING_MIN_FRAMES=abc\nVLM_MAX_FRAMES=xyz\n", encoding="utf-8"
        )

        mgr = ConfigManager(str(temp_config_dir))
        config = mgr.load_all()

        assert config.scene_understanding_min_frames == 10
        assert config.vlm_max_frames == 10

    def test_sft_label_defaults(self, temp_config_dir: Path) -> None:
        """SFT label rewrite is opt-in: disabled by default with the default output dir."""
        # Empty .env keeps the project-root .env fallback from leaking overrides.
        (temp_config_dir / ".env").write_text("", encoding="utf-8")
        mgr = ConfigManager(str(temp_config_dir))
        config = mgr.load_all()

        assert config.sft_label_enabled is False
        assert config.sft_label_output_dir == "output/sft_labels"

    def test_sft_label_env_overrides(self, temp_config_dir: Path) -> None:
        """SFT_LABEL_ENABLE / SFT_LABEL_OUTPUT_DIR env vars must be honored."""
        (temp_config_dir / ".env").write_text(
            "SFT_LABEL_ENABLE=true\nSFT_LABEL_OUTPUT_DIR=/tmp/sft_out\n",
            encoding="utf-8",
        )

        mgr = ConfigManager(str(temp_config_dir))
        config = mgr.load_all()

        assert config.sft_label_enabled is True
        assert config.sft_label_output_dir == "/tmp/sft_out"


# ---------------------------------------------------------------------------
# SystemConfig model validator tests
# ---------------------------------------------------------------------------


class TestSystemConfigValidator:
    def test_both_provider_fields_list_wins(self, caplog) -> None:
        """When both llm_provider and llm_providers are provided, the list wins."""
        single = LLMProviderConfig(provider="openai", api_key="old-key")
        multi = [LLMProviderConfig(provider="anthropic", api_key="new-key")]

        with caplog.at_level("WARNING"):
            config = SystemConfig(llm_provider=single, llm_providers=multi)

        assert len(config.llm_providers) == 1
        assert config.llm_providers[0].provider == "anthropic"
        assert config.llm_providers[0].api_key == "new-key"
        assert config.llm_provider.provider == "anthropic"
        assert "llm_providers takes precedence" in caplog.text


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
                    "event_id": 1,
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
        annotation_spec = {
            "annotation_spec": {
                "version": "1.0",
                "events": [
                    {
                        "event_id": 1,
                        "action_label": "Bad Category",
                        "description": "Desc",
                        "boundary_conditions": [],
                    }
                ],
            }
        }
        (temp_config_dir / "event_categories.yaml").write_text(
            yaml.safe_dump(cats), encoding="utf-8"
        )
        (temp_config_dir / "annotation_spec.yaml").write_text(
            yaml.safe_dump(annotation_spec), encoding="utf-8"
        )

        mgr = ConfigManager(str(temp_config_dir))
        mgr.load_all()
        errors = mgr.validate_config()
        assert any("missing_template" in e for e in errors)

    def test_annotation_spec_event_id_mismatch(self, temp_config_dir: Path) -> None:
        annotation_spec = {
            "annotation_spec": {
                "version": "1.0",
                "events": [
                    {
                        "event_id": 1,
                        "action_label": "机动车违停",
                        "description": "desc",
                        "boundary_conditions": [],
                    },
                    {
                        "event_id": 99,
                        "action_label": "不存在的事件",
                        "description": "desc",
                        "boundary_conditions": [],
                    },
                ],
            }
        }
        (temp_config_dir / "annotation_spec.yaml").write_text(
            yaml.safe_dump(annotation_spec), encoding="utf-8"
        )

        mgr = ConfigManager(str(temp_config_dir))
        mgr.load_all()
        with pytest.raises(ConfigValidationError, match="event IDs do not match"):
            mgr.validate_config()


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
                    "event_id": 1,
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
                    "event_id": 2,
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
                    "event_id": 3,
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
        (config_dir / "event_categories.yaml").write_text(
            yaml.safe_dump({"event_categories": []}), encoding="utf-8"
        )
        prompts_dir = config_dir / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "empty.yaml").write_text(
            yaml.safe_dump({"prompt_templates": []}), encoding="utf-8"
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
        prompts_dir = config_dir / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "empty.yaml").write_text(
            yaml.safe_dump({"prompt_templates": []}), encoding="utf-8"
        )

        mgr = ConfigManager(str(config_dir))
        with pytest.raises(ValueError, match="must be a mapping"):
            mgr.load_all()

    def test_malformed_yaml_raises(self, tmp_path: Path) -> None:
        """Syntax-broken YAML must fail fast, not silently load empty config."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        prompts_dir = config_dir / "prompts"
        prompts_dir.mkdir()

        # Broken event_categories.yaml
        (config_dir / "event_categories.yaml").write_text(
            "event_categories: [\n  - unclosed bracket\n", encoding="utf-8"
        )
        (prompts_dir / "empty.yaml").write_text(
            yaml.safe_dump({"prompt_templates": []}), encoding="utf-8"
        )

        mgr = ConfigManager(str(config_dir))
        with pytest.raises(yaml.YAMLError):
            mgr.load_all()

        # Broken prompts yaml
        (config_dir / "event_categories.yaml").write_text(
            yaml.safe_dump({"event_categories": []}), encoding="utf-8"
        )
        (prompts_dir / "empty.yaml").write_text(
            "prompt_templates: [\n  - unclosed bracket\n", encoding="utf-8"
        )

        mgr = ConfigManager(str(config_dir))
        with pytest.raises(yaml.YAMLError):
            mgr.load_all()


# ---------------------------------------------------------------------------
# Split prompt_templates directory
# ---------------------------------------------------------------------------


class TestSplitPromptTemplates:
    def _write_minimal_categories(self, config_dir: Path) -> None:
        cats = {
            "event_categories": [
                {
                    "event_id": 1,
                    "event_code": "A",
                    "name": "Illegal Parking",
                    "name_zh": "违法停车",
                    "description": "Vehicle stopped illegally.",
                    "detection_mode": "expert_agent",
                    "prompt_template_id": "illegal_parking",
                    "confidence_threshold": 0.7,
                    "is_active": True,
                }
            ]
        }
        (config_dir / "event_categories.yaml").write_text(
            yaml.safe_dump(cats), encoding="utf-8"
        )

    def test_prompts_directory_takes_precedence(self, tmp_path: Path) -> None:
        """If prompts/ exists and contains YAML files, use them."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        self._write_minimal_categories(config_dir)

        prompts_dir = config_dir / "prompts"
        prompts_dir.mkdir()
        split_templates = {
            "prompt_templates": [
                {
                    "template_id": "illegal_parking",
                    "name": "Split Template",
                    "system_prompt": "from split",
                    "user_prompt": "from split",
                }
            ]
        }
        (prompts_dir / "event_0.yaml").write_text(
            yaml.safe_dump(split_templates), encoding="utf-8"
        )

        mgr = ConfigManager(str(config_dir))
        mgr.load_all()
        tmpl = mgr.get_prompt_template("illegal_parking")
        assert "from split" in tmpl.system_prompt

    def test_empty_prompts_directory_raises(self, tmp_path: Path) -> None:
        """An empty prompts/ directory must raise FileNotFoundError."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        self._write_minimal_categories(config_dir)
        (config_dir / "prompts").mkdir()

        mgr = ConfigManager(str(config_dir))
        with pytest.raises(FileNotFoundError):
            mgr.load_all()

    def test_duplicate_template_later_file_wins(self, tmp_path: Path, caplog) -> None:
        """Duplicate template_id + version combinations are overwritten in load order."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        self._write_minimal_categories(config_dir)

        prompts_dir = config_dir / "prompts"
        prompts_dir.mkdir()
        for fname, text in (
            ("common.yaml", "first"),
            ("event_0.yaml", "second"),
        ):
            data = {
                "prompt_templates": [
                    {
                        "template_id": "illegal_parking",
                        "version": "1.0.0",
                        "name": f"{text} Template",
                        "system_prompt": f"from {text}",
                        "user_prompt": f"from {text}",
                    }
                ]
            }
            (prompts_dir / fname).write_text(
                yaml.safe_dump(data), encoding="utf-8"
            )

        mgr = ConfigManager(str(config_dir))
        with caplog.at_level("WARNING"):
            mgr.load_all()
        tmpl = mgr.get_prompt_template("illegal_parking")
        assert "from second" in tmpl.system_prompt
        assert "Duplicate prompt template" in caplog.text


# ---------------------------------------------------------------------------
# Regression tests: event_id integrity and unsupported configuration
# ---------------------------------------------------------------------------


class TestEventIdIntegrity:
    def test_duplicate_event_id_raises(self, temp_config_dir: Path) -> None:
        """Duplicate event_ids must fail fast instead of silently overwriting."""
        cats = {
            "event_categories": [
                {
                    "event_id": 1,
                    "event_code": "A",
                    "name": "First",
                    "name_zh": "第一",
                    "description": "desc",
                    "detection_mode": "expert_agent",
                    "prompt_template_id": "illegal_parking",
                    "is_active": True,
                },
                {
                    "event_id": 1,
                    "event_code": "A2",
                    "name": "Second",
                    "name_zh": "第二",
                    "description": "desc",
                    "detection_mode": "expert_agent",
                    "prompt_template_id": "illegal_parking",
                    "is_active": True,
                },
            ]
        }
        (temp_config_dir / "event_categories.yaml").write_text(
            yaml.safe_dump(cats), encoding="utf-8"
        )

        mgr = ConfigManager(str(temp_config_dir))
        with pytest.raises(ValueError, match="Duplicate event_id 1"):
            mgr.load_all()

    def test_duplicate_adjudication_rule_id_raises(self, temp_config_dir: Path) -> None:
        """Duplicate adjudication rule_ids must fail fast at load time."""
        cats = {
            "event_categories": [
                {
                    "event_id": 1,
                    "event_code": "A",
                    "name": "Illegal Parking",
                    "name_zh": "违法停车",
                    "description": "desc",
                    "detection_mode": "expert_agent",
                    "prompt_template_id": "illegal_parking",
                    "is_active": True,
                }
            ],
            "adjudication_rules": [
                {"rule_id": "r1", "name": "first", "description": "d", "priority": 10},
                {"rule_id": "r1", "name": "second", "description": "d", "priority": 20},
            ],
        }
        (temp_config_dir / "event_categories.yaml").write_text(
            yaml.safe_dump(cats), encoding="utf-8"
        )

        mgr = ConfigManager(str(temp_config_dir))
        with pytest.raises(ValueError, match="Duplicate adjudication rule_id 'r1'"):
            mgr.load_all()

    def _write_categories_and_spec(self, config_dir: Path, categories: list) -> None:
        """Overwrite categories YAML and a matching annotation_spec."""
        (config_dir / "event_categories.yaml").write_text(
            yaml.safe_dump({"event_categories": categories}), encoding="utf-8"
        )
        annotation_spec = {
            "annotation_spec": {
                "version": "1.0",
                "events": [
                    {
                        "event_id": cat["event_id"],
                        "action_label": cat["name_zh"],
                        "description": "desc",
                        "boundary_conditions": [],
                    }
                    for cat in categories
                ],
            }
        }
        (config_dir / "annotation_spec.yaml").write_text(
            yaml.safe_dump(annotation_spec, allow_unicode=True), encoding="utf-8"
        )

    def test_event_id_gap_reported(self, temp_config_dir: Path) -> None:
        """Non-continuous event_ids must be reported (encoding bits would shift)."""
        categories = [
            {
                "event_id": 1,
                "event_code": "A",
                "name": "Cat Zero",
                "name_zh": "事件零",
                "description": "desc",
                "detection_mode": "expert_agent",
                "prompt_template_id": "illegal_parking",
                "is_active": True,
            },
            {
                "event_id": 3,
                "event_code": "C",
                "name": "Cat Two",
                "name_zh": "事件二",
                "description": "desc",
                "detection_mode": "expert_agent",
                "prompt_template_id": "emergency_lane",
                "is_active": True,
            },
        ]
        self._write_categories_and_spec(temp_config_dir, categories)

        mgr = ConfigManager(str(temp_config_dir))
        mgr.load_all()
        errors = mgr.validate_config()
        assert any("continuous" in e for e in errors)

    def test_active_non_expert_detection_mode_rejected(self, temp_config_dir: Path) -> None:
        """Active categories with detection_mode != expert_agent have no execution path."""
        categories = [
            {
                "event_id": 1,
                "event_code": "A",
                "name": "Direct Active",
                "name_zh": "直接活跃",
                "description": "desc",
                "detection_mode": "direct_vlm",
                "is_active": True,
            },
            {
                "event_id": 2,
                "event_code": "B",
                "name": "Direct Inactive",
                "name_zh": "直接未激活",
                "description": "desc",
                "detection_mode": "direct_vlm",
                "is_active": False,
            },
        ]
        self._write_categories_and_spec(temp_config_dir, categories)

        mgr = ConfigManager(str(temp_config_dir))
        mgr.load_all()
        errors = mgr.validate_config()
        mode_errors = [e for e in errors if "detection_mode" in e]
        # Only the active category is rejected; the inactive one just holds its bit.
        assert len(mode_errors) == 1
        assert "id=1" in mode_errors[0]

    def test_active_category_declaring_tools_rejected(self, temp_config_dir: Path) -> None:
        """Declared tools have no effect while the tool registry is empty."""
        categories = [
            {
                "event_id": 1,
                "event_code": "A",
                "name": "Tool User",
                "name_zh": "工具用户",
                "description": "desc",
                "detection_mode": "expert_agent",
                "prompt_template_id": "illegal_parking",
                "tools": ["some_tool"],
                "is_active": True,
            }
        ]
        self._write_categories_and_spec(temp_config_dir, categories)

        mgr = ConfigManager(str(temp_config_dir))
        mgr.load_all()
        errors = mgr.validate_config()
        assert any("tool registry is empty" in e for e in errors)
