#!/usr/bin/env python3
"""
Anthropic API Proxy for Oracle Code Assist LiteLLM.

Converts Anthropic Messages API requests to Oracle/OpenAI Chat Completions format
and streams responses back in Anthropic SSE format.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import AsyncIterator
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

# Constants
ORACLE_CODE_ASSIST_LITELLM_BASE = "https://code-internal.aiservice.us-chicago-1.oci.oraclecloud.com/20250206/app/litellm"
ORACLE_CODE_ASSIST_LITELLM_ENDPOINT = f"{ORACLE_CODE_ASSIST_LITELLM_BASE}/chat/completions"
MAX_TOKENS_CAP = 16384

# Event types
EVENT_MESSAGE_START = "message_start"
EVENT_MESSAGE_DELTA = "message_delta"
EVENT_MESSAGE_STOP = "message_stop"
EVENT_CONTENT_BLOCK_START = "content_block_start"
EVENT_CONTENT_BLOCK_DELTA = "content_block_delta"
EVENT_CONTENT_BLOCK_STOP = "content_block_stop"
EVENT_PING = "ping"

# Content types
CONTENT_TEXT = "text"
CONTENT_TOOL_USE = "tool_use"
CONTENT_TOOL_RESULT = "tool_result"

# Delta types
DELTA_TEXT = "text_delta"
DELTA_INPUT_JSON = "input_json_delta"

# Stop reasons
STOP_END_TURN = "end_turn"
STOP_MAX_TOKENS = "max_tokens"
STOP_TOOL_USE = "tool_use"
STOP_STOP_SEQUENCE = "stop_sequence"

# Roles
ROLE_ASSISTANT = "assistant"
ROLE_USER = "user"
TOOL_FUNCTION = "function"

# Tool name validation regex
TOOL_NAME_REGEXP = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")

# Global state
api_key: str = ""
api_key_lock = threading.RLock()
small_model: str = "oca/grok-code-fast-1"
big_model: str = "oca/grok-code-fast-1"

# Loggers
debug_logger: logging.Logger
info_logger: logging.Logger
warn_logger: logging.Logger
error_logger: logging.Logger

# HTTP client
http_client: Optional[httpx.AsyncClient] = None

app = FastAPI()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class Token:
    access_token: str = ""
    id_token: str = ""
    expires_at: float = 0.0
    cached_at: float = 0.0
    expires_at_duplicate: float = 0.0
    refresh_expires_at: float = 0.0


@dataclass
class AnthropicUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def to_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
        }


@dataclass
class AnthropicContent:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        result: dict[str, Any] = {"type": self.type}
        if self.type == CONTENT_TEXT:
            result["text"] = self.text
        elif self.type == CONTENT_TOOL_USE:
            result["id"] = self.id
            result["name"] = self.name
            result["input"] = self.input
        return result


@dataclass
class ToolCallState:
    id: Optional[str] = None
    name: Optional[str] = None
    args_buffer: str = ""
    last_sent_args: str = ""
    claude_index: int = 0
    started: bool = False
    has_tools: bool = False


@dataclass
class AggregationState:
    content: list[AnthropicContent] = field(default_factory=list)
    current_tools: dict[int, AnthropicContent] = field(default_factory=dict)
    stop_reason: str = ""
    usage: AnthropicUsage = field(default_factory=AnthropicUsage)
    id: str = ""
    model: str = ""
    role: str = ""
    type: str = ""


# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------

def init_logging(level: str) -> None:
    global debug_logger, info_logger, warn_logger, error_logger

    level_map = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warn": logging.WARNING,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }
    log_level = level_map.get(level.lower(), logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%Y/%m/%d %H:%M:%S",
    )

    # Create separate loggers for each level
    debug_logger = logging.getLogger("debug")
    info_logger = logging.getLogger("info")
    warn_logger = logging.getLogger("warn")
    error_logger = logging.getLogger("error")

    for logger in [debug_logger, info_logger, warn_logger, error_logger]:
        logger.handlers.clear()
        logger.setLevel(logging.DEBUG)

    # Configure handlers based on level
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)

    if log_level <= logging.DEBUG:
        debug_logger.addHandler(stdout_handler)
    if log_level <= logging.INFO:
        info_logger.addHandler(stdout_handler)
    if log_level <= logging.WARNING:
        warn_logger.addHandler(stderr_handler)
    if log_level <= logging.ERROR:
        error_logger.addHandler(stderr_handler)


# ---------------------------------------------------------------------------
# Token Management
# ---------------------------------------------------------------------------

def load_token(token_path: str) -> float:
    """Load token from file and return modification time."""
    global api_key

    with open(token_path, "r") as f:
        data = json.load(f)

    # Support both token.json format (access_token) and secrets.json format (ocaApiKey)
    access_token = data.get("access_token") or data.get("ocaApiKey", "")
    if not access_token:
        raise ValueError("access token not found in token file (expected 'access_token' or 'ocaApiKey')")

    with api_key_lock:
        api_key = access_token

    info_logger.info(f"Successfully loaded new access token from {token_path}")

    return os.path.getmtime(token_path)


def watch_token_file(token_path: str) -> None:
    """Background thread to watch for token file changes."""
    last_mod_time = 0.0

    try:
        last_mod_time = load_token(token_path)
    except Exception as e:
        error_logger.error(f"Initial token load failed: {e}. Will retry.")

    while True:
        time.sleep(10)
        try:
            current_mod_time = os.path.getmtime(token_path)
            if current_mod_time > last_mod_time:
                info_logger.info("Token file change detected. Reloading...")
                last_mod_time = load_token(token_path)
        except Exception as e:
            error_logger.error(f"Failed to reload token file: {e}")


# ---------------------------------------------------------------------------
# Request Conversion
# ---------------------------------------------------------------------------

def parse_tool_result_content(content: Any) -> str:
    """Parse tool result content (handle string or array)."""
    if isinstance(content, str):
        return content
    elif isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                item_type = item.get("type", "")
                if item_type == CONTENT_TEXT:
                    parts.append(item.get("text", ""))
                else:
                    parts.append(str(item))
            else:
                parts.append(str(item))
        return "\n".join(parts).strip()
    else:
        return str(content) if content is not None else ""


def convert_to_oracle_request(req: dict) -> dict:
    """Convert Anthropic request to Oracle/OpenAI Chat Completions format."""
    messages = []

    # Handle system prompt (string or array)
    system_prompt = ""
    system = req.get("system")
    if isinstance(system, str):
        system_prompt = system
    elif isinstance(system, list):
        text_parts = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == CONTENT_TEXT:
                text_parts.append(block.get("text", ""))
        system_prompt = "\n".join(text_parts).strip()

    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    tool_msg_count = 0
    assistant_tool_call_msg_count = 0

    for msg in req.get("messages", []):
        role = msg.get("role", "")
        content = msg.get("content")

        # Simple case: plain string content
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            warn_logger.warning("Skipping invalid message content (non-string, non-array)")
            continue

        if role == ROLE_ASSISTANT:
            text_parts = []
            tool_calls = []

            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type", "")

                if block_type == CONTENT_TEXT:
                    text_parts.append(block.get("text", ""))
                elif block_type == CONTENT_TOOL_USE:
                    name = block.get("name", "")
                    call_id = block.get("id", "")
                    input_data = block.get("input", {})

                    try:
                        args_str = json.dumps(input_data) if isinstance(input_data, dict) else "{}"
                    except (TypeError, ValueError):
                        args_str = "{}"

                    tool_calls.append({
                        "id": call_id,
                        "type": TOOL_FUNCTION,
                        "function": {
                            "name": name,
                            "arguments": args_str,
                        }
                    })
                    debug_logger.debug(
                        f"CONVERT: assistant tool_use -> tool_call id={call_id} name={name} args.len={len(args_str)}"
                    )

            assistant_text = "\n".join(text_parts).strip()
            if assistant_text or tool_calls:
                msg_dict: dict[str, Any] = {"role": ROLE_ASSISTANT}
                # Only include content if there's text or no tool calls
                if assistant_text:
                    msg_dict["content"] = assistant_text
                elif tool_calls:
                    # For assistant messages with tool_calls and no text, omit content
                    pass
                if tool_calls:
                    msg_dict["tool_calls"] = tool_calls
                    assistant_tool_call_msg_count += 1
                messages.append(msg_dict)

        elif role == ROLE_USER:
            user_text_parts = []

            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type", "")

                if block_type == CONTENT_TEXT:
                    user_text_parts.append(block.get("text", ""))
                elif block_type == CONTENT_TOOL_RESULT:
                    tool_use_id = block.get("tool_use_id", "")
                    result = parse_tool_result_content(block.get("content"))
                    messages.append({
                        "role": "tool",
                        "content": result,
                        "tool_call_id": tool_use_id,
                    })
                    tool_msg_count += 1
                    debug_logger.debug(
                        f"CONVERT: user tool_result -> tool message tool_call_id={tool_use_id} result.len={len(result)}"
                    )
                else:
                    user_text_parts.append(f"[content type={block_type} omitted]")

            if user_text_parts:
                messages.append({"role": ROLE_USER, "content": "\n".join(user_text_parts).strip()})

        else:
            # Other roles: stringify text blocks
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == CONTENT_TEXT:
                    text_parts.append(block.get("text", ""))
            if text_parts:
                messages.append({"role": role, "content": "\n".join(text_parts).strip()})

    info_logger.info(
        f"CONVERT SUMMARY: total_messages={len(messages)} "
        f"assistant_tool_call_msgs={assistant_tool_call_msg_count} tool_msgs={tool_msg_count}"
    )

    # Cap max_tokens
    max_tokens = req.get("max_tokens", MAX_TOKENS_CAP)
    if max_tokens > MAX_TOKENS_CAP:
        max_tokens = MAX_TOKENS_CAP

    oracle_req: dict[str, Any] = {
        "model": req.get("model", ""),
        "messages": messages,
        "max_completion_tokens": max_tokens,
        "temperature": req.get("temperature", 0),
        "stream": True,
    }

    if req.get("stop_sequences"):
        oracle_req["stop"] = req["stop_sequences"]
    if req.get("top_p") is not None:
        oracle_req["top_p"] = req["top_p"]
    if req.get("top_k") is not None:
        oracle_req["top_k"] = req["top_k"]

    # Convert tools
    tools = []
    for tool in req.get("tools", []):
        name = tool.get("name", "")
        if not TOOL_NAME_REGEXP.match(name):
            warn_logger.warning(f"Invalid tool name: {name}")
            continue
        tools.append({
            "type": TOOL_FUNCTION,
            "function": {
                "name": name,
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {}),
            }
        })
    if tools:
        oracle_req["tools"] = tools

    # Convert tool_choice
    tool_choice = req.get("tool_choice", {})
    if isinstance(tool_choice, dict) and tool_choice.get("type"):
        choice_type = tool_choice["type"]
        if choice_type == "auto":
            oracle_req["tool_choice"] = "auto"
        elif choice_type == "any":
            oracle_req["tool_choice"] = "required"
            if tool_choice.get("disable_parallel_tool_use"):
                warn_logger.warning("Disable parallel not supported by Oracle")
        elif choice_type == "tool":
            oracle_req["tool_choice"] = {
                "type": TOOL_FUNCTION,
                "function": {"name": tool_choice.get("name", "")}
            }
        elif choice_type == "none":
            oracle_req["tool_choice"] = "none"
        else:
            oracle_req["tool_choice"] = "auto"

    return oracle_req


# ---------------------------------------------------------------------------
# SSE Event Helpers
# ---------------------------------------------------------------------------

def format_sse_event(event_name: str, data: dict) -> str:
    """Format an SSE event."""
    json_data = json.dumps(data, separators=(",", ":"))
    return f"event: {event_name}\ndata: {json_data}\n\n"


# ---------------------------------------------------------------------------
# JSON Recovery
# ---------------------------------------------------------------------------

def attempt_json_recovery(s: str) -> str:
    """Try to extract a valid JSON object by trimming to last balanced braces."""
    balance = 0
    last_valid = -1
    for i, ch in enumerate(s):
        if ch == "{":
            balance += 1
        elif ch == "}":
            if balance > 0:
                balance -= 1
                if balance == 0:
                    last_valid = i
    if last_valid >= 0:
        return s[: last_valid + 1]
    return ""


# ---------------------------------------------------------------------------
# Streaming Handler
# ---------------------------------------------------------------------------

async def handle_streaming(
    request_id: str,
    original_model: str,
    oracle_req: dict,
    current_api_key: str,
) -> tuple[Optional[AsyncIterator], Optional[dict]]:
    """
    Handle streaming response from Oracle and convert to Anthropic SSE format.
    Returns an async generator for streaming, or a dict for error responses.
    """
    global http_client

    body = json.dumps(oracle_req)
    debug_logger.debug(f"[{request_id}] ORACLE REQ JSON: {body}")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {current_api_key}",
    }

    debug_logger.debug(f"[{request_id}] Sending request to Oracle: {ORACLE_CODE_ASSIST_LITELLM_ENDPOINT}")

    try:
        response = await http_client.post(
            ORACLE_CODE_ASSIST_LITELLM_ENDPOINT,
            content=body,
            headers=headers,
            timeout=60.0,
        )
    except Exception as e:
        error_logger.error(f"[{request_id}] ERROR: Failed to send request: {e}")
        return None, {"error": "Streaming error", "status": 500}

    debug_logger.debug(f"[{request_id}] Received response: status={response.status_code}")

    if response.status_code != 200:
        resp_body = await response.aread()
        error_logger.error(f"[{request_id}] ERROR: Oracle error: {response.status_code} {resp_body.decode()}")
        return None, {"error": f"Oracle error: {response.status_code}", "status": response.status_code}

    async def stream_generator():
        info_logger.info(f"[{request_id}] Starting stream")

        message_id = "msg_" + uuid.uuid4().hex[:24]
        text_block_index = -1
        text_block_started = False
        next_index = 0
        current_tool_calls: dict[int, ToolCallState] = {}
        accumulated_usage = AnthropicUsage()

        # message_start
        message_start_event = {
            "type": EVENT_MESSAGE_START,
            "message": {
                "id": message_id,
                "type": "message",
                "role": ROLE_ASSISTANT,
                "model": original_model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": accumulated_usage.to_dict(),
            }
        }
        yield format_sse_event(EVENT_MESSAGE_START, message_start_event)
        debug_logger.debug(f"[{request_id}] Sent SSE: {EVENT_MESSAGE_START}")

        # ping
        ping_event = {"type": EVENT_PING}
        yield format_sse_event(EVENT_PING, ping_event)
        debug_logger.debug(f"[{request_id}] Sent SSE: {EVENT_PING}")

        final_stop_reason = STOP_END_TURN
        has_tool_use = False
        event_counter = 0

        async for line in response.aiter_lines():
            line = line.strip()
            if line:
                debug_logger.debug(f"[{request_id}] Received SSE: {line}")

            if line.startswith("data: "):
                data = line[6:]
                event_counter += 1

                if data == "[DONE]":
                    break

                try:
                    oracle_chunk = json.loads(data)
                except json.JSONDecodeError as e:
                    error_logger.error(f"[{request_id}] ERROR: Parse failed: {e}")
                    continue

                usage_data = oracle_chunk.get("usage")
                if usage_data:
                    accumulated_usage.input_tokens += usage_data.get("input_tokens", 0)
                    accumulated_usage.output_tokens += usage_data.get("output_tokens", 0)

                for choice in oracle_chunk.get("choices", []):
                    delta = choice.get("delta", {})

                    # Text deltas
                    if delta.get("content"):
                        if not text_block_started:
                            text_block_index = next_index
                            next_index += 1
                            cbs = {
                                "type": EVENT_CONTENT_BLOCK_START,
                                "index": text_block_index,
                                "content_block": {
                                    "type": CONTENT_TEXT,
                                    "text": "",
                                }
                            }
                            yield format_sse_event(EVENT_CONTENT_BLOCK_START, cbs)
                            text_block_started = True

                        cbd = {
                            "type": EVENT_CONTENT_BLOCK_DELTA,
                            "index": text_block_index,
                            "delta": {
                                "type": DELTA_TEXT,
                                "text": delta["content"],
                            }
                        }
                        yield format_sse_event(EVENT_CONTENT_BLOCK_DELTA, cbd)

                    # Tool calls
                    for tc_delta in delta.get("tool_calls", []):
                        tc_index = tc_delta.get("index", 0)
                        if tc_index not in current_tool_calls:
                            current_tool_calls[tc_index] = ToolCallState()

                        tool_call = current_tool_calls[tc_index]

                        # Update ID/Name
                        tc_id = tc_delta.get("id", "")
                        if tc_id and tc_id != "null":
                            tool_call.id = tc_id
                            debug_logger.debug(f"[{request_id}] Tool #{tc_index} ID: {tc_id}")

                        func = tc_delta.get("function", {})
                        func_name = func.get("name", "")
                        if func_name and func_name != "null":
                            tool_call.name = func_name
                            debug_logger.debug(f"[{request_id}] Tool #{tc_index} name: {func_name}")

                        # Start when we know the function name
                        if tool_call.name and not tool_call.started:
                            if not tool_call.id:
                                tool_call.id = f"toolu_{message_id}_{tc_index}"
                            tool_call.claude_index = next_index
                            next_index += 1
                            tool_call.started = True
                            has_tool_use = True
                            tool_call.has_tools = True

                            info_logger.info(
                                f"[{request_id}] Starting tool block: {tool_call.name} ({tool_call.id}) at {tool_call.claude_index}"
                            )

                            tool_block_start = {
                                "type": EVENT_CONTENT_BLOCK_START,
                                "index": tool_call.claude_index,
                                "content_block": {
                                    "type": CONTENT_TOOL_USE,
                                    "id": tool_call.id,
                                    "name": tool_call.name,
                                    "input": {},
                                }
                            }
                            yield format_sse_event(EVENT_CONTENT_BLOCK_START, tool_block_start)

                            # Catch up if args were buffered before block started
                            if tool_call.args_buffer:
                                delta_str = tool_call.args_buffer[len(tool_call.last_sent_args):]
                                if delta_str:
                                    cbd = {
                                        "type": EVENT_CONTENT_BLOCK_DELTA,
                                        "index": tool_call.claude_index,
                                        "delta": {
                                            "type": DELTA_INPUT_JSON,
                                            "partial_json": delta_str,
                                        }
                                    }
                                    yield format_sse_event(EVENT_CONTENT_BLOCK_DELTA, cbd)
                                    tool_call.last_sent_args = tool_call.args_buffer

                        # Buffer args
                        args = func.get("arguments", "")
                        if args:
                            tool_call.args_buffer += args
                            info_logger.info(
                                f"[{request_id}] Tool #{tc_index} args bytes buffered: +{len(args)} (total {len(tool_call.args_buffer)})"
                            )
                            if tool_call.started:
                                delta_str = tool_call.args_buffer[len(tool_call.last_sent_args):]
                                if delta_str:
                                    cbd = {
                                        "type": EVENT_CONTENT_BLOCK_DELTA,
                                        "index": tool_call.claude_index,
                                        "delta": {
                                            "type": DELTA_INPUT_JSON,
                                            "partial_json": delta_str,
                                        }
                                    }
                                    yield format_sse_event(EVENT_CONTENT_BLOCK_DELTA, cbd)
                                    tool_call.last_sent_args = tool_call.args_buffer

                    # Finish reason
                    finish_reason = choice.get("finish_reason", "")
                    if finish_reason:
                        if finish_reason == "length":
                            final_stop_reason = STOP_MAX_TOKENS
                        elif finish_reason in ("tool_calls", "function_call"):
                            final_stop_reason = STOP_TOOL_USE
                        elif finish_reason == "stop":
                            final_stop_reason = STOP_END_TURN

        info_logger.info(f"[{request_id}] Finalizing stream after {event_counter} events")

        # Close text block if started
        if text_block_started:
            yield format_sse_event(
                EVENT_CONTENT_BLOCK_STOP,
                {"type": EVENT_CONTENT_BLOCK_STOP, "index": text_block_index}
            )

        # Close tool blocks
        for tool_data in current_tool_calls.values():
            if tool_data.started:
                yield format_sse_event(
                    EVENT_CONTENT_BLOCK_STOP,
                    {"type": EVENT_CONTENT_BLOCK_STOP, "index": tool_data.claude_index}
                )

        # Override stop_reason if any tool block was started
        if has_tool_use:
            final_stop_reason = STOP_TOOL_USE

        # message_delta
        message_delta_event = {
            "type": EVENT_MESSAGE_DELTA,
            "delta": {
                "stop_reason": final_stop_reason,
                "stop_sequence": None,
            },
            "usage": accumulated_usage.to_dict(),
        }
        yield format_sse_event(EVENT_MESSAGE_DELTA, message_delta_event)

        # message_stop
        yield format_sse_event(EVENT_MESSAGE_STOP, {"type": EVENT_MESSAGE_STOP})

    return stream_generator(), None


# ---------------------------------------------------------------------------
# Non-Streaming Aggregation
# ---------------------------------------------------------------------------

async def aggregate_stream(
    request_id: str,
    original_model: str,
    oracle_req: dict,
    current_api_key: str,
) -> dict:
    """Aggregate streaming response into a single Anthropic response."""
    state = AggregationState()
    state.model = original_model
    state.role = ROLE_ASSISTANT
    state.type = "message"
    full_tool_buffers: dict[int, str] = {}

    body = json.dumps(oracle_req)
    debug_logger.debug(f"[{request_id}] ORACLE REQ JSON: {body}")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {current_api_key}",
    }

    try:
        response = await http_client.post(
            ORACLE_CODE_ASSIST_LITELLM_ENDPOINT,
            content=body,
            headers=headers,
            timeout=60.0,
        )
    except Exception as e:
        error_logger.error(f"[{request_id}] ERROR: Failed to send request: {e}")
        raise

    if response.status_code != 200:
        resp_body = await response.aread()
        error_logger.error(f"[{request_id}] ERROR: Oracle error: {response.status_code} {resp_body.decode()}")
        raise Exception(f"Oracle error: {response.status_code}")

    message_id = "msg_" + uuid.uuid4().hex[:24]
    state.id = message_id
    text_content: Optional[AnthropicContent] = None
    current_tools: dict[int, AnthropicContent] = {}

    async for line in response.aiter_lines():
        line = line.strip()
        if not line.startswith("data: "):
            continue

        data = line[6:]
        if data == "[DONE]":
            break

        try:
            oracle_chunk = json.loads(data)
        except json.JSONDecodeError as e:
            error_logger.error(f"[{request_id}] ERROR: Parse failed: {e}")
            continue

        debug_logger.debug(f"[{request_id}] Aggregating SSE: {data}")

        usage_data = oracle_chunk.get("usage")
        if usage_data:
            state.usage.input_tokens += usage_data.get("input_tokens", 0)
            state.usage.output_tokens += usage_data.get("output_tokens", 0)

        for choice in oracle_chunk.get("choices", []):
            delta = choice.get("delta", {})

            # Text content
            if delta.get("content"):
                if text_content is None:
                    text_content = AnthropicContent(type=CONTENT_TEXT, text="")
                text_content.text += delta["content"]

            # Tool calls
            for tc_delta in delta.get("tool_calls", []):
                tc_index = tc_delta.get("index", 0)
                if tc_index not in current_tools:
                    current_tools[tc_index] = AnthropicContent(type=CONTENT_TOOL_USE)
                    full_tool_buffers[tc_index] = ""

                tool = current_tools[tc_index]

                tc_id = tc_delta.get("id", "")
                if tc_id and tc_id != "null":
                    tool.id = tc_id

                func = tc_delta.get("function", {})
                func_name = func.get("name", "")
                if func_name and func_name != "null":
                    tool.name = func_name

                args = func.get("arguments", "")
                if args:
                    full_tool_buffers[tc_index] += args

            # Finish reason
            finish_reason = choice.get("finish_reason", "")
            if finish_reason:
                if finish_reason == "length":
                    state.stop_reason = STOP_MAX_TOKENS
                elif finish_reason in ("tool_calls", "function_call"):
                    state.stop_reason = STOP_TOOL_USE
                elif finish_reason == "stop":
                    state.stop_reason = STOP_END_TURN

    # Build final content
    if text_content and text_content.text:
        state.content.append(text_content)

    for tc_index, tool in current_tools.items():
        if not tool.id:
            tool.id = f"toolu_{message_id}_{tc_index}"
        buf = full_tool_buffers.get(tc_index, "")
        if buf:
            try:
                tool.input = json.loads(buf)
            except json.JSONDecodeError as e:
                warn_logger.warning(f"[{request_id}] WARNING: Tool input parse failed. Error={e} Buffer={buf!r}")
                fixed = attempt_json_recovery(buf)
                if fixed:
                    try:
                        tool.input = json.loads(fixed)
                    except json.JSONDecodeError as e2:
                        warn_logger.warning(f"[{request_id}] WARNING: Tool input parse failed after recovery: {e2}")
                        tool.input = {}
                else:
                    tool.input = {}
        else:
            tool.input = {}
        state.content.append(tool)

    # Set stop_reason based on content
    if state.content and state.content[0].type == CONTENT_TOOL_USE:
        state.stop_reason = STOP_TOOL_USE
    if not state.stop_reason:
        state.stop_reason = STOP_END_TURN

    # Ensure non-empty content
    if not state.content:
        state.content.append(AnthropicContent(type=CONTENT_TEXT, text=""))

    final = {
        "id": state.id,
        "type": state.type,
        "role": state.role,
        "model": state.model,
        "content": [c.to_dict() for c in state.content],
        "stop_reason": state.stop_reason,
        "stop_sequence": None,
        "usage": state.usage.to_dict(),
    }

    debug_logger.debug(f"[{request_id}] AGG FINAL RESPONSE:\n{json.dumps(final, indent=2)}")
    return final


# ---------------------------------------------------------------------------
# HTTP Handlers
# ---------------------------------------------------------------------------

@app.post("/v1/messages")
async def handle_messages(request: Request):
    request_id = uuid.uuid4().hex[:8]

    info_logger.info(f"[{request_id}] Received request from Claude Code: {request.method} {request.url.path}")

    try:
        body_bytes = await request.body()
        req = json.loads(body_bytes)
    except Exception as e:
        error_logger.error(f"[{request_id}] ERROR: Invalid Request Body: {e}")
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    stream = req.get("stream", False)
    original_model = req.get("model", "")

    info_logger.info(
        f"[{request_id}] Processing request for model: {original_model}, stream: {stream}, max_tokens: {req.get('max_tokens', 0)}"
    )

    # Model mapping
    clean_model = original_model.lower().replace("anthropic/", "")
    if "haiku" in clean_model:
        req["model"] = small_model
        info_logger.info(f"[{request_id}] Mapped haiku model to: {small_model}")
    elif "sonnet" in clean_model:
        req["model"] = big_model
        info_logger.info(f"[{request_id}] Mapped sonnet model to: {big_model}")
    else:
        info_logger.info(f"[{request_id}] Using original model: {original_model}")

    oracle_req = convert_to_oracle_request(req)

    with api_key_lock:
        current_api_key = api_key

    if not current_api_key:
        error_logger.error(f"[{request_id}] ERROR: API key not loaded")
        return JSONResponse({"error": "API key not loaded"}, status_code=500)

    # Always stream from upstream
    oracle_req["stream"] = True

    if stream:
        generator, error = await handle_streaming(request_id, original_model, oracle_req, current_api_key)
        if error:
            return JSONResponse({"error": error["error"]}, status_code=error["status"])
        return StreamingResponse(
            generator,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
        )
    else:
        try:
            final_response = await aggregate_stream(request_id, original_model, oracle_req, current_api_key)
            debug_logger.debug(
                f"[{request_id}] Sending non-stream response: id={final_response['id']}, "
                f"content_blocks={len(final_response['content'])}, tokens={final_response['usage']['output_tokens']}"
            )
            return JSONResponse(final_response)
        except Exception as e:
            error_logger.error(f"[{request_id}] ERROR: Aggregation failed: {e}")
            return JSONResponse({"error": "Error aggregating response"}, status_code=500)


@app.get("/health")
async def health():
    return JSONResponse({"status": "healthy"})


@app.get("/")
async def root():
    return JSONResponse({"message": "Anthropic Proxy for OracleCodeAssistLiteLLM"})


# ---------------------------------------------------------------------------
# Application Lifecycle
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup_event():
    global http_client
    http_client = httpx.AsyncClient(
        timeout=60.0,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=100),
    )


@app.on_event("shutdown")
async def shutdown_event():
    global http_client
    if http_client:
        await http_client.aclose()


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

def main():
    global small_model, big_model

    parser = argparse.ArgumentParser(description="Anthropic Proxy for Oracle Code Assist LiteLLM")
    parser.add_argument("--token", default="secrets.json", help="Path to token file (secrets.json or token.json)")
    parser.add_argument("--small-model", default="oca/grok-code-fast-1", help="Small model name")
    parser.add_argument("--big-model", default="oca/grok-code-fast-1", help="Big model name")
    parser.add_argument("--log-level", default="info", help="Set the logging level (debug, info, warn, error)")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on")
    args = parser.parse_args()

    # Configure logging
    init_logging(args.log_level)

    small_model = args.small_model
    big_model = args.big_model

    info_logger.info(f"Starting Oracle LLMProxy server with small-model={small_model}, big-model={big_model}")

    # Initial token load
    try:
        load_token(args.token)
    except Exception as e:
        print(f"FATAL: Could not perform initial load of token file: {e}", file=sys.stderr)
        sys.exit(1)

    # Start token watcher thread
    watcher_thread = threading.Thread(target=watch_token_file, args=(args.token,), daemon=True)
    watcher_thread.start()

    # Run server
    import uvicorn
    info_logger.info(f"Starting server on :{args.port}")
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
