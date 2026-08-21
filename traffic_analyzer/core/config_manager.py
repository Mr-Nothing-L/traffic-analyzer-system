"""
ConfigManager module for the traffic analyzer framework.

Loads and validates YAML configuration files and .env settings,
exposing them as strongly typed Pydantic models.

[文件说明]
作用:配置管理中心(ConfigManager),负责加载与校验 YAML 配置和 .env 设置,
     统一以 Pydantic 模型向外提供 SystemConfig、事件类别、prompt 模板与裁决规则。
上游:cli.py、orchestrator/analysis_orchestrator.py、core/pipeline_steps.py、
     core/expert_agent.py、core/expert_agent_far_enhancement.py 等所有需要配置的模块。
下游:config/event_categories.yaml、config/annotation_spec.yaml、config/prompts/*.yaml
     及 config/.env(仅读取环境变量,不写敏感值)。
"""

from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import dotenv_values, load_dotenv

from traffic_analyzer.models.schemas import (
    AdjudicationRule,
    EventCategory,
    LLMProviderConfig,
    PromptTemplate,
    SamplingConfig,
    SystemConfig,
)

logger = logging.getLogger(__name__)


class ConfigValidationError(ValueError):
    """Raised when configuration files fail cross-validation checks."""


class ConfigManager:
    """Manages loading and validation of framework configuration.

    The manager reads YAML files from a designated config directory and overlays
    LLM provider settings from a ``.env`` file (via ``python-dotenv``). All data
    is exposed as Pydantic v2 models for type-safe consumption across the system.

    Attributes:
        config_dir: Directory containing YAML configuration files.
        _system_config: Cached ``SystemConfig`` instance.
        _event_categories: Mapping of event_id -> ``EventCategory``.
        _prompt_templates: Mapping of template_id -> ``PromptTemplate``.
    """

    _YAML_FILES = {
        "event_categories": "event_categories.yaml",
    }
    _PROMPT_DIR = "prompts"

    def __init__(self, config_dir: str) -> None:
        """Initialise the manager with a configuration directory.

        Args:
            config_dir: Absolute or relative path to the directory that holds
                the YAML configs and optionally a ``.env`` file.
        """
        self.config_dir = Path(config_dir).resolve()
        self._system_config: Optional[SystemConfig] = None
        self._event_categories: Dict[int, EventCategory] = {}
        self._prompt_templates: Dict[str, PromptTemplate] = {}
        self._adjudication_rules: Dict[str, AdjudicationRule] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_all(self, config_dir: Optional[str] = None) -> SystemConfig:
        """Load all configuration sources and return a ``SystemConfig``.

        This method reads the three YAML files, parses the ``.env`` file for
        LLM provider overrides, and assembles a fully populated
        ``SystemConfig`` model. The result is cached internally.

        Args:
            config_dir: If provided, updates ``self.config_dir`` before loading.

        Returns:
            A validated ``SystemConfig`` instance.

        Raises:
            FileNotFoundError: If a required YAML file is missing.
            ValueError: If YAML content cannot be parsed into the expected shape.
        """
        if config_dir is not None:
            self.config_dir = Path(config_dir).resolve()

        # Load .env into os.environ for non-LLM configuration (e.g. PREFILTER_ENABLE,
        # SAMPLING_FPS). LLM provider settings are read separately via dotenv_values
        # so that system environment variables are ignored.
        self._load_dotenv_files()

        # --- event_categories YAML ---
        try:
            raw_event_categories = self._load_yaml("event_categories")
        except (FileNotFoundError, ValueError, yaml.YAMLError):
            raise  # critical config missing or malformed — must fail fast
        except Exception as exc:
            logger.error(
                "[config_manager:load_all] EVENT_CATEGORIES_LOAD_ERROR | file=%s | %s",
                self._YAML_FILES["event_categories"],
                exc,
                exc_info=True,
            )
            raw_event_categories = {}

        # --- prompt_templates YAML (split directory with fallback) ---
        try:
            raw_prompt_templates, prompt_files_loaded = self._load_prompt_templates()
        except (FileNotFoundError, ValueError, yaml.YAMLError):
            raise  # critical config missing or malformed — must fail fast
        except Exception as exc:
            logger.error(
                "[config_manager:load_all] PROMPT_TEMPLATES_LOAD_ERROR | dir=%s | %s",
                self._PROMPT_DIR,
                exc,
                exc_info=True,
            )
            raw_prompt_templates = {}
            prompt_files_loaded = []

        # --- LLM config ---
        try:
            llm_providers = self._load_env_llm_providers()
        except Exception as exc:
            logger.error(
                "[config_manager:load_all] LLM_CONFIG_ERROR | %s",
                exc,
                exc_info=True,
            )
            llm_providers = [LLMProviderConfig()]

        # Build lookup tables; duplicate event_ids would silently overwrite each
        # other and misalign the binary encoding, so fail fast instead.
        self._event_categories = {}
        for cat in raw_event_categories.get("event_categories", []):
            event_id = cat["event_id"]
            if event_id in self._event_categories:
                raise ValueError(
                    f"Duplicate event_id {event_id} in {self._YAML_FILES['event_categories']}"
                )
            self._event_categories[event_id] = EventCategory.model_validate(cat)

        # Load adjudication rules; duplicate rule_ids would silently overwrite
        # each other, so fail fast instead.
        self._adjudication_rules = {}
        for rule in raw_event_categories.get("adjudication_rules", []):
            rule_id = rule["rule_id"]
            if rule_id in self._adjudication_rules:
                raise ValueError(
                    f"Duplicate adjudication rule_id '{rule_id}' in {self._YAML_FILES['event_categories']}"
                )
            self._adjudication_rules[rule_id] = AdjudicationRule.model_validate(rule)

        # Group prompt templates by template_id to support multiple versions
        self._prompt_templates: Dict[str, Dict[str, PromptTemplate]] = {}
        for tmpl in raw_prompt_templates.get("prompt_templates", []):
            pt = PromptTemplate.model_validate(tmpl)
            if pt.template_id not in self._prompt_templates:
                self._prompt_templates[pt.template_id] = {}
            self._prompt_templates[pt.template_id][pt.version] = pt
            logger.debug("Loaded prompt template '%s' version '%s'", pt.template_id, pt.version)

        # Log templates with multiple versions
        for tid, versions in self._prompt_templates.items():
            if len(versions) > 1:
                logger.info("Prompt template '%s' has %d versions: %s", tid, len(versions), list(versions.keys()))

        # Read optional frame count limits from env
        su_min_frames = os.getenv("SCENE_UNDERSTANDING_MIN_FRAMES")
        vlm_max_frames = os.getenv("VLM_MAX_FRAMES")
        expert_enable_reflection = os.getenv("EXPERT_ENABLE_REFLECTION")
        grounding_check_enable = os.getenv("GROUNDING_CHECK_ENABLE")
        system_kwargs: Dict[str, Any] = {}
        if su_min_frames is not None:
            try:
                system_kwargs["scene_understanding_min_frames"] = int(su_min_frames)
            except ValueError:
                logger.warning("Invalid SCENE_UNDERSTANDING_MIN_FRAMES value '%s', using default", su_min_frames)
                # Pass the SystemConfig field default (models/config.py) explicitly;
                # otherwise the field default_factory would re-parse the invalid
                # env value and crash.
                system_kwargs["scene_understanding_min_frames"] = 10
        if vlm_max_frames is not None:
            try:
                system_kwargs["vlm_max_frames"] = int(vlm_max_frames)
            except ValueError:
                logger.warning("Invalid VLM_MAX_FRAMES value '%s', using default", vlm_max_frames)
                system_kwargs["vlm_max_frames"] = 10
        if expert_enable_reflection is not None:
            system_kwargs["expert_enable_reflection"] = expert_enable_reflection.lower() in ("1", "true", "yes", "on")
        if grounding_check_enable is not None:
            system_kwargs["grounding_check_enable"] = grounding_check_enable.lower() in ("1", "true", "yes", "on")

        # --- SystemConfig build ---
        try:
            self._system_config = SystemConfig(
                llm_providers=llm_providers,
                sampling=SamplingConfig(),  # reads SAMPLING_FPS from env
                **system_kwargs,
            )
        except Exception as exc:
            logger.error(
                "[config_manager:load_all] SYSTEM_CONFIG_ERROR | %s",
                exc,
                exc_info=True,
            )
            raise

        total_template_versions = sum(len(v) for v in self._prompt_templates.values())
        logger.info(
            "Config loaded: %d categories, %d adjudication rules, "
            "%d prompt templates (%d versions) from %d file(s)",
            len(self._event_categories),
            len(self._adjudication_rules),
            len(self._prompt_templates),
            total_template_versions,
            len(prompt_files_loaded),
        )

        return self._system_config

    def get_event_categories(self) -> List[EventCategory]:
        """Return all configured event categories, ordered by ``event_id``."""
        if self._system_config is None:
            raise RuntimeError("Configuration has not been loaded. Call load_all() first.")
        return [self._event_categories[k] for k in sorted(self._event_categories)]

    def get_active_event_categories(self) -> List[EventCategory]:
        """Return only event categories with ``is_active=True``, ordered by ``event_id``."""
        if self._system_config is None:
            raise RuntimeError("Configuration has not been loaded. Call load_all() first.")
        return [
            self._event_categories[k]
            for k in sorted(self._event_categories)
            if self._event_categories[k].is_active
        ]

    def get_llm_providers(self) -> List[LLMProviderConfig]:
        """Return all configured LLM providers in priority order."""
        if self._system_config is None:
            raise RuntimeError("Configuration has not been loaded. Call load_all() first.")
        return self._system_config.llm_providers

    def get_llm_provider(self) -> LLMProviderConfig:
        """Return the primary (first) LLM provider.

        Kept for backwards compatibility with code that expects a single provider.
        """
        if self._system_config is None:
            raise RuntimeError("Configuration has not been loaded. Call load_all() first.")
        if not self._system_config.llm_providers:
            raise RuntimeError("No LLM providers configured.")
        return self._system_config.llm_providers[0]

    def get_prompt_template(
        self,
        template_id: str,
        version: Optional[str] = None,
    ) -> PromptTemplate:
        """Fetch a prompt template by ID, with optional version selection.

        Supports A/B testing via ``traffic_percentage`` on template variants.
        Version selection order:
        1. Explicit ``version`` parameter
        2. Environment variable ``PROMPT_VERSION_{template_id}``
        3. A/B traffic split (if variants have ``traffic_percentage``)
        4. Latest version (highest version string)

        Args:
            template_id: The ``template_id`` of the desired template.
            version: Optional explicit version to select.

        Returns:
            The selected ``PromptTemplate``.

        Raises:
            KeyError: If no template with the given ID exists.
            ValueError: If the requested version is not found.
        """
        try:
            if self._system_config is None:
                raise RuntimeError("Configuration has not been loaded. Call load_all() first.")
            if template_id not in self._prompt_templates:
                raise KeyError(f"Prompt template '{template_id}' not found.")

            versions = self._prompt_templates[template_id]

            # 1. Explicit version parameter
            if version is not None:
                if version not in versions:
                    raise ValueError(
                        f"Prompt template '{template_id}' version '{version}' not found. "
                        f"Available: {list(versions.keys())}"
                    )
                return versions[version]

            # 2. Environment variable override
            env_version = os.getenv(f"PROMPT_VERSION_{template_id.upper().replace('-', '_')}")
            if env_version and env_version in versions:
                logger.debug("Using env-specified version '%s' for template '%s'", env_version, template_id)
                return versions[env_version]

            # 3. A/B traffic split (only when multiple variants have traffic_percentage)
            variants_with_traffic = [
                (v, pt) for v, pt in versions.items() if pt.traffic_percentage is not None
            ]
            if len(variants_with_traffic) > 1:
                import random
                roll = random.randint(1, 100)
                cumulative = 0
                for v, pt in sorted(variants_with_traffic, key=lambda x: x[1].traffic_percentage or 0):
                    cumulative += pt.traffic_percentage or 0
                    if roll <= cumulative:
                        logger.debug("A/B selected version '%s' for template '%s' (roll=%d)", v, template_id, roll)
                        return pt
                # Fallback to last variant if roll exceeds cumulative
                return variants_with_traffic[-1][1]

            # 4. Default: latest version (semantic version comparison)
            def _version_key(v: str) -> tuple:
                try:
                    return tuple(int(x) for x in v.split("."))
                except ValueError:
                    return (0,)

            latest_version = max(versions.keys(), key=_version_key)
            return versions[latest_version]
        except Exception as exc:
            logger.error(
                "[config_manager:get_prompt_template] TEMPLATE_FETCH_ERROR | template_id=%s version=%s | %s",
                template_id,
                version,
                exc,
                exc_info=True,
            )
            raise

    def get_adjudication_rules(self) -> List[AdjudicationRule]:
        """Return all configured adjudication rules, ordered by priority (descending)."""
        if self._system_config is None:
            raise RuntimeError("Configuration has not been loaded. Call load_all() first.")
        return sorted(self._adjudication_rules.values(), key=lambda r: r.priority, reverse=True)

    def validate_config(self) -> List[str]:
        """Validate cross-references and consistency across config files.

        Checks performed:
        1. ``annotation_spec.yaml`` event IDs exactly match
           ``event_categories.yaml`` event IDs.
        2. Every ``EventCategory`` with ``detection_mode == expert_agent`` has a
           valid ``prompt_template_id``.
        3. Adjudication rules have valid priorities (duplicate rule_ids already
           fail at load time).
        4. Prompt template A/B traffic percentages sum to 100%.
        5. Tools referenced in event categories exist in prompt templates;
           active categories declaring tools are rejected because the tool
           registry currently registers none.
        6. Event IDs are continuous from 0 — inactive categories included, since
           they still occupy a bit in the binary encoding.
        7. Active categories use ``expert_agent``, the only detection mode with
           an execution path.

        Returns:
            A list of human-readable error messages. An empty list indicates a
            fully valid configuration.

        Raises:
            ConfigValidationError: If ``annotation_spec.yaml`` is missing or its
                event IDs do not match ``event_categories.yaml``.
        """
        if self._system_config is None:
            raise RuntimeError("Configuration has not been loaded. Call load_all() first.")

        errors: List[str] = []

        # 1. annotation_spec.yaml event IDs must match event_categories.yaml
        annotation_spec_path = self.config_dir / "annotation_spec.yaml"
        if not annotation_spec_path.exists():
            raise ConfigValidationError(
                f"Required config file not found for cross-validation: {annotation_spec_path}"
            )
        try:
            with annotation_spec_path.open("r", encoding="utf-8") as fh:
                annotation_data = yaml.safe_load(fh) or {}
        except Exception as exc:
            raise ConfigValidationError(
                f"Failed to parse {annotation_spec_path}: {exc}"
            ) from exc

        annotation_events = annotation_data.get("annotation_spec", {}).get("events", [])
        annotation_event_ids = {
            ev["event_id"]
            for ev in annotation_events
            if isinstance(ev, dict) and "event_id" in ev
        }
        category_event_ids = set(self._event_categories.keys())

        if annotation_event_ids != category_event_ids:
            missing_in_annotation = sorted(category_event_ids - annotation_event_ids)
            extra_in_annotation = sorted(annotation_event_ids - category_event_ids)
            details = []
            if missing_in_annotation:
                details.append(
                    f"event_categories.yaml has event_ids not in annotation_spec.yaml: {missing_in_annotation}"
                )
            if extra_in_annotation:
                details.append(
                    f"annotation_spec.yaml has event_ids not in event_categories.yaml: {extra_in_annotation}"
                )
            raise ConfigValidationError(
                "annotation_spec.yaml event IDs do not match event_categories.yaml event IDs. "
                + "; ".join(details)
            )

        # Build a set of all valid template IDs
        valid_template_ids = set(self._prompt_templates.keys())

        # 2. expert_agent events must have prompt_template_id
        for cat in self._event_categories.values():
            if cat.detection_mode.value == "expert_agent":
                if not cat.prompt_template_id:
                    errors.append(
                        f"EventCategory '{cat.name}' (id={cat.event_id}) uses "
                        f"detection_mode=expert_agent but has no prompt_template_id."
                    )
                elif cat.prompt_template_id not in valid_template_ids:
                    errors.append(
                        f"EventCategory '{cat.name}' (id={cat.event_id}) references "
                        f"unknown prompt_template_id '{cat.prompt_template_id}'."
                    )

        # 3. Adjudication rule validation (duplicate rule_ids fail at load time)
        for rule in self._adjudication_rules.values():
            if rule.priority < 0 or rule.priority > 1000:
                errors.append(
                    f"Adjudication rule '{rule.rule_id}' has priority {rule.priority} "
                    f"outside valid range [0, 1000]."
                )

        # 4. Prompt template A/B traffic percentage validation
        for template_id, versions in self._prompt_templates.items():
            variants_with_traffic = [
                (v, pt) for v, pt in versions.items() if pt.traffic_percentage is not None
            ]
            if len(variants_with_traffic) > 1:
                total_pct = sum(pt.traffic_percentage or 0 for _, pt in variants_with_traffic)
                if total_pct > 100:
                    errors.append(
                        f"Prompt template '{template_id}' A/B variants traffic_percentage "
                        f"sum to {total_pct}% (exceeds 100%)."
                    )
                elif total_pct < 100:
                    errors.append(
                        f"Prompt template '{template_id}' A/B variants traffic_percentage "
                        f"sum to {total_pct}% (less than 100%, some traffic will fallback to last variant)."
                    )

        # 5. Validate tools referenced in event categories exist in prompt templates
        for cat in self._event_categories.values():
            if cat.tools:
                if cat.is_active:
                    # The tool registry registers no tools yet, so any declared
                    # tools would silently do nothing.
                    errors.append(
                        f"EventCategory '{cat.name}' (id={cat.event_id}) declares tools "
                        f"{cat.tools} but the tool registry is empty; declared tools have no effect."
                    )
                if cat.prompt_template_id:
                    template_versions = self._prompt_templates.get(cat.prompt_template_id, {})
                    if template_versions:
                        latest_template = max(template_versions.values(), key=lambda t: t.version)
                        template_tools = set(latest_template.available_tools or [])
                        for tool_name in cat.tools:
                            if tool_name not in template_tools:
                                errors.append(
                                    f"EventCategory '{cat.name}' (id={cat.event_id}) references "
                                    f"tool '{tool_name}' not listed in prompt template '{cat.prompt_template_id}' "
                                    f"available_tools."
                                )
                    else:
                        errors.append(
                            f"EventCategory '{cat.name}' (id={cat.event_id}) has tools {cat.tools} "
                            f"but prompt template '{cat.prompt_template_id}' not found."
                        )

        # 6. Event IDs are the global annotation-doc v4.5 action numbers and
        # must be continuous from 1; id 9 is the reserved "normal" placeholder
        # and is intentionally skipped. Any other gap silently shrinks the
        # binary encoding width and drops higher events from the encoding.
        # Inactive categories still occupy a bit, so all categories count.
        sorted_ids = sorted(self._event_categories.keys())
        expected_ids = [i for i in range(1, sorted_ids[-1] + 1) if i != 9] if sorted_ids else []
        if sorted_ids != expected_ids:
            errors.append(
                f"event_categories.yaml event_ids must be continuous from 1 "
                f"(9 reserved as the normal placeholder), got {sorted_ids}."
            )

        # 7. Only expert_agent has an execution path; a non-expert active
        # category (only reachable via stale/bad YAML) would pin its bit to 0.
        for cat in self._event_categories.values():
            if cat.is_active and cat.detection_mode.value != "expert_agent":
                errors.append(
                    f"EventCategory '{cat.name}' (id={cat.event_id}) is active but uses "
                    f"detection_mode={cat.detection_mode.value}; only expert_agent is "
                    f"currently implemented."
                )

        return errors

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_yaml(self, key: str) -> Dict[str, Any]:
        """Load a single YAML file by its logical key.

        Args:
            key: One of the keys in ``_YAML_FILES``.

        Returns:
            The parsed YAML content as a Python dict.
        """
        filename = self._YAML_FILES[key]
        path = self.config_dir / filename
        try:
            if not path.exists():
                raise FileNotFoundError(f"Required config file not found: {path}")
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            if data is None:
                return {}
            if not isinstance(data, dict):
                raise ValueError(f"Top-level of {path} must be a mapping, got {type(data).__name__}")
            return data
        except Exception as exc:
            logger.error(
                "[config_manager:_load_yaml] YAML_LOAD_ERROR | file=%s | %s",
                filename,
                exc,
                exc_info=True,
            )
            raise

    def _load_prompt_templates(self) -> tuple[Dict[str, Any], List[Path]]:
        """Load prompt templates from the split ``prompts/`` directory.

        ``<config_dir>/prompts/`` must exist and contain at least one ``.yaml``
        file. Those files are loaded in lexicographic order and their
        ``prompt_templates`` lists are merged.

        Duplicate ``template_id`` + ``version`` combinations are resolved with
        the later file winning; a warning is logged for each duplicate.

        Returns:
            A tuple of (merged raw dict, list of loaded file paths).

        Raises:
            FileNotFoundError: If the ``prompts/`` directory does not exist or
                contains no ``.yaml`` files.
        """
        prompt_dir = self.config_dir / self._PROMPT_DIR
        if not prompt_dir.is_dir():
            raise FileNotFoundError(
                f"Required prompt directory not found: {prompt_dir}"
            )

        yaml_files = sorted([p for p in prompt_dir.glob("*.yaml") if p.is_file()])
        if not yaml_files:
            raise FileNotFoundError(
                f"Prompt directory {prompt_dir} contains no .yaml files"
            )

        merged: Dict[str, Any] = {"prompt_templates": []}
        seen: Dict[str, Dict[str, Path]] = {}
        loaded_paths: List[Path] = []

        for path in yaml_files:
            try:
                with path.open("r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh)
            except Exception as exc:
                logger.error(
                    "[config_manager:_load_prompt_templates] YAML_LOAD_ERROR | file=%s | %s",
                    path,
                    exc,
                    exc_info=True,
                )
                raise

            if data is None:
                data = {}
            if not isinstance(data, dict):
                raise ValueError(
                    f"Top-level of {path} must be a mapping, got {type(data).__name__}"
                )

            templates = data.get("prompt_templates", [])
            if not isinstance(templates, list):
                raise ValueError(
                    f"'prompt_templates' in {path} must be a list, got {type(templates).__name__}"
                )

            for tmpl in templates:
                if not isinstance(tmpl, dict):
                    raise ValueError(
                        f"Each prompt template in {path} must be a mapping"
                    )
                tid = tmpl.get("template_id")
                version = tmpl.get("version")
                if tid is None:
                    raise ValueError(
                        f"Prompt template in {path} is missing required 'template_id'"
                    )

                if tid in seen and version in seen[tid]:
                    logger.warning(
                        "Duplicate prompt template '%s' version '%s' in %s; overwriting version from %s",
                        tid,
                        version,
                        path,
                        seen[tid][version],
                    )
                seen.setdefault(tid, {})[version] = path
                merged["prompt_templates"].append(tmpl)

            loaded_paths.append(path)

        return merged, loaded_paths

    def _load_dotenv_files(self) -> None:
        """Load the first available ``.env`` file into the process environment."""
        env_loaded = False
        candidates = [self.config_dir / ".env"]

        # Also check the package root directory (one level above this file's package)
        try:
            import traffic_analyzer as _ta

            pkg_root = Path(_ta.__file__).resolve().parent.parent
            candidates.append(pkg_root / ".env")
        except Exception:
            pass

        for env_path in candidates:
            if env_path.exists():
                load_dotenv(dotenv_path=str(env_path), override=True)
                logger.info("Loaded environment variables from %s", env_path)
                env_loaded = True
                break

        if not env_loaded:
            # Final fallback: CWD / process env
            loaded = load_dotenv(override=True)
            if loaded:
                logger.info("Loaded environment variables from CWD .env")
            else:
                logger.error(
                    "[config_manager:_load_dotenv_files] ENV_FILE_NOT_FOUND | searched=%s",
                    ", ".join(str(p) for p in candidates),
                )

    def _load_dotenv_values(self) -> Dict[str, Optional[str]]:
        """Read the first available ``.env`` file into a dictionary.

        Unlike ``_load_dotenv_files``, this method intentionally does **not**
        fall back to ``os.environ``; it only considers ``.env`` files. This
        ensures LLM/VLM API configuration is strictly isolated from shell
        environment variables.

        Returns:
            A mapping of variable names to their string values. Only ``.env``
            files are consulted; system environment variables are excluded.
        """
        env = dotenv_values(self.config_dir / ".env")
        if not env:
            # 兼容旧位置：项目根目录 .env
            env = dotenv_values(Path(__file__).resolve().parents[2] / ".env")
        return env

    _ENV_NUMERIC_FIELDS = (
        ("MAX_TOKENS", "max_tokens", int),
        ("TEMPERATURE", "temperature", float),
        ("TIMEOUT", "timeout", float),
        ("MAX_RETRIES", "max_retries", int),
        ("CACHE_MAX_SIZE", "cache_max_size", int),
    )

    def _build_llm_config_from_env(
        self, env: Dict[str, Optional[str]], prefix: Optional[str] = None
    ) -> LLMProviderConfig:
        """Build a single ``LLMProviderConfig`` from a ``.env`` dictionary.

        Args:
            env: Mapping of variable names to values, typically produced by
                :func:`dotenv.dotenv_values`. System environment variables must
                not be mixed into this dictionary.
            prefix: If ``None``, read the legacy ``LLM_*`` variables (and
                ``VLM_PROVIDER``). If given, read ``{prefix}_*`` variables, e.g.
                ``LLM_PROVIDER_0_PROVIDER``.

        Returns:
            An ``LLMProviderConfig`` with values overridden by the ``.env`` file.
        """
        kwargs: Dict[str, Any] = {}

        if prefix is None:
            # Support both VLM_PROVIDER (used in .env template) and LLM_PROVIDER
            provider = env.get("VLM_PROVIDER") or env.get("LLM_PROVIDER")
        else:
            provider = env.get(f"{prefix}_PROVIDER")

        if provider:
            kwargs["provider"] = provider

        # Provider-specific API key overrides generic/prefixed key
        if provider:
            specific_api_key = env.get(f"{provider.upper()}_API_KEY")
            if specific_api_key:
                kwargs["api_key"] = specific_api_key

        if prefix is None:
            api_key = env.get("LLM_API_KEY")
        else:
            api_key = env.get(f"{prefix}_API_KEY")
        if api_key:
            kwargs.setdefault("api_key", api_key)

        if prefix is None:
            base_url = env.get("LLM_BASE_URL")
        else:
            base_url = env.get(f"{prefix}_BASE_URL")
        if base_url:
            kwargs["base_url"] = base_url

        # Provider-specific base_url overrides the generic/prefixed one
        provider = kwargs.get("provider") or provider or ""
        if provider:
            specific_base_url = env.get(f"{provider.upper()}_BASE_URL")
            if specific_base_url:
                kwargs["base_url"] = specific_base_url

        # Provider-specific model overrides generic/prefixed one
        if provider:
            specific_model = env.get(f"{provider.upper()}_MODEL")
            if specific_model:
                kwargs["model"] = specific_model

        if prefix is None:
            model = env.get("LLM_MODEL")
        else:
            model = env.get(f"{prefix}_MODEL")
        if model:
            kwargs.setdefault("model", model)

        for base_name, attr_name, cast in self._ENV_NUMERIC_FIELDS:
            env_name = f"LLM_{base_name}" if prefix is None else f"{prefix}_{base_name}"
            val = env.get(env_name)
            if val is not None:
                try:
                    kwargs[attr_name] = cast(val)
                except (ValueError, TypeError) as exc:
                    logger.error(
                        "[config_manager:_build_llm_config_from_env] ENV_PARSE_ERROR | var=%s value=%s | %s",
                        env_name,
                        val,
                        exc,
                        exc_info=True,
                    )

        # Boolean flag for cache enable/disable
        if prefix is None:
            cache_enabled = env.get("LLM_ENABLE_CACHE")
        else:
            cache_enabled = env.get(f"{prefix}_ENABLE_CACHE")
        if cache_enabled is not None:
            kwargs["enable_cache"] = cache_enabled.lower() in ("1", "true", "yes", "on")

        # Disk cache path (cross-process persistent cache)
        disk_cache_path = env.get("TRAFFIC_ANALYZER_DISK_CACHE")
        if disk_cache_path:
            kwargs["disk_cache_path"] = disk_cache_path

        disk_cache_max = env.get("TRAFFIC_ANALYZER_DISK_CACHE_MAX_ENTRIES")
        if disk_cache_max is not None:
            try:
                kwargs["disk_cache_max_entries"] = int(disk_cache_max)
            except (ValueError, TypeError) as exc:
                logger.error(
                    "[config_manager:_build_llm_config_from_env] ENV_PARSE_ERROR | var=TRAFFIC_ANALYZER_DISK_CACHE_MAX_ENTRIES value=%s | %s",
                    disk_cache_max,
                    exc,
                )

        return LLMProviderConfig(**kwargs)

    def _load_env_llm_config(self) -> LLMProviderConfig:
        """Parse ``.env`` (if present) and return a single ``LLMProviderConfig``.

        Kept for backwards compatibility; new code should prefer
        :meth:`_load_env_llm_providers`.
        """
        env = self._load_dotenv_values()
        return self._build_llm_config_from_env(env, prefix=None)

    def _load_env_llm_providers(self) -> List[LLMProviderConfig]:
        """Parse ``.env`` (if present) and return a list of ``LLMProviderConfig``.

        If indexed variables such as ``LLM_PROVIDER_0_PROVIDER`` or
        ``LLM_PROVIDER_1_PROVIDER`` are present, those providers are used.
        Otherwise the legacy single-provider ``LLM_*`` variables are read and
        returned as a one-element list.
        """
        env = self._load_dotenv_values()

        indices = set()
        for key in env:
            if key.startswith("LLM_PROVIDER_") and key.endswith("_PROVIDER"):
                try:
                    idx = int(key[len("LLM_PROVIDER_") : -len("_PROVIDER")])
                    indices.add(idx)
                except ValueError:
                    pass

        if not indices:
            providers = [self._build_llm_config_from_env(env, prefix=None)]
        else:
            providers = []
            # Only build providers for indices that are actually defined; iterating
            # 0..max would fabricate an empty default provider for each gap (e.g.
            # only LLM_PROVIDER_1_* set -> phantom anthropic provider at index 0).
            for i in sorted(indices):
                providers.append(
                    self._build_llm_config_from_env(env, prefix=f"LLM_PROVIDER_{i}")
                )

        # LLM_AUTO_SWITCH=0/false/no/off disables failover: only the first
        # (active) provider is used. Anything else / unset keeps auto-switch on.
        auto_switch = str(env.get("LLM_AUTO_SWITCH") or "").strip().lower()
        if auto_switch in ("0", "false", "no", "off"):
            logger.info(
                "LLM_AUTO_SWITCH disabled; using only the first LLM provider (%s)",
                providers[0].provider if providers else "<none>",
            )
            return providers[:1]
        return providers
