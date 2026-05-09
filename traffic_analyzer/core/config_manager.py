"""
ConfigManager module for the traffic analyzer framework.

Loads, validates, and hot-reloads YAML configuration files and .env settings,
exposing them as strongly typed Pydantic models.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv

from traffic_analyzer.models.schemas import (
    CrossEventInferenceRule,
    EventCategory,
    LLMProviderConfig,
    LogicChain,
    PromptTemplate,
    SamplingConfig,
    SystemConfig,
)

logger = logging.getLogger(__name__)


class ConfigManager:
    """Manages loading, validation, and hot-reloading of framework configuration.

    The manager reads YAML files from a designated config directory and overlays
    LLM provider settings from a ``.env`` file (via ``python-dotenv``). All data
    is exposed as Pydantic v2 models for type-safe consumption across the system.

    Attributes:
        config_dir: Directory containing YAML configuration files.
        _system_config: Cached ``SystemConfig`` instance.
        _event_categories: Mapping of event_id -> ``EventCategory``.
        _logic_chains: Mapping of chain_id -> ``LogicChain``.
        _prompt_templates: Mapping of template_id -> ``PromptTemplate``.
    """

    _YAML_FILES = {
        "event_categories": "event_categories.yaml",
        "logic_chains": "logic_chains.yaml",
        "prompt_templates": "prompt_templates.yaml",
    }

    def __init__(self, config_dir: str) -> None:
        """Initialise the manager with a configuration directory.

        Args:
            config_dir: Absolute or relative path to the directory that holds
                the YAML configs and optionally a ``.env`` file.
        """
        self.config_dir = Path(config_dir).resolve()
        self._system_config: Optional[SystemConfig] = None
        self._event_categories: Dict[int, EventCategory] = {}
        self._logic_chains: Dict[str, LogicChain] = {}
        self._prompt_templates: Dict[str, PromptTemplate] = {}
        self._inference_rules: Dict[str, CrossEventInferenceRule] = {}

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

        raw_event_categories = self._load_yaml("event_categories")
        raw_logic_chains = self._load_yaml("logic_chains")
        raw_prompt_templates = self._load_yaml("prompt_templates")

        # Parse .env for LLM settings
        llm_config = self._load_env_llm_config()

        # Build lookup tables
        self._event_categories = {
            cat["event_id"]: EventCategory.model_validate(cat)
            for cat in raw_event_categories.get("event_categories", [])
        }

        # Load cross-event inference rules
        self._inference_rules = {
            rule["rule_id"]: CrossEventInferenceRule.model_validate(rule)
            for rule in raw_event_categories.get("cross_event_inference_rules", [])
        }

        self._logic_chains = {
            chain["chain_id"]: LogicChain.model_validate(chain)
            for chain in raw_logic_chains.get("logic_chains", [])
        }

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
        system_kwargs: Dict[str, Any] = {}
        if su_min_frames is not None:
            try:
                system_kwargs["scene_understanding_min_frames"] = int(su_min_frames)
            except ValueError:
                logger.warning("Invalid SCENE_UNDERSTANDING_MIN_FRAMES value '%s', using default", su_min_frames)
        if vlm_max_frames is not None:
            try:
                system_kwargs["vlm_max_frames"] = int(vlm_max_frames)
            except ValueError:
                logger.warning("Invalid VLM_MAX_FRAMES value '%s', using default", vlm_max_frames)

        self._system_config = SystemConfig(
            llm_provider=llm_config,
            sampling=SamplingConfig(),  # defaults; could be extended via YAML later
            **system_kwargs,
        )

        logger.info(
            "Config loaded: %d categories, %d inference rules, %d logic chains, %d prompt templates",
            len(self._event_categories),
            len(self._inference_rules),
            len(self._logic_chains),
            len(self._prompt_templates),
        )

        return self._system_config

    def get_event_categories(self) -> List[EventCategory]:
        """Return all configured event categories, ordered by ``event_id``."""
        if self._system_config is None:
            raise RuntimeError("Configuration has not been loaded. Call load_all() first.")
        return [self._event_categories[k] for k in sorted(self._event_categories)]

    def get_logic_chain(self, chain_id: str) -> Optional[LogicChain]:
        """Fetch a logic chain by its unique identifier.

        Args:
            chain_id: The ``chain_id`` field of the desired ``LogicChain``.

        Returns:
            The matching ``LogicChain`` or ``None`` if not found.
        """
        if self._system_config is None:
            raise RuntimeError("Configuration has not been loaded. Call load_all() first.")
        return self._logic_chains.get(chain_id)

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

        # 4. Default: latest version (highest version string)
        latest_version = max(versions.keys())
        return versions[latest_version]

    def get_inference_rules(self) -> List[CrossEventInferenceRule]:
        """Return all configured cross-event inference rules."""
        if self._system_config is None:
            raise RuntimeError("Configuration has not been loaded. Call load_all() first.")
        return list(self._inference_rules.values())

    def validate_config(self) -> List[str]:
        """Validate cross-references and consistency across config files.

        Checks performed:
        1. Every ``EventCategory`` with ``detection_mode == LOGIC_CHAIN`` references
           an existing ``LogicChain``.
        2. Every ``logic_chain_id`` referenced inside a ``LogicStep`` (``loop_body_chain_id``)
           points to an existing chain.
        3. Every ``prompt_template_id`` referenced by any ``LogicStep`` exists.
        4. Step ``output_key`` values are non-empty for steps that produce data.
        5. ``true_next_step`` / ``false_next_step`` references point to existing step IDs
           within the same chain.

        Returns:
            A list of human-readable error messages. An empty list indicates a
            fully valid configuration.
        """
        if self._system_config is None:
            raise RuntimeError("Configuration has not been loaded. Call load_all() first.")

        errors: List[str] = []

        # 1. EventCategory -> LogicChain references
        for cat in self._event_categories.values():
            if cat.detection_mode.value == "logic_chain":
                if not cat.logic_chain_id:
                    errors.append(
                        f"EventCategory '{cat.name}' (id={cat.event_id}) uses "
                        f"detection_mode=logic_chain but has no logic_chain_id."
                    )
                elif cat.logic_chain_id not in self._logic_chains:
                    errors.append(
                        f"EventCategory '{cat.name}' (id={cat.event_id}) references "
                        f"unknown logic_chain_id '{cat.logic_chain_id}'."
                    )

        # Build a set of all valid template IDs
        valid_template_ids = set(self._prompt_templates.keys())

        for chain in self._logic_chains.values():
            step_ids = {step.step_id for step in chain.steps}

            for step in chain.steps:
                # 2. Loop body chain references
                if step.loop_body_chain_id and step.loop_body_chain_id not in self._logic_chains:
                    errors.append(
                        f"LogicChain '{chain.chain_id}' step '{step.step_id}' "
                        f"references unknown loop_body_chain_id '{step.loop_body_chain_id}'."
                    )

                # 3. Prompt template references
                if step.prompt_template_id and step.prompt_template_id not in valid_template_ids:
                    errors.append(
                        f"LogicChain '{chain.chain_id}' step '{step.step_id}' "
                        f"references unknown prompt_template_id '{step.prompt_template_id}'."
                    )

                # 4. Output key presence for producing steps
                if step.step_type.value in ("vlm_call", "compute", "cv_fusion", "loop"):
                    if not step.output_key:
                        errors.append(
                            f"LogicChain '{chain.chain_id}' step '{step.step_id}' "
                            f"(type={step.step_type.value}) must define an output_key."
                        )

                # 5. Branch target validation
                for branch_attr in ("true_next_step", "false_next_step"):
                    target = getattr(step, branch_attr, None)
                    if target and target not in step_ids:
                        errors.append(
                            f"LogicChain '{chain.chain_id}' step '{step.step_id}' "
                            f"{branch_attr} points to unknown step '{target}'."
                        )

        # 6. direct_vlm events must have prompt_template_id
        for cat in self._event_categories.values():
            if cat.detection_mode.value == "direct_vlm":
                if not cat.prompt_template_id:
                    errors.append(
                        f"EventCategory '{cat.name}' (id={cat.event_id}) uses "
                        f"detection_mode=direct_vlm but has no prompt_template_id."
                    )
                elif cat.prompt_template_id not in valid_template_ids:
                    errors.append(
                        f"EventCategory '{cat.name}' (id={cat.event_id}) references "
                        f"unknown prompt_template_id '{cat.prompt_template_id}'."
                    )

        # 7. scene_tag events must have at least one inference source
        for cat in self._event_categories.values():
            if cat.detection_mode.value == "scene_tag":
                if not cat.scene_boolean_field and not cat.scene_tag_key:
                    errors.append(
                        f"EventCategory '{cat.name}' (id={cat.event_id}) uses "
                        f"detection_mode=scene_tag but has neither scene_boolean_field nor scene_tag_key."
                    )

        # 8. Cross-event inference rule validation
        valid_event_ids = set(self._event_categories.keys())
        for rule in self._inference_rules.values():
            if rule.target_event_id not in valid_event_ids:
                errors.append(
                    f"Inference rule '{rule.rule_id}' references unknown target_event_id {rule.target_event_id}."
                )
            if rule.source_event_id not in valid_event_ids:
                errors.append(
                    f"Inference rule '{rule.rule_id}' references unknown source_event_id {rule.source_event_id}."
                )
            if rule.target_event_id == rule.source_event_id:
                errors.append(
                    f"Inference rule '{rule.rule_id}' target and source are the same event."
                )
            if not rule.source_description_keywords:
                errors.append(
                    f"Inference rule '{rule.rule_id}' has empty source_description_keywords."
                )

        return errors

    def reload(self) -> SystemConfig:
        """Hot-reload all configuration files from disk.

        Returns:
            A freshly loaded and validated ``SystemConfig``.
        """
        logger.info("Hot-reloading configuration from %s", self.config_dir)
        return self.load_all()

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
        if not path.exists():
            raise FileNotFoundError(f"Required config file not found: {path}")
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ValueError(f"Top-level of {path} must be a mapping, got {type(data).__name__}")
        return data

    def _load_env_llm_config(self) -> LLMProviderConfig:
        """Parse ``.env`` (if present) and return an ``LLMProviderConfig``.

        Recognised environment variables (all optional):

        * ``LLM_PROVIDER`` -> ``provider``
        * ``LLM_API_KEY`` -> ``api_key``
        * ``LLM_BASE_URL`` -> ``base_url``
        * ``LLM_MODEL`` -> ``model``
        * ``LLM_MAX_TOKENS`` -> ``max_tokens``
        * ``LLM_TEMPERATURE`` -> ``temperature``
        * ``LLM_TIMEOUT`` -> ``timeout``
        * ``LLM_MAX_RETRIES`` -> ``max_retries``

        Returns:
            An ``LLMProviderConfig`` with values overridden by the environment.
        """
        # Search for .env in multiple locations (config_dir, package root, CWD)
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
                logger.warning(
                    "No .env file found. Searched: %s. "
                    "Ensure you have a .env file in the config directory or project root.",
                    ", ".join(str(p) for p in candidates),
                )

        kwargs: Dict[str, Any] = {}

        # Support both VLM_PROVIDER (used in .env template) and LLM_PROVIDER
        provider = os.getenv("VLM_PROVIDER") or os.getenv("LLM_PROVIDER")
        if provider:
            kwargs["provider"] = provider

        # Provider-specific API key overrides generic LLM_API_KEY
        if provider:
            specific_api_key = os.getenv(f"{provider.upper()}_API_KEY")
            if specific_api_key:
                kwargs["api_key"] = specific_api_key

        if api_key := os.getenv("LLM_API_KEY"):
            kwargs.setdefault("api_key", api_key)
        if base_url := os.getenv("LLM_BASE_URL"):
            kwargs["base_url"] = base_url

        # Provider-specific base_url overrides the generic one
        provider = kwargs.get("provider") or os.getenv("LLM_PROVIDER", "")
        if provider:
            specific_base_url = os.getenv(f"{provider.upper()}_BASE_URL")
            if specific_base_url:
                kwargs["base_url"] = specific_base_url

        if model := os.getenv("LLM_MODEL"):
            kwargs["model"] = model

        for env_name, attr_name, cast in (
            ("LLM_MAX_TOKENS", "max_tokens", int),
            ("LLM_TEMPERATURE", "temperature", float),
            ("LLM_TIMEOUT", "timeout", float),
            ("LLM_MAX_RETRIES", "max_retries", int),
            ("LLM_CACHE_MAX_SIZE", "cache_max_size", int),
        ):
            if (val := os.getenv(env_name)) is not None:
                try:
                    kwargs[attr_name] = cast(val)
                except (ValueError, TypeError) as exc:
                    logger.warning("Invalid %s value '%s': %s", env_name, val, exc)

        # Boolean flag for cache enable/disable
        cache_enabled = os.getenv("LLM_ENABLE_CACHE")
        if cache_enabled is not None:
            kwargs["enable_cache"] = cache_enabled.lower() in ("1", "true", "yes", "on")

        return LLMProviderConfig(**kwargs)
