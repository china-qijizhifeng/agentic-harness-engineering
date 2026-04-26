"""Canonical trace parsing and normalization helpers.

Vendored from agent_debugger_core.runtime.trace_converter to avoid an
extra package dependency.
"""

from __future__ import annotations

import json
import re
import traceback
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from nexau.archs.tracer.adapters.in_memory import InMemoryTracer
except ImportError:  # pragma: no cover
    InMemoryTracer = Any  # type: ignore[misc,assignment]


# ---------------------------------------------------------------------------
# make_jsonable (inlined from agent_debugger_core.utils.json_compat)
# ---------------------------------------------------------------------------

def make_jsonable(value: Any, *, _seen: set[int] | None = None) -> Any:
    """Best-effort convert *value* into something ``json.dumps`` can handle."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if _seen is None:
        _seen = set()
    obj_id = id(value)
    if obj_id in _seen:
        return str(value)
    _seen.add(obj_id)
    if isinstance(value, dict):
        return {str(k): make_jsonable(v, _seen=_seen) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [make_jsonable(v, _seen=_seen) for v in value]
    return str(value)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LLM_SPAN_NAME_KEYWORDS = (
    "openai",
    "anthropic",
    "gemini",
    "gpt",
    "llama",
)
TOOL_NAME_PREFIXES = ("tool:", "tool_", "tool ")
_MISSING = object()


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _json_fallback(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def coerce_observations(value: Any) -> List[Dict[str, Any]]:
    """Best-effort normalize serialized observations into list[dict]."""
    if value is None:
        return []

    if isinstance(value, list):
        out: List[Dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                out.append(item)
                continue
            if not isinstance(item, str):
                continue
            text = item.strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(parsed, dict):
                out.append(parsed)
            elif isinstance(parsed, list):
                out.extend(entry for entry in parsed if isinstance(entry, dict))
        return out

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:  # noqa: BLE001
            return []
        if isinstance(parsed, list):
            return [entry for entry in parsed if isinstance(entry, dict)]
        if isinstance(parsed, dict):
            nested = parsed.get("data")
            if isinstance(nested, list):
                return [entry for entry in nested if isinstance(entry, dict)]
            return [parsed]
        return []

    if isinstance(value, dict):
        nested = value.get("data")
        if isinstance(nested, list):
            return [entry for entry in nested if isinstance(entry, dict)]
        return [value]

    return []


def extract_text_from_message(msg: Dict[str, Any]) -> str:
    """Extract plain text from a message payload."""
    content = msg.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                else:
                    parts.append(json.dumps(item, ensure_ascii=False, default=_json_fallback))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    try:
        return json.dumps(content, ensure_ascii=False, default=_json_fallback)
    except Exception:  # noqa: BLE001
        return str(content)


def extract_reasoning_content_from_message(message: Any) -> Any:
    """Best-effort extraction of reasoning content from assistant payloads."""
    if not isinstance(message, dict):
        return _MISSING

    for key in ("reasoning_content", "reasoningContent", "reasoning"):
        if key in message:
            return message.get(key)

    content = message.get("content")
    if not isinstance(content, list):
        return _MISSING

    reasoning_items: List[Any] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").lower()
        if "reasoning" not in item_type:
            continue
        text = item.get("text")
        reasoning_items.append(text if isinstance(text, str) else item)

    if not reasoning_items:
        return _MISSING
    if len(reasoning_items) == 1:
        return reasoning_items[0]
    return reasoning_items


def extract_reasoning_tokens(output: Any) -> Any:
    """Return reasoning token usage when the span reports it."""
    if not isinstance(output, dict):
        return _MISSING

    usage = output.get("usage")
    if not isinstance(usage, dict):
        return _MISSING

    for key in ("reasoning_tokens", "reasoningTokens"):
        if key in usage:
            return usage.get(key)
    return _MISSING


def normalize_input_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=_json_fallback)
    except Exception:  # noqa: BLE001
        return str(value)


def strip_trailing_tool_call(text: str) -> str:
    """Remove trailing tool call XML blocks from assistant text."""
    if not text:
        return text

    text = re.sub(
        r"\n\n<tool_use>.*?</tool_use>\s*",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )

    lower = text.lower()
    end_tag = "</use_parallel_tool_calls>"
    idx = lower.rfind(end_tag)
    if idx != -1:
        return text[:idx].rstrip()
    return text


# ---------------------------------------------------------------------------
# OpenAI / Responses-API helpers
# ---------------------------------------------------------------------------

def _get_assistant_from_responses_api_output_items(items: Any) -> Optional[Dict[str, Any]]:
    """Extract the assistant message from OpenAI Responses API output items."""
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message":
            continue
        if item.get("role") != "assistant":
            continue
        return item
    return None


def _get_responses_api_output_items(output: Any) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(output, dict):
        return None
    items = output.get("output")
    if not isinstance(items, list):
        return None
    return [item for item in items if isinstance(item, dict)]


def _has_responses_api_llm_items(output: Any) -> bool:
    items = _get_responses_api_output_items(output)
    if not items:
        return False
    return any(
        str(item.get("type") or "").lower() in {"message", "function_call", "reasoning"}
        for item in items
    )


def get_assistant_from_openai_generation_output(output: Any) -> Optional[Dict[str, Any]]:
    """Extract an assistant message from OpenAI/Anthropic style outputs."""
    if isinstance(output, str):
        return {"role": "assistant", "content": output}
    if output is None:
        return {"role": "assistant", "content": ""}
    if not isinstance(output, (dict, list)):
        return None

    if isinstance(output, list) and output:
        first_item = output[0]
        if isinstance(first_item, dict) and first_item.get("role") == "assistant":
            return first_item

    if isinstance(output, dict):
        if output.get("role") == "assistant" and "content" in output:
            return output
        response_message = _get_assistant_from_responses_api_output_items(output.get("output"))
        if response_message is not None:
            return response_message
        if len(output) == 1:
            for value in output.values():
                if isinstance(value, list) and value:
                    first_item = value[0]
                    if isinstance(first_item, dict) and first_item.get("role") == "assistant":
                        return first_item

        if "choices" in output:
            first_choice = output["choices"][0]
            if isinstance(first_choice, dict):
                message = first_choice.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant":
                    return message

        message = output.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            return message

        messages = output.get("messages")
        if isinstance(messages, list):
            for message_item in reversed(messages):
                if isinstance(message_item, dict) and message_item.get("role") == "assistant":
                    return message_item

        content = output.get("content")
        if isinstance(content, str):
            return {"role": "assistant", "content": content}

    return None


def normalize_generation_input(input_obj: Any) -> Dict[str, Any]:
    """Normalize generation input payload into a dict."""
    if isinstance(input_obj, dict):
        args = input_obj.get("args")
        if isinstance(args, list) and args:
            first = args[0]
            if isinstance(first, dict):
                return first
        return input_obj
    if isinstance(input_obj, list) and input_obj:
        first = input_obj[0]
        if isinstance(first, dict):
            return first
    return {}


def get_messages_from_openai_generation_input(input_obj: Any) -> List[Dict[str, Any]]:
    """Extract message list from OpenAI generation input."""
    if not isinstance(input_obj, (dict, list)):
        return []

    if isinstance(input_obj, list):
        return [message for message in input_obj if isinstance(message, dict)]

    messages = input_obj.get("messages", [])
    if isinstance(messages, list):
        return [message for message in messages if isinstance(message, dict)]

    response_input = input_obj.get("input")
    if isinstance(response_input, list):
        return [
            message
            for message in response_input
            if isinstance(message, dict) and bool(str(message.get("role") or "").strip())
        ]
    if isinstance(response_input, dict) and bool(str(response_input.get("role") or "").strip()):
        return [response_input]
    return []


# ---------------------------------------------------------------------------
# Span classification & sorting
# ---------------------------------------------------------------------------

def is_llm_span(obs: Dict[str, Any]) -> bool:
    """Identify spans that correspond to LLM calls."""
    span_type = (obs.get("type") or "").upper()
    span_kind = (obs.get("span_type") or "").upper()
    if span_type not in ("SPAN", "LLM", "GENERATION") and span_kind != "LLM":
        return False

    output = obs.get("output")
    has_llm_output = (
        get_assistant_from_openai_generation_output(output) is not None
        or _has_responses_api_llm_items(output)
    )

    if not has_llm_output:
        return False

    name = (obs.get("name") or "").lower()
    return any(keyword in name for keyword in LLM_SPAN_NAME_KEYWORDS)


def is_tool_span(obs: Dict[str, Any]) -> bool:
    """Identify spans that correspond to tool calls."""
    name = (obs.get("name") or "").lower()
    span_kind = (obs.get("span_type") or "").upper()
    if span_kind == "TOOL":
        return True
    return any(name.startswith(prefix) for prefix in TOOL_NAME_PREFIXES)


def _sort_key(obs: Dict[str, Any]) -> Any:
    for attr in ("startTime", "start_time", "createdAt", "created_at"):
        value = obs.get(attr)
        if value is not None:
            return value
    return 0


def sort_observations(observations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(observations, key=_sort_key)


# ---------------------------------------------------------------------------
# High-level extraction
# ---------------------------------------------------------------------------

def get_system_prompt_from_observations(observations: List[Dict[str, Any]]) -> str:
    """Extract the first system prompt from an observation list."""
    for obs in sort_observations(observations):
        if not is_llm_span(obs):
            continue

        obs_input = normalize_generation_input(obs.get("input", {}) or {})
        messages = get_messages_from_openai_generation_input(obs_input)
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "system":
                return extract_text_from_message(message)
        instructions = obs_input.get("instructions")
        if isinstance(instructions, str) and instructions.strip():
            return instructions
        break
    return ""


def extract_user_message_from_trace_input(value: Any) -> str:
    if isinstance(value, dict):
        message = value.get("message")
        if isinstance(message, str) and message.strip():
            return message

        messages = value.get("messages")
        if isinstance(messages, list):
            for entry in reversed(messages):
                if not isinstance(entry, dict) or entry.get("role") != "user":
                    continue
                text = extract_text_from_message(entry)
                if text:
                    return text

        response_input = value.get("input")
        if isinstance(response_input, list):
            for entry in reversed(response_input):
                if not isinstance(entry, dict) or entry.get("role") != "user":
                    continue
                text = extract_text_from_message(entry)
                if text:
                    return text

    return normalize_input_to_text(value)


def _is_subagent_tool_name(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith("agent_") or lowered.startswith("sub-agent")


def _is_metadata_subagent_observation(observation: Dict[str, Any]) -> bool:
    metadata = observation.get("metadata") or {}
    if not isinstance(metadata, dict):
        return False
    return bool(metadata.get("subagent_id")) and bool(metadata.get("controller_observation_id"))


def build_agent_turns_from_observations(observations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build assistant turns and attach following tool calls."""
    if not observations:
        return []

    sorted_obs = sort_observations(observations)
    agent_turns: List[Dict[str, Any]] = []

    index = 0
    total = len(sorted_obs)
    while index < total:
        observation = sorted_obs[index]
        if not is_llm_span(observation):
            index += 1
            continue

        assistant_msg = get_assistant_from_openai_generation_output(observation.get("output") or {})
        agent_text = ""
        if assistant_msg:
            agent_text = strip_trailing_tool_call(extract_text_from_message(assistant_msg))

        next_index = index + 1
        tool_calls: List[Dict[str, Any]] = []
        while next_index < total:
            next_obs = sorted_obs[next_index]
            if is_llm_span(next_obs):
                break

            if is_tool_span(next_obs):
                next_name = str(next_obs.get("name") or "")
                tool_call: Dict[str, Any] = {
                    "id": next_obs.get("id"),
                    "name": next_name,
                    "type": "tool",
                    "input": next_obs.get("input", {}) or {},
                    "output": next_obs.get("output", {}) or {},
                    "startTime": next_obs.get("startTime") or next_obs.get("start_time"),
                    "endTime": next_obs.get("endTime") or next_obs.get("end_time"),
                    "latency": next_obs.get("latency"),
                }
                if _is_subagent_tool_name(next_name):
                    tool_call["is_subagent"] = True
                    tool_call["subagent_observation_id"] = next_obs.get("id")
                tool_calls.append(tool_call)

            next_index += 1

        turn: Dict[str, Any] = {
            "role": assistant_msg["role"] if assistant_msg else "assistant",
            "content": agent_text,
            "tool_calls": tool_calls,
            "openai_obs_id": observation.get("id"),
            "startTime": observation.get("startTime") or observation.get("start_time"),
            "endTime": observation.get("endTime") or observation.get("end_time"),
            "latency": observation.get("latency"),
            "model": observation.get("model"),
        }
        reasoning_content = extract_reasoning_content_from_message(assistant_msg)
        if reasoning_content is not _MISSING:
            turn["reasoning_content"] = reasoning_content
        reasoning_tokens = extract_reasoning_tokens(observation.get("output"))
        if reasoning_tokens is not _MISSING:
            turn["reasoning_tokens"] = reasoning_tokens
        agent_turns.append(turn)
        index = next_index

    return agent_turns


def extract_subagents_from_observations(observations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extract sub-agent trajectories from both metadata and parent/child heuristics."""
    if not observations:
        return []

    children_index: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for observation in observations:
        parent_id = observation.get("parentObservationId") or observation.get("parent_id")
        if parent_id:
            children_index[str(parent_id)].append(observation)

    subagents: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()

    metadata_index: Dict[str, Dict[str, Any]] = {}
    metadata_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for observation in sort_observations(observations):
        metadata = observation.get("metadata") or {}
        if not isinstance(metadata, dict):
            continue

        subagent_id = metadata.get("subagent_id")
        controller_observation_id = metadata.get("controller_observation_id")
        if not subagent_id or not controller_observation_id:
            continue

        key = str(subagent_id)
        metadata_groups[key].append(observation)
        metadata_index.setdefault(
            key,
            {
                "id": subagent_id,
                "name": metadata.get("subagent_name"),
                "mode": metadata.get("subagent_mode"),
                "controller_observation_id": controller_observation_id,
            },
        )

    for key, entry in metadata_index.items():
        messages = build_agent_turns_from_observations(metadata_groups[key])
        if not messages:
            continue
        subagents.append({**entry, "messages": messages})
        seen_ids.add(key)

    for observation in sort_observations(observations):
        name = str(observation.get("name") or "")
        if not name.startswith("agent_"):
            continue

        agent_id = observation.get("id")
        agent_key = str(agent_id) if agent_id is not None else ""
        if not agent_key or agent_key in seen_ids:
            continue

        sub_obs = children_index.get(agent_key, [])
        messages = build_agent_turns_from_observations(sub_obs) if sub_obs else []
        if not messages:
            continue

        subagents.append(
            {
                "id": agent_id,
                "name": name,
                "mode": "agent_prefix",
                "controller_observation_id": None,
                "messages": messages,
            }
        )
        seen_ids.add(agent_key)

    for observation in sort_observations(observations):
        name = str(observation.get("name") or "")
        if not name.lower().startswith("sub-agent"):
            continue

        controller_id = observation.get("id")
        controller_key = str(controller_id) if controller_id is not None else ""
        if not controller_key:
            continue

        for child in children_index.get(controller_key, []):
            child_id = child.get("id")
            child_key = str(child_id) if child_id is not None else ""
            if not child_key or child_key in seen_ids:
                continue

            sub_obs = children_index.get(child_key, [])
            messages = build_agent_turns_from_observations(sub_obs) if sub_obs else []
            if not messages:
                continue

            subagents.append(
                {
                    "id": child_id,
                    "name": child.get("name") or name,
                    "mode": "sub_agent_prefix",
                    "controller_observation_id": controller_id,
                    "messages": messages,
                }
            )
            seen_ids.add(child_key)

    return subagents


def extract_tool_definitions_from_observations(observations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collect unique tool definitions from LLM spans."""
    tool_definitions: List[Dict[str, Any]] = []
    seen = set()

    for observation in observations:
        if not is_llm_span(observation):
            continue

        obs_input = normalize_generation_input(observation.get("input", {}) or {})
        tools = obs_input.get("tools")
        if not isinstance(tools, list):
            continue

        for tool in tools:
            if not isinstance(tool, dict):
                continue
            serialized = json.dumps(tool, sort_keys=True, ensure_ascii=False, default=_json_fallback)
            if serialized in seen:
                continue
            seen.add(serialized)
            tool_definitions.append(tool)

    return tool_definitions


def get_first_observation_model(observations: List[Dict[str, Any]]) -> Optional[str]:
    """Infer model name from the first LLM observation, if possible."""
    for obs in observations:
        if not is_llm_span(obs):
            continue

        obs_input = normalize_generation_input(obs.get("input", {}) or {})
        model = obs_input.get("model")
        if isinstance(model, str) and model:
            return model
        if model:
            return str(model)

        metadata = obs.get("metadata")
        if isinstance(metadata, dict):
            meta_model = metadata.get("model")
            if isinstance(meta_model, str) and meta_model:
                return meta_model
            if meta_model:
                return str(meta_model)

    return None


def _sum_total_tokens(observations: List[Dict[str, Any]]) -> int:
    total_tokens = 0
    for observation in observations:
        span_total_tokens = observation.get("totalTokens")
        if span_total_tokens and span_total_tokens != "N/A":
            try:
                total_tokens += int(span_total_tokens)
            except (TypeError, ValueError):
                pass
            continue

        output = observation.get("output") or {}
        if not isinstance(output, dict):
            continue
        usage = output.get("usage") or {}
        if not isinstance(usage, dict):
            continue
        nested_total = usage.get("total_tokens") or usage.get("totalTokens")
        if nested_total and nested_total != "N/A":
            try:
                total_tokens += int(nested_total)
            except (TypeError, ValueError):
                pass
    return total_tokens


def _sum_total_cost(observations: List[Dict[str, Any]]) -> float:
    total_cost = 0.0
    for observation in observations:
        value = observation.get("calculatedTotalCost")
        if not value or value == "N/A":
            continue
        try:
            total_cost += float(value)
        except (TypeError, ValueError):
            pass
    return total_cost


def _normalize_observations(trace: Dict[str, Any], *, coerce_observation_payloads: bool) -> List[Dict[str, Any]]:
    observations = trace.get("observations", []) or []
    if coerce_observation_payloads:
        return coerce_observations(observations)
    return [entry for entry in observations if isinstance(entry, dict)]


def _extract_trace_data_impl(
    trace: Dict[str, Any],
    *,
    coerce_observation_payloads: bool,
    include_system_prompt_message: bool,
    include_user_message: bool,
    include_langfuse_metadata: bool,
) -> Dict[str, Any]:
    cleaned_trace: Dict[str, Any] = {
        "id": trace.get("id") or trace.get("trace_id") or "N/A",
        "timestamp": trace.get("timestamp", "N/A"),
        "name": trace.get("name", "N/A"),
        "input": trace.get("input", "N/A"),
        "output": trace.get("output", "N/A"),
        "latency": trace.get("latency", "N/A"),
    }
    if include_langfuse_metadata:
        cleaned_trace.update(
            {
                "totalCost": trace.get("totalCost", "N/A"),
                "sessionId": trace.get("sessionId", "N/A"),
                "userId": trace.get("userId", "N/A"),
                "projectId": trace.get("projectId", "N/A"),
            }
        )

    observations = _normalize_observations(
        trace,
        coerce_observation_payloads=coerce_observation_payloads,
    )
    main_observations = [obs for obs in observations if not _is_metadata_subagent_observation(obs)]
    system_prompt = get_system_prompt_from_observations(main_observations)
    agent_turns = build_agent_turns_from_observations(main_observations)
    subagents = extract_subagents_from_observations(observations)
    tool_definitions = extract_tool_definitions_from_observations(main_observations)
    first_observation_model = get_first_observation_model(main_observations)

    messages: List[Dict[str, Any]] = []
    user_message_text: Optional[str] = None
    if include_system_prompt_message and system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if include_user_message:
        user_message_text = extract_user_message_from_trace_input(trace.get("input", "N/A"))
        if user_message_text and user_message_text != "N/A":
            messages.append({"role": "user", "content": user_message_text})
    messages.extend(agent_turns)

    total_tokens = _sum_total_tokens(observations)
    total_cost = _sum_total_cost(observations)
    generation_count = len([obs for obs in observations if is_llm_span(obs)])

    cleaned_trace.update(
        {
            "system_prompt": system_prompt,
            "messages_count": len(messages),
            "messages": messages,
            "total_tokens": total_tokens if total_tokens > 0 else "N/A",
            "observation_count": len(observations),
            "generation_count": generation_count,
            "subagents": subagents,
            "tool_definitions": tool_definitions,
        }
    )
    if include_user_message:
        cleaned_trace["user_message"] = user_message_text or ""
        cleaned_trace["calculated_total_cost"] = total_cost if total_cost > 0 else "N/A"
    if first_observation_model is not None:
        cleaned_trace["model"] = first_observation_model
    return cleaned_trace


def extract_trace_data(
    trace: Dict[str, Any],
    *,
    coerce_observation_payloads: bool = False,
    include_system_prompt_message: bool = False,
    include_user_message: bool = False,
    include_langfuse_metadata: bool = False,
    capture_errors: bool = False,
    jsonable_output: bool = False,
) -> Dict[str, Any]:
    """Extract a cleaned trace dict from normalized observations."""
    if not capture_errors:
        cleaned_trace = _extract_trace_data_impl(
            trace,
            coerce_observation_payloads=coerce_observation_payloads,
            include_system_prompt_message=include_system_prompt_message,
            include_user_message=include_user_message,
            include_langfuse_metadata=include_langfuse_metadata,
        )
    else:
        cleaned_trace = {
            "id": trace.get("id") or trace.get("trace_id") or "N/A",
            "timestamp": trace.get("timestamp", "N/A"),
            "name": trace.get("name", "N/A"),
            "input": trace.get("input", "N/A"),
            "output": trace.get("output", "N/A"),
            "latency": trace.get("latency", "N/A"),
        }
        if include_langfuse_metadata:
            cleaned_trace.update(
                {
                    "totalCost": trace.get("totalCost", "N/A"),
                    "sessionId": trace.get("sessionId", "N/A"),
                    "userId": trace.get("userId", "N/A"),
                    "projectId": trace.get("projectId", "N/A"),
                }
            )
        try:
            cleaned_trace = _extract_trace_data_impl(
                trace,
                coerce_observation_payloads=coerce_observation_payloads,
                include_system_prompt_message=include_system_prompt_message,
                include_user_message=include_user_message,
                include_langfuse_metadata=include_langfuse_metadata,
            )
        except Exception:  # noqa: BLE001
            cleaned_trace["extract_error"] = traceback.format_exc()

    if jsonable_output:
        return make_jsonable(cleaned_trace)
    return cleaned_trace


def flatten_inmemory_spans(raw_traces: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten nested InMemoryTracer spans to observation-like dicts."""
    observations: List[Dict[str, Any]] = []

    def _walk(span: Dict[str, Any], parent_id: Optional[str]) -> None:
        observation = {
            "id": span.get("id"),
            "name": span.get("name"),
            "type": "SPAN",
            "span_type": span.get("type"),
            "parentObservationId": parent_id,
            "startTime": span.get("start_time"),
            "endTime": span.get("end_time"),
            "latency": span.get("duration_ms"),
            "input": span.get("inputs") or {},
            "output": span.get("outputs") or {},
            "attributes": span.get("attributes") or {},
            "error": span.get("error"),
            "model": (span.get("attributes") or {}).get("model"),
        }
        observations.append(observation)
        for child in span.get("children", []) or []:
            if isinstance(child, dict):
                _walk(child, span.get("id"))

    for root in raw_traces:
        if isinstance(root, dict):
            _walk(root, None)

    return observations


def extract_trace_data_from_inmemory_dump(
    raw: Any,
    *,
    include_system_prompt_message: bool = False,
    include_user_message: bool = False,
    include_langfuse_metadata: bool = False,
    capture_errors: bool = False,
    jsonable_output: bool = False,
) -> Dict[str, Any]:
    """Extract a cleaned trace dict from an InMemoryTracer dump."""
    if isinstance(raw, dict):
        if isinstance(raw.get("observations"), list):
            return extract_trace_data(
                raw,
                include_system_prompt_message=include_system_prompt_message,
                include_user_message=include_user_message,
                include_langfuse_metadata=include_langfuse_metadata,
                capture_errors=capture_errors,
                jsonable_output=jsonable_output,
            )
        raise TypeError("Unsupported dict format: expected key 'observations' as a list.")

    if not isinstance(raw, list):
        raise TypeError(f"Unsupported dump format: expected list or dict, got {type(raw)!r}.")

    root = raw[0] if raw else {}
    observations = flatten_inmemory_spans(raw)
    return extract_trace_data(
        {
            "id": root.get("id") if isinstance(root, dict) else "in_memory_trace",
            "timestamp": root.get("start_time") if isinstance(root, dict) else None,
            "name": root.get("name") if isinstance(root, dict) else None,
            "input": root.get("inputs") if isinstance(root, dict) else None,
            "output": root.get("outputs") if isinstance(root, dict) else None,
            "latency": root.get("duration_ms") if isinstance(root, dict) else None,
            "observations": observations,
        },
        include_system_prompt_message=include_system_prompt_message,
        include_user_message=include_user_message,
        include_langfuse_metadata=include_langfuse_metadata,
        capture_errors=capture_errors,
        jsonable_output=jsonable_output,
    )
