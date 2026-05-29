"""ExpertAgent — single-event detection agent.

Each ExpertAgent is responsible for detecting exactly one event category.
It reports what it sees (fact identification) without any filtering or
exclusion logic. Adjudication happens later in the pipeline.
"""

from __future__ import annotations

import logging
import json
from typing import Any, Dict, List, Optional, Tuple

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.core.vlm_engine import VLMInferenceEngine
from traffic_analyzer.models.schemas import (
    AnalysisContext,
    EventCandidate,
    EventCategory,
    EventInstance,
    PromptTemplate,
)
from traffic_analyzer.utils.event_detection import parse_expert_response, select_event_images

logger = logging.getLogger(__name__)

# JSON schema expected from the VLM for expert-agent responses.
_EXPERT_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["detected"],
    "properties": {
        "detected": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "summary": {"type": "string"},
        "instances": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_time_sec": {"type": "number"},
                    "end_time_sec": {"type": "number"},
                    "evidence_frames": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "description": {"type": "string"},
                    "reasoning": {"type": "string"},
                },
            },
        },
    },
}


class ExpertAgent:
    """Single-event detection agent. Only responsible for fact identification."""

    def __init__(
        self,
        category: EventCategory,
        vlm_engine: VLMInferenceEngine,
        config_manager: ConfigManager,
    ) -> None:
        self.category = category
        self.vlm_engine = vlm_engine
        self.config_manager = config_manager

    def detect(self, context: AnalysisContext) -> EventCandidate:
        """Run VLM detection for this single event.

        Steps:
        1. Select images from context.keyframes.
        2. Load and render the prompt template.
        3. Call the VLM engine.
        4. Parse the response into an EventCandidate.

        If the VLM call fails, returns an EventCandidate with detected=False
        and a summary containing the error message.
        """
        # -- 1. Image selection ------------------------------------------------
        vlm_max_frames = 6
        if context.config is not None:
            vlm_max_frames = context.config.vlm_max_frames
        images = select_event_images(context, vlm_max_frames)

        if not images:
            logger.error(
                "[expert_agent:detect] NO_IMAGES | event_id=%d event_name=%s",
                self.category.event_id,
                self.category.name_zh,
            )
            return EventCandidate(
                event_id=self.category.event_id,
                event_name=self.category.name_zh,
                detected=False,
                summary="No images available for detection",
            )

        # -- 2. Prompt template ------------------------------------------------
        if not self.category.prompt_template_id:
            logger.error(
                "[expert_agent:detect] NO_TEMPLATE | event_id=%d event_name=%s",
                self.category.event_id,
                self.category.name_zh,
            )
            return EventCandidate(
                event_id=self.category.event_id,
                event_name=self.category.name_zh,
                detected=False,
                summary="No prompt template configured for this event",
            )

        try:
            template = self.config_manager.get_prompt_template(
                self.category.prompt_template_id
            )
        except (KeyError, RuntimeError) as exc:
            logger.error(
                "[expert_agent:detect] TEMPLATE_LOAD_ERROR | event_id=%d event_name=%s template_id=%s | %s",
                self.category.event_id,
                self.category.name_zh,
                self.category.prompt_template_id,
                exc,
                exc_info=True,
            )
            return EventCandidate(
                event_id=self.category.event_id,
                event_name=self.category.name_zh,
                detected=False,
                summary=f"Failed to load prompt template: {exc}",
            )

        # -- 3. Inject prior knowledge (scene_understanding rules) -----------
        # scene_understanding prompt contains universal rules (direction,
        # emergency lane identification, camera perspective) that all experts
        # should know.  It is treated as fixed prior knowledge, not a VLM call.
        prior_knowledge = ""
        try:
            prior_template = self.config_manager.get_prompt_template(
                "scene_understanding"
            )
            if prior_template.user_prompt:
                prior_knowledge = prior_template.user_prompt
        except (KeyError, RuntimeError):
            logger.debug(
                "ExpertAgent[%s]: scene_understanding template not found, "
                "skipping prior knowledge injection",
                self.category.name_zh,
            )

        if prior_knowledge:
            logger.info(
                "[expert_agent:detect] PRIOR_KNOWLEDGE | event_id=%d event_name=%s loaded=True length=%d",
                self.category.event_id,
                self.category.name_zh,
                len(prior_knowledge),
            )
            # Build enhanced template with prior knowledge appended to system_prompt
            enhanced_system = template.system_prompt
            if enhanced_system and not enhanced_system.endswith("\n"):
                enhanced_system += "\n"
            enhanced_system += (
                "\n============================================================\n"
                "先验知识（高速公路监控场景通用规则，直接应用，无需重新推断）\n"
                "============================================================\n"
                + prior_knowledge
            )
            template = PromptTemplate(
                template_id=template.template_id,
                name=template.name,
                version=template.version,
                system_prompt=enhanced_system,
                user_prompt=template.user_prompt,
                output_format_hint=template.output_format_hint,
                example_input=template.example_input,
                example_output=template.example_output,
                traffic_percentage=template.traffic_percentage,
                available_tools=template.available_tools,
            )

        # -- 4. Context variables ----------------------------------------------
        context_vars: Dict[str, Any] = {
            "event_definition": self.category.definition,
            "event_name": self.category.name_zh,
            "event_id": self.category.event_id,
        }
        if context.video_meta is not None:
            context_vars["video_meta"] = context.video_meta.model_dump()

        # -- CV evidence (legacy, now handled by yolo_track_tool via Native API) --
        cv_evidence = ""
        tracking_evidence = ""
        context_vars["cv_evidence"] = cv_evidence
        context_vars["tracking_evidence"] = tracking_evidence

        # -- 5. First VLM call -------------------------------------------------
        first_response = None
        tool_result = None
        annotated_image = None
        
        # If tools configured, use Anthropic Native API directly
        if self.category.tools and self.vlm_engine.provider == "anthropic":
            try:
                native_result = self._execute_anthropic_native_tools(
                    template, images, context_vars, context
                )
                if native_result is not None:
                    tool_result, annotated_image = native_result
                    logger.info(
                        "[expert_agent:detect] ANTHROPIC_NATIVE_SUCCESS | event_id=%d event_name=%s",
                        self.category.event_id,
                        self.category.name_zh,
                    )
            except Exception as exc:
                logger.warning(
                    "[expert_agent:detect] ANTHROPIC_NATIVE_ERROR | event_id=%d event_name=%s | %s",
                    self.category.event_id,
                    self.category.name_zh,
                    exc,
                )
        
        # Fallback to regular call (no tools or Native API failed)
        if tool_result is None:
            try:
                first_response = self.vlm_engine.call(
                    template=template,
                    images=images,
                    context_vars=context_vars,
                    response_schema=_EXPERT_RESPONSE_SCHEMA,
                )
            except Exception as exc:
                logger.error(
                    "[expert_agent:detect] VLM_ERROR | event_id=%d event_name=%s | %s",
                    self.category.event_id,
                    self.category.name_zh,
                    exc,
                    exc_info=True,
                )
                error_candidate = EventCandidate(
                    event_id=self.category.event_id,
                    event_name=self.category.name_zh,
                    detected=False,
                    summary=f"VLM call failed: {exc}",
                )
                error_candidate.cv_evidence = cv_evidence
                error_candidate.tracking_evidence = tracking_evidence
                return error_candidate
            
            # Check for string-based tool calls in response
            if self.category.tools and first_response:
                tool_result, annotated_image = self._execute_tool_calls(
                    first_response, context, images
                )

        # -- 6. Second VLM call (if tool was called) ---------------------------
        if tool_result is not None:
            logger.info(
                "[expert_agent:detect] TOOL_CALL_EXECUTED | event_id=%d event_name=%s | "
                "proceeding to second VLM call with tool results",
                self.category.event_id,
                self.category.name_zh,
            )
            second_candidate = self._second_vlm_call(
                template=template,
                first_response=first_response,
                tool_result=tool_result,
                annotated_image=annotated_image,
                images=images,
                context_vars=context_vars,
            )
            second_candidate.cv_evidence = cv_evidence
            second_candidate.tracking_evidence = tool_result.get("tracking_text", "")
            return second_candidate

        # -- 8. Parse first response (no tool call) ----------------------------
        candidate = parse_expert_response(first_response, self.category)
        candidate.cv_evidence = cv_evidence
        candidate.tracking_evidence = tracking_evidence
        logger.debug(
            "ExpertAgent[%s]: detected=%s confidence=%.2f instances=%d",
            self.category.name_zh,
            candidate.detected,
            candidate.confidence,
            len(candidate.instances),
        )
        return candidate

    def _execute_tool_calls(
        self,
        response: Any,
        context: AnalysisContext,
        images: List[Any],
    ) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Parse tool calls from VLM response and execute them.

        Returns:
            (tool_result_dict, annotated_image_path) or (None, None) if no tool call.
        """
        if not response or not hasattr(response, "raw_text"):
            return None, None

        raw_text = response.raw_text or ""
        if "<tool_call>" not in raw_text:
            return None, None

        # Try to import tool router
        try:
            from traffic_analyzer.tools.tool_router import ToolRouter, ToolRequest, ToolResponse
            from traffic_analyzer.tools.tool_registry import get_default_router
        except ImportError as exc:
            logger.warning(
                "[expert_agent:_execute_tool_calls] TOOL_ROUTER_NOT_AVAILABLE | %s",
                exc,
            )
            return None, None

        router = get_default_router()
        if router is None:
            logger.warning(
                "[expert_agent:_execute_tool_calls] DEFAULT_ROUTER_NOT_INITIALIZED"
            )
            return None, None

        # Parse tool request from raw_text
        try:
            tool_request = ToolRequest.from_json(raw_text)
        except Exception as exc:
            logger.warning(
                "[expert_agent:_execute_tool_calls] PARSE_FAILED | %s",
                exc,
            )
            return None, None

        # Check if the requested tool is in allowed tools list
        if tool_request.tool_name not in self.category.tools:
            logger.warning(
                "[expert_agent:_execute_tool_calls] TOOL_NOT_ALLOWED | "
                "requested=%s allowed=%s",
                tool_request.tool_name,
                self.category.tools,
            )
            return None, None

        # Auto-fill video_path from context if not provided
        if "video_path" in tool_request.arguments:
            arg_path = tool_request.arguments["video_path"]
            if arg_path in ("{{video_meta.file_path}}", "", None):
                if context.video_meta is not None:
                    tool_request.arguments["video_path"] = context.video_meta.file_path
                    logger.debug(
                        "[expert_agent:_execute_tool_calls] AUTO_FILL_VIDEO_PATH | %s",
                        context.video_meta.file_path,
                    )

        # Execute tool
        try:
            tool_response = router.route(tool_request)
        except Exception as exc:
            logger.error(
                "[expert_agent:_execute_tool_calls] ROUTE_FAILED | tool=%s | %s",
                tool_request.tool_name,
                exc,
                exc_info=True,
            )
            return None, None

        if not tool_response.success:
            logger.warning(
                "[expert_agent:_execute_tool_calls] TOOL_FAILED | tool=%s error=%s",
                tool_request.tool_name,
                tool_response.error,
            )
            return None, None

        # Extract annotated image path and tracking data
        result_data = tool_response.data or {}
        annotated_image = result_data.get("annotated_image_path")

        # Build tracking text for prompt injection
        tracking_text = self._format_tracking_result(result_data)

        logger.info(
            "[expert_agent:_execute_tool_calls] SUCCESS | tool=%s | "
            "annotated_image=%s displacements=%d",
            tool_request.tool_name,
            annotated_image,
            len(result_data.get("displacements", [])),
        )

        return {
            "tool_name": tool_request.tool_name,
            "result": result_data,
            "tracking_text": tracking_text,
        }, annotated_image

    def _execute_native_tool_calls(
        self,
        template: Any,
        images: List[Any],
        context_vars: Dict[str, Any],
        context: AnalysisContext,
    ) -> Optional[Tuple[Dict[str, Any], Optional[str]]]:
        """
        Execute tool calls using Anthropic Native API.
        
        Returns:
            (tool_result_dict, annotated_image_path) or None if failed/no tool call.
        """
        try:
            from traffic_analyzer.tools.tool_registry import get_default_router
            from traffic_analyzer.tools.tool_schema import ToolDefinition
        except ImportError as exc:
            logger.debug(
                "[expert_agent:_execute_native_tool_calls] IMPORT_FAILED | %s",
                exc,
            )
            return None
        
        # Get tool definitions for configured tools
        router = get_default_router()
        if router is None:
            logger.debug(
                "[expert_agent:_execute_native_tool_calls] NO_ROUTER"
            )
            return None
        
        tool_definitions = []
        for tool_name in self.category.tools:
            tool_def = router.get_tool(tool_name)
            if tool_def is None:
                logger.warning(
                    "[expert_agent:_execute_native_tool_calls] TOOL_NOT_FOUND | tool=%s",
                    tool_name,
                )
                continue
            tool_definitions.append(tool_def.to_anthropic())
        
        if not tool_definitions:
            logger.debug(
                "[expert_agent:_execute_native_tool_calls] NO_TOOL_DEFS"
            )
            return None
        
        # First call with tools
        try:
            first_response, tool_uses = self.vlm_engine.call_with_tools(
                template=template,
                images=images,
                tool_definitions=tool_definitions,
                context_vars=context_vars,
                response_schema=_EXPERT_RESPONSE_SCHEMA,
            )
        except Exception as exc:
            logger.error(
                "[expert_agent:_execute_native_tool_calls] FIRST_CALL_ERROR | event_id=%d | %s",
                self.category.event_id,
                exc,
                exc_info=True,
            )
            return None
        
        if not tool_uses:
            logger.debug(
                "[expert_agent:_execute_native_tool_calls] NO_TOOL_USES"
            )
            return None
        
        # Track the last successful tool name for fallback
        last_tool_name = ""
        
        # Execute each tool
        tool_results = []
        all_result_data = {}
        annotated_image = None
        
        for tool_use in tool_uses:
            tool_name = tool_use.get("name", "")
            tool_id = tool_use.get("id", "")
            tool_input = tool_use.get("input", {})
            
            if tool_name not in self.category.tools:
                logger.warning(
                    "[expert_agent:_execute_native_tool_calls] TOOL_NOT_ALLOWED | "
                    "requested=%s allowed=%s",
                    tool_name,
                    self.category.tools,
                )
                continue
            
            last_tool_name = tool_name
            
            # Auto-fill video_path
            if "video_path" in tool_input:
                arg_path = tool_input["video_path"]
                if arg_path in ("{{video_meta.file_path}}", "", None):
                    if context.video_meta is not None:
                        tool_input["video_path"] = context.video_meta.file_path
            
            # Execute tool
            try:
                from traffic_analyzer.tools.tool_router import ToolRequest
                tool_request = ToolRequest(
                    tool_name=tool_name,
                    arguments=tool_input,
                )
                tool_response = router.route(tool_request)
            except Exception as exc:
                logger.error(
                    "[expert_agent:_execute_native_tool_calls] EXECUTE_FAILED | tool=%s | %s",
                    tool_name,
                    exc,
                    exc_info=True,
                )
                continue
            
            if not tool_response.success:
                logger.warning(
                    "[expert_agent:_execute_native_tool_calls] TOOL_FAILED | tool=%s error=%s",
                    tool_name,
                    tool_response.error,
                )
                continue
            
            result_data = tool_response.data or {}
            all_result_data = result_data
            annotated_image = result_data.get("annotated_image_path")
            
            # Format result for VLM
            tracking_text = self._format_tracking_result(result_data)
            tool_results.append({
                "tool_use_id": tool_id,
                "content": tracking_text,
            })
            
            logger.info(
                "[expert_agent:_execute_native_tool_calls] TOOL_SUCCESS | tool=%s | "
                "annotated_image=%s displacements=%d",
                tool_name,
                annotated_image,
                len(result_data.get("displacements", [])),
            )
        
        if not tool_results:
            logger.debug(
                "[expert_agent:_execute_native_tool_calls] NO_SUCCESSFUL_TOOLS"
            )
            return None
        
        # Second call with tool results
        try:
            # Build previous messages from first call
            system_prompt, user_prompt = self.vlm_engine.render_prompt(template, context_vars)
            
            # Import the build function from vlm_engine module
            from traffic_analyzer.core.vlm_engine import _build_anthropic_payload
            _, kwargs = _build_anthropic_payload(
                system_prompt,
                user_prompt,
                images,
                self.vlm_engine.config.model,
                self.vlm_engine.config.max_tokens,
                self.vlm_engine.config.temperature,
            )
            kwargs["tools"] = tool_definitions
            kwargs["tool_choice"] = {"type": "auto"}
            
            # Get messages from first call
            previous_messages = kwargs.get("messages", [])
            
            second_response = self.vlm_engine.call_with_tool_results(
                template=template,
                images=images,
                previous_messages=previous_messages,
                tool_results=tool_results,
                context_vars=context_vars,
                response_schema=_EXPERT_RESPONSE_SCHEMA,
            )
        except Exception as exc:
            logger.error(
                "[expert_agent:_execute_native_tool_calls] SECOND_CALL_ERROR | event_id=%d | %s",
                self.category.event_id,
                exc,
                exc_info=True,
            )
            # Fallback: return first tool result without second call
            tracking_text = self._format_tracking_result(all_result_data)
            return {
                "tool_name": last_tool_name,
                "result": all_result_data,
                "tracking_text": tracking_text,
            }, annotated_image
        
        # Parse second response
        if second_response.success:
            logger.info(
                "[expert_agent:_execute_native_tool_calls] SECOND_CALL_SUCCESS | event_id=%d",
                self.category.event_id,
            )
        
        tracking_text = self._format_tracking_result(all_result_data)
        return {
            "tool_name": last_tool_name,
            "result": all_result_data,
            "tracking_text": tracking_text,
        }, annotated_image

    def _execute_anthropic_native_tools(
        self,
        template: Any,
        images: List[Any],
        context_vars: Dict[str, Any],
        context: AnalysisContext,
    ) -> Optional[Tuple[Dict[str, Any], Optional[str]]]:
        """
        Execute tool calls using Anthropic Native API directly.
        
        Returns:
            (tool_result_dict, annotated_image_path) or None if failed/no tool call.
        """
        import anthropic
        from traffic_analyzer.tools.tool_registry import get_default_router
        from traffic_analyzer.tools.tool_router import ToolRequest
        
        # Get tool definitions
        router = get_default_router()
        if router is None:
            logger.debug("[expert_agent:_execute_anthropic_native_tools] NO_ROUTER")
            return None
        
        tool_definitions = []
        for tool_name in self.category.tools:
            tool_def = router.get_tool(tool_name)
            if tool_def is None:
                logger.warning("[expert_agent:_execute_anthropic_native_tools] TOOL_NOT_FOUND | tool=%s", tool_name)
                continue
            tool_definitions.append(tool_def.to_anthropic())
        
        if not tool_definitions:
            logger.debug("[expert_agent:_execute_anthropic_native_tools] NO_TOOL_DEFS")
            return None
        
        # Render prompt
        system_prompt, user_prompt = self.vlm_engine.render_prompt(template, context_vars)
        
        # Build messages with images
        messages = []
        content = []
        if user_prompt:
            content.append({"type": "text", "text": user_prompt})
        for img in images:
            # Encode image to base64
            from traffic_analyzer.core.vlm_engine import _encode_image_to_base64
            b64_uri = _encode_image_to_base64(img)
            b64_data = b64_uri.split(",", 1)[1]
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": b64_data,
                },
            })
        messages.append({"role": "user", "content": content})
        
        # First call with tools
        client = self.vlm_engine._client
        if client is None:
            logger.error("[expert_agent:_execute_anthropic_native_tools] NO_CLIENT")
            return None
        
        try:
            response = client.messages.create(
                model=self.vlm_engine.config.model,
                max_tokens=self.vlm_engine.config.max_tokens,
                temperature=self.vlm_engine.config.temperature,
                system=system_prompt,
                messages=messages,
                tools=tool_definitions,
                tool_choice={"type": "auto"},
            )
        except Exception as exc:
            logger.error(
                "[expert_agent:_execute_anthropic_native_tools] FIRST_CALL_ERROR | event_id=%d | %s",
                self.category.event_id,
                exc,
            )
            return None
        
        # Extract tool uses (standard Anthropic API)
        tool_uses = []
        raw_text = ""
        
        # Check stop_reason first (standard Anthropic pattern)
        if getattr(response, "stop_reason", None) == "tool_use":
            for block in response.content:
                if getattr(block, "type", None) == "tool_use":
                    tool_uses.append({
                        "name": getattr(block, "name", ""),
                        "id": getattr(block, "id", ""),
                        "input": getattr(block, "input", {}),
                    })
                elif getattr(block, "type", None) == "text":
                    raw_text += block.text
        else:
            # No native tool_use — collect text for fallback parsing
            for block in response.content:
                if getattr(block, "type", None) == "text":
                    raw_text += block.text
        
        # Fallback: parse <tool_call> from text if native tool_use not available
        if not tool_uses and "<tool_call>" in raw_text:
            import re
            tool_call_match = re.search(r'<tool_call>\s*(\{.*?\})\s*</tool_call>', raw_text, re.DOTALL)
            if tool_call_match:
                try:
                    tool_json = json.loads(tool_call_match.group(1))
                    tool_uses.append({
                        "name": tool_json.get("tool_name", ""),
                        "id": "tool_call_from_text",
                        "input": tool_json.get("arguments", {}),
                    })
                    logger.info(
                        "[expert_agent:_execute_anthropic_native_tools] PARSED_FROM_TEXT | tool=%s",
                        tool_json.get("tool_name"),
                    )
                except Exception as exc:
                    logger.warning("JSON_PARSE_FAILED | %s", exc)
        
        if not tool_uses:
            logger.debug(
                "[expert_agent:_execute_anthropic_native_tools] NO_TOOL_USES | stop_reason=%s",
                getattr(response, "stop_reason", "unknown"),
            )
            return None
        
        logger.info(
            "[expert_agent:_execute_anthropic_native_tools] TOOL_USES_FOUND | count=%d",
            len(tool_uses),
        )
        
        # Execute tools
        tool_results_for_vlm = []
        all_result_data = {}
        annotated_image = None
        last_tool_name = ""
        
        for tool_use in tool_uses:
            tool_name = tool_use["name"]
            tool_id = tool_use["id"]
            tool_input = tool_use["input"]
            
            if tool_name not in self.category.tools:
                logger.warning("TOOL_NOT_ALLOWED | requested=%s allowed=%s", tool_name, self.category.tools)
                continue
            
            last_tool_name = tool_name
            
            # Auto-fill video_path
            if "video_path" in tool_input:
                arg_path = tool_input["video_path"]
                if arg_path in ("{{video_meta.file_path}}", "", None):
                    if context.video_meta is not None:
                        tool_input["video_path"] = context.video_meta.file_path
            
            # Execute
            try:
                tool_request = ToolRequest(tool_name=tool_name, arguments=tool_input)
                tool_response = router.route(tool_request)
            except Exception as exc:
                logger.error("EXECUTE_FAILED | tool=%s | %s", tool_name, exc)
                continue
            
            if not tool_response.success:
                logger.warning("TOOL_FAILED | tool=%s error=%s", tool_name, tool_response.error)
                continue
            
            result_data = tool_response.data or {}
            all_result_data = result_data
            annotated_image = result_data.get("annotated_image_path")
            
            tracking_text = self._format_tracking_result(result_data)
            tool_results_for_vlm.append({
                "tool_use_id": tool_id,
                "content": tracking_text,
            })
            
            logger.info(
                "[expert_agent:_execute_anthropic_native_tools] TOOL_SUCCESS | tool=%s displacements=%d",
                tool_name,
                len(result_data.get("displacements", [])),
            )
        
        if not tool_results_for_vlm:
            return None
        
        # Second call with tool results
        # Build message history: user -> assistant (tool_use) -> user (tool_result)
        second_messages = list(messages)
        
        # Add assistant message with tool_use
        assistant_content = []
        for tu in tool_uses:
            assistant_content.append({
                "type": "tool_use",
                "id": tu["id"],
                "name": tu["name"],
                "input": tu["input"],
            })
        second_messages.append({"role": "assistant", "content": assistant_content})
        
        # Add user message with tool_result
        user_tool_content = []
        for tr in tool_results_for_vlm:
            user_tool_content.append({
                "type": "tool_result",
                "tool_use_id": tr["tool_use_id"],
                "content": tr["content"],
            })
        second_messages.append({"role": "user", "content": user_tool_content})
        
        try:
            second_response = client.messages.create(
                model=self.vlm_engine.config.model,
                max_tokens=self.vlm_engine.config.max_tokens,
                temperature=self.vlm_engine.config.temperature,
                system=system_prompt,
                messages=second_messages,
                tools=tool_definitions,
            )
        except Exception as exc:
            logger.error(
                "[expert_agent:_execute_anthropic_native_tools] SECOND_CALL_ERROR | event_id=%d | %s",
                self.category.event_id,
                exc,
            )
            tracking_text = self._format_tracking_result(all_result_data)
            return {
                "tool_name": last_tool_name,
                "result": all_result_data,
                "tracking_text": tracking_text,
            }, annotated_image
        
        # Parse second response
        second_text = ""
        for block in second_response.content:
            if getattr(block, "type", None) == "text":
                second_text += block.text
        
        logger.info(
            "[expert_agent:_execute_anthropic_native_tools] SECOND_CALL_SUCCESS | event_id=%d text_len=%d",
            self.category.event_id,
            len(second_text),
        )
        
        # Try to parse JSON from second response
        from traffic_analyzer.core.vlm_engine import _extract_json_from_text
        try:
            parsed = _extract_json_from_text(second_text)
            logger.debug("JSON_PARSED | detected=%s", parsed.get("detected", "unknown"))
        except Exception as exc:
            logger.debug("JSON_PARSE_FAILED | %s", exc)
        
        tracking_text = self._format_tracking_result(all_result_data)
        return {
            "tool_name": last_tool_name,
            "result": all_result_data,
            "tracking_text": tracking_text,
        }, annotated_image

    def _format_tracking_result(self, result_data: Dict[str, Any]) -> str:
        """Format YOLO tracking result into human-readable text for prompt injection."""
        lines = ["=== YOLO 车辆跟踪结果 ===", ""]

        displacements = result_data.get("displacements", [])
        if not displacements:
            lines.append("未检测到车辆跟踪数据。")
            return "\n".join(lines)

        lines.append(f"共跟踪到 {len(displacements)} 辆车：")
        lines.append("")

        for disp in displacements:
            track_id = disp.get("track_id", "?")
            cls = disp.get("class", "unknown")
            direction = disp.get("direction_text", "未知")
            distance = disp.get("distance_pixels", 0)
            is_stationary = disp.get("is_stationary", False)
            stationary_str = "静止" if is_stationary else "移动"

            lines.append(
                f"  track_id={track_id} ({cls}): {stationary_str}, "
                f"方向={direction}, 总位移={distance:.1f}px"
            )

        lines.append("")
        lines.append(
            "附带的最后一张图是 YOLO 跟踪标注帧，框内蓝色数字为 track_id，"
            "与上述数据中的 track_id 对应。"
        )

        return "\n".join(lines)

    def _second_vlm_call(
        self,
        template: PromptTemplate,
        first_response: Any,
        tool_result: Dict[str, Any],
        annotated_image: Optional[str],
        images: List[Any],
        context_vars: Dict[str, Any],
    ) -> EventCandidate:
        """Perform second VLM call with tool results injected.

        Light-weight context: includes first response summary + tool results.
        """
        # Build enhanced prompt with tool results
        enhanced_user = template.user_prompt or ""

        # Add first response context (light-weight)
        first_text = first_response.raw_text if hasattr(first_response, "raw_text") else ""
        context_section = (
            "\n\n============================================================\n"
            "上下文 — 第一次分析结论\n"
            "============================================================\n"
            f"{first_text[:500]}...\n"
            "\n以上是你之前的分析。现在基于新的跟踪数据，请重新判断。"
        )

        # Add tool results
        tool_section = (
            "\n\n============================================================\n"
            "工具调用结果 — YOLO 车辆跟踪数据\n"
            "============================================================\n"
            f"{tool_result['tracking_text']}\n"
            "\n【重要】基于以上跟踪数据，重新判断并输出 JSON。必须包含 detected 字段。"
        )

        enhanced_user += context_section + tool_section

        # Build second template
        second_template = PromptTemplate(
            template_id=template.template_id,
            name=template.name,
            version=template.version,
            system_prompt=template.system_prompt,
            user_prompt=enhanced_user,
            output_format_hint=template.output_format_hint,
            example_input=template.example_input,
            example_output=template.example_output,
            traffic_percentage=template.traffic_percentage,
            available_tools=[],  # Don't show tools again in second call
        )

        # Build image list: annotated image + first/last original frames for context
        second_images = []
        if images:
            # Add first and last frame for temporal context
            second_images.append(images[0])
            if len(images) > 1:
                second_images.append(images[-1])
        if annotated_image:
            second_images.append(annotated_image)
            logger.debug(
                "[expert_agent:_second_vlm_call] ADDED_ANNOTATED_IMAGE | path=%s",
                annotated_image,
            )

        # Second VLM call
        try:
            second_response = self.vlm_engine.call(
                template=second_template,
                images=second_images,
                context_vars=context_vars,
                response_schema=_EXPERT_RESPONSE_SCHEMA,
            )
        except Exception as exc:
            logger.error(
                "[expert_agent:_second_vlm_call] SECOND_VLM_ERROR | event_id=%d | %s",
                self.category.event_id,
                exc,
                exc_info=True,
            )
            # Fallback to first response
            return parse_expert_response(first_response, self.category)

        # Parse second response
        candidate = parse_expert_response(second_response, self.category)
        
        # Store tool results for report generation
        candidate.tool_results = [tool_result] if tool_result else []
        
        # Build raw_vlm_text (handle None first_response for Native API path)
        first_text = getattr(first_response, 'raw_text', '[Native API tool call]') if first_response else '[Native API tool call]'
        candidate.raw_vlm_text = (
            f"[First call]\n{first_text}\n\n"
            f"[Second call with tool results]\n{second_response.raw_text}"
        )
        logger.info(
            "[expert_agent:_second_vlm_call] COMPLETE | event_id=%d detected=%s confidence=%.2f",
            self.category.event_id,
            candidate.detected,
            candidate.confidence,
        )
        return candidate
