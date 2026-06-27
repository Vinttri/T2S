"""Utility functions for agents."""

import json
import logging
import os
import time
from typing import Any, Dict, List

from litellm import completion
from api.config import Config


def _agent_call_timeout() -> int:
    """Per-call cap (seconds) for an agent LLM call. Lower than the configured
    COMPLETION timeout (often 600s) so a STALLED call fails fast and the pipeline
    can fall back / still finish within the client budget — instead of one hung
    completion eating the whole request (the 8-min stall that timed out case 7).
    Above the legit max single-call latency (~210s) so real calls are not cut."""
    try:
        return max(60, min(580, int(os.getenv("AGENT_LLM_TIMEOUT_SECONDS", "280") or 280)))
    except (ValueError, TypeError):
        return 280


def run_completion(messages: List[Dict[str, str]], custom_model: str = None,
                   custom_api_key: str = None, **kwargs) -> str:
    """Run an LLM completion with optional custom model/key overrides.

    Returns the content string from the first choice.
    """
    kwargs.setdefault("timeout", _agent_call_timeout())
    completion_args = Config.completion_kwargs(
        custom_model=custom_model,
        custom_api_key=custom_api_key,
        messages=messages,
        top_p=1,
        **kwargs,
    )

    # Temperature is a UI/Settings value (COMPLETION_TEMPERATURE) like every
    # other model setting. Most agents pass temperature=0 to mean "deterministic
    # default"; defer that to the configured value so the Settings-page
    # temperature actually drives the whole pipeline. An explicit NON-zero temp
    # from a special-purpose agent (e.g. healer, follow-up) is respected as-is.
    if not completion_args.get("temperature"):
        completion_args["temperature"] = Config.COMPLETION_TEMPERATURE

    last_result = None
    max_attempts = Config.LLM_COMPLETION_MAX_ATTEMPTS
    for attempt in range(max_attempts):
        started = time.perf_counter()
        logging.info(
            "LLM completion started: model=%s attempt=%d/%d timeout=%s retries=%s",
            completion_args["model"],
            attempt + 1,
            max_attempts,
            completion_args.get("timeout"),
            completion_args.get("num_retries"),
        )
        try:
            result = completion(**completion_args)
        except Exception as exc:
            elapsed = time.perf_counter() - started
            logging.error(
                "LLM completion failed: model=%s attempt=%d/%d elapsed=%.2fs error=%s",
                completion_args["model"],
                attempt + 1,
                max_attempts,
                elapsed,
                str(exc)[:300],
            )
            raise
        elapsed = time.perf_counter() - started
        logging.info(
            "LLM completion finished: model=%s attempt=%d/%d elapsed=%.2fs",
            completion_args["model"],
            attempt + 1,
            max_attempts,
            elapsed,
        )
        last_result = result
        content = result.choices[0].message.content
        if content:
            return content

        logging.warning(
            "LLM completion returned empty content on attempt %d/%d for model %s",
            attempt + 1,
            max_attempts,
            completion_args["model"],
        )
        time.sleep(0.5 * (attempt + 1))

    finish_reason = None
    try:
        finish_reason = last_result.choices[0].finish_reason
    except (AttributeError, IndexError, TypeError):
        pass
    raise ValueError(f"LLM completion returned empty content; finish_reason={finish_reason}")


def run_tool_completion(messages: List[Dict[str, str]], tools: list,
                        custom_model: str = None, custom_api_key: str = None,
                        tool_name: str = None, **kwargs) -> str:
    """Run an LLM completion that answers by CALLING A TOOL, and return the tool
    call's raw arguments (a JSON object string).

    Forcing the answer through a function call means the structured output is
    assembled by the runtime, not hand-written by the model: it is valid JSON out
    of the box (no control-character parse failures, no retry cascade) and the
    model spends no tokens emitting the wrapper schema. Falls back to plain
    message content when the model replies with text instead of a tool call.
    """
    # Force a tool call with the STRING form "required" — several OpenAI-compatible
    # local servers (LM Studio) reject the object form
    # {"type":"function","function":{"name":...}} with HTTP 400 "Invalid tool_choice
    # type: 'object'". With a single tool in the list, "required" already pins it.
    tool_choice: Any = "required"
    kwargs.setdefault("timeout", _agent_call_timeout())
    completion_args = Config.completion_kwargs(
        custom_model=custom_model,
        custom_api_key=custom_api_key,
        messages=messages,
        tools=tools,
        tool_choice=tool_choice,
        **kwargs,
    )
    if not completion_args.get("temperature"):
        completion_args["temperature"] = Config.COMPLETION_TEMPERATURE

    max_attempts = Config.LLM_COMPLETION_MAX_ATTEMPTS
    for attempt in range(max_attempts):
        started = time.perf_counter()
        logging.info(
            "LLM tool completion started: model=%s attempt=%d/%d tool=%s",
            completion_args["model"], attempt + 1, max_attempts, tool_name or "(any)",
        )
        try:
            result = completion(**completion_args)
        except Exception as exc:
            logging.error("LLM tool completion failed: %s", str(exc)[:300])
            raise
        elapsed = time.perf_counter() - started
        message = result.choices[0].message
        tool_calls = getattr(message, "tool_calls", None) or []
        if tool_calls:
            args = tool_calls[0].function.arguments
            logging.info(
                "LLM tool completion finished: model=%s attempt=%d/%d elapsed=%.2fs args_chars=%d",
                completion_args["model"], attempt + 1, max_attempts, elapsed, len(args or ""),
            )
            if args:
                return args
        content = getattr(message, "content", None)
        if content:
            logging.warning(
                "LLM tool completion returned text, not a tool call (%.2fs); using content", elapsed,
            )
            return content
        logging.warning(
            "LLM tool completion empty on attempt %d/%d for model %s",
            attempt + 1, max_attempts, completion_args["model"],
        )
        time.sleep(0.5 * (attempt + 1))
    raise ValueError("LLM tool completion returned empty content (no tool call, no text)")


def run_tool_loop(messages: List[Dict[str, Any]], tools: list, handlers: dict,
                  final_tools, custom_model: str = None, custom_api_key: str = None,
                  max_steps: int = 4, **kwargs):
    """Multi-step tool loop for an agent that may READ/SEARCH before it finishes.

    The model calls tools; a read/search tool present in ``handlers`` is executed
    (its string result is fed back as a ``role:tool`` message) and the loop
    continues; calling one of ``final_tools`` ENDS the loop. Bounded by
    ``max_steps`` — on the last step only the final tools are offered, so a weak
    model cannot loop forever. This is what lets an agent fetch a missing column
    (doubt -> look) instead of guessing, while staying deterministic-bounded.

    Returns ``(final_tool_name, arguments_str)`` when a final tool is called,
    ``(None, content)`` if the model answers with text, or ``(None, None)`` if it
    never produced a usable call.
    """
    msgs = list(messages)
    final_set = set(final_tools)
    kwargs.setdefault("timeout", _agent_call_timeout())
    for step in range(max_steps):
        last = step == max_steps - 1
        active = [t for t in tools
                  if (not last) or t["function"]["name"] in final_set]
        completion_args = Config.completion_kwargs(
            custom_model=custom_model, custom_api_key=custom_api_key,
            messages=msgs, tools=active, tool_choice="required", **kwargs,
        )
        if not completion_args.get("temperature"):
            completion_args["temperature"] = Config.COMPLETION_TEMPERATURE
        started = time.perf_counter()
        try:
            result = completion(**completion_args)
        except Exception as exc:
            logging.error("LLM tool loop step %d failed: %s", step + 1, str(exc)[:200])
            raise
        elapsed = time.perf_counter() - started
        message = result.choices[0].message
        tool_calls = getattr(message, "tool_calls", None) or []
        if not tool_calls:
            content = getattr(message, "content", None)
            logging.info("LLM tool loop step %d/%d: %s (%.1fs)", step + 1, max_steps,
                         "text reply" if content else "empty", elapsed)
            if content:
                return (None, content)
            continue
        call = tool_calls[0]
        name = call.function.name
        args = call.function.arguments or ""
        logging.info("LLM tool loop step %d/%d: model called %s (%.1fs, args=%dch)",
                     step + 1, max_steps, name, elapsed, len(args))
        if name in final_set:
            return (name, args)
        handler = handlers.get(name)
        try:
            tool_result = handler(args) if handler else f"(no handler for tool {name})"
        except Exception as exc:  # pylint: disable=broad-exception-caught
            tool_result = f"(tool {name} failed: {str(exc)[:160]})"
        # Feed the call + its result back so the model can use it on the next step.
        msgs.append({"role": "assistant", "content": message.content or "",
                     "tool_calls": [{"id": call.id, "type": "function",
                                     "function": {"name": name, "arguments": args}}]})
        msgs.append({"role": "tool", "tool_call_id": call.id,
                     "content": str(tool_result)[:6000]})
    return (None, None)


class BaseAgent:  # pylint: disable=too-few-public-methods
    """Base class for agents."""

    def __init__(self, queries_history: list, result_history: list,
                 custom_api_key: str = None, custom_model: str = None):
        """Initialize the agent with query and result history."""
        if result_history is None:
            self.messages = []
        else:
            self.messages = []
            for query, result in zip(queries_history[:-1], result_history):
                self.messages.append({"role": "user", "content": query})
                self.messages.append({"role": "assistant", "content": result})

        self.custom_api_key = custom_api_key
        self.custom_model = custom_model


def parse_response(response: str) -> Dict[str, Any]:
    """
    Parse Claude's response to extract the analysis.
    Handles cases where LLM returns multiple JSON blocks by extracting the last valid one.

    Args:
        response: Claude's response string

    Returns:
        Parsed analysis results
    """
    if not response:
        return {
            "is_sql_translatable": False,
            "confidence": 0,
            "explanation": "LLM returned an empty response",
            "error": str(response),
        }

    try:
        # Try to find all JSON blocks (anything between { and })
        # and parse the last valid one (LLM sometimes corrects itself)
        # Find all potential JSON blocks
        json_blocks = []
        depth = 0
        start_idx = None

        for i, char in enumerate(response):
            if char == '{':
                if depth == 0:
                    start_idx = i
                depth += 1
            elif char == '}':
                depth -= 1
                if depth == 0 and start_idx is not None:
                    json_blocks.append(response[start_idx:i+1])
                    start_idx = None

        # Try to parse JSON blocks from last to first (prefer the corrected version).
        # strict=False so a raw newline/tab inside a string value (the model often
        # pretty-prints the SQL or query_analysis with literal line breaks) does NOT
        # throw away an otherwise-valid, expensive completion and trigger a retry.
        for json_str in reversed(json_blocks):
            try:
                analysis = json.loads(json_str, strict=False)
                # Validate it has required fields
                if "is_sql_translatable" in analysis and "sql_query" in analysis:
                    return analysis
            except json.JSONDecodeError:
                continue

        # Fallback to original method if block parsing fails
        json_start = response.find("{")
        json_end = response.rfind("}") + 1
        json_str = response[json_start:json_end]
        analysis = json.loads(json_str, strict=False)
        return analysis
    except (json.JSONDecodeError, ValueError) as e:
        # Fallback if JSON parsing fails
        return {
            "is_sql_translatable": False,
            "confidence": 0,
            "explanation": f"Failed to parse response: {str(e)}",
            "error": str(response),
        }
