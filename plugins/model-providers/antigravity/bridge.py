"""OpenAI-compatible request/response normalization and tool-calling bridge for Antigravity.

Translates OpenAI ``messages`` / ``tools`` / ``tool_choice`` into ACP prompt turns,
streams incremental text & reasoning chunks while preventing ``<tool_call>`` XML
leakage via lookahead buffering, and normalizes responses into OpenAI SDK types.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from openai.types.chat.chat_completion import ChatCompletion, Choice
from openai.types.chat.chat_completion_chunk import (
    ChatCompletionChunk,
    Choice as ChunkChoice,
    ChoiceDelta,
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
)
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from openai.types.chat.chat_completion_message_tool_call import (
    ChatCompletionMessageToolCall,
    Function,
)
from openai.types.completion_usage import CompletionUsage

TOOL_CALL_OPEN_TAG = "<tool_call>"
TOOL_CALL_CLOSE_TAG = "</tool_call>"
TOOL_CALL_BLOCK_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)

TOOL_CALL_CONTRACT = (
    "When you need to call a tool from the Available Tools list below, DO NOT invoke "
    "any internal IDE/CLI host tools. Instead, emit one or more XML blocks in this exact format:\n"
    '<tool_call>{"name": "tool_name", "arguments": {"arg": "value"}}</tool_call>\n'
    "Only call tools listed in Available Tools. After emitting <tool_call> blocks, wait for "
    "the tool result in the next turn."
)


def extract_text_content(content: Any) -> str:
    """Flatten OpenAI string or content-part list into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return "" if content is None else str(content)


def build_openai_tool_call(
    index: int,
    name: str,
    arguments: Any,
    call_id: str | None = None,
) -> ChatCompletionMessageToolCall:
    """Create a normalized OpenAI ``ChatCompletionMessageToolCall``."""
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
            arguments_json = json.dumps(parsed, ensure_ascii=False)
        except json.JSONDecodeError:
            arguments_json = json.dumps({"raw": arguments}, ensure_ascii=False)
    elif arguments is None:
        arguments_json = "{}"
    else:
        arguments_json = json.dumps(arguments, ensure_ascii=False)

    resolved_id = (call_id or "").strip() or f"call_{index}"
    return ChatCompletionMessageToolCall(
        id=resolved_id,
        type="function",
        function=Function(name=str(name).strip(), arguments=arguments_json),
    )


def _parse_tool_payload(raw_json: str, index: int) -> ChatCompletionMessageToolCall | None:
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    fn_obj = payload.get("function")
    if isinstance(fn_obj, dict):
        name = str(fn_obj.get("name") or "").strip()
        args = fn_obj.get("arguments", {})
    else:
        name = str(payload.get("name") or "").strip()
        args = payload.get("arguments", {})
    if not name:
        return None
    call_id = payload.get("id")
    return build_openai_tool_call(
        index,
        name,
        args,
        str(call_id) if isinstance(call_id, str) else None,
    )


def extract_tool_calls_from_text(text: str) -> tuple[str, list[ChatCompletionMessageToolCall]]:
    """Extract ``<tool_call>{...}</tool_call>`` blocks (or bare JSON tool payload) from text."""
    if not text:
        return "", []

    tool_calls: list[ChatCompletionMessageToolCall] = []
    matches = list(TOOL_CALL_BLOCK_RE.finditer(text))
    if matches:
        cleaned = text
        for match in matches:
            tc = _parse_tool_payload(match.group(1), len(tool_calls) + 1)
            if tc is not None:
                tool_calls.append(tc)
                cleaned = cleaned.replace(match.group(0), "")
        if tool_calls:
            return cleaned.strip(), tool_calls

    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        tc = _parse_tool_payload(stripped, 1)
        if tc is not None:
            return "", [tc]

    return text, []


def render_tool_bridge_sections(
    tools: list[dict[str, Any]] | None,
    tool_choice: Any = None,
) -> list[str]:
    """Render tool definitions and ``<tool_call>`` contract for the prompt."""
    if not tools or tool_choice == "none":
        return []

    rendered_tools: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function", tool)
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        rendered_tools.append(
            {
                "name": name,
                "description": str(fn.get("description") or "").strip(),
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )

    if not rendered_tools:
        return []

    sections = [
        f"[Tool Calling Contract]\n{TOOL_CALL_CONTRACT}",
        "[Available Tools]\n" + json.dumps(rendered_tools, ensure_ascii=False, indent=2),
    ]
    if isinstance(tool_choice, str) and tool_choice not in ("auto", "none"):
        sections.append(f"[Tool Choice]\n{tool_choice}")
    elif isinstance(tool_choice, dict):
        sections.append("[Tool Choice]\n" + json.dumps(tool_choice, ensure_ascii=False))
    return sections


def render_single_message(message: dict[str, Any]) -> str:
    """Format one OpenAI message dict into a transcript block."""
    role = str(message.get("role") or "user").strip().lower()
    content = extract_text_content(message.get("content")).strip()
    blocks: list[str] = []

    if role == "system":
        if content:
            blocks.append(f"[System]\n{content}")
    elif role == "assistant":
        if content:
            blocks.append(f"[Assistant]\n{content}")
        for tc in message.get("tool_calls") or ():
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            tc_payload = {
                "id": tc.get("id") or "",
                "name": fn.get("name") or "",
                "arguments": fn.get("arguments") or "{}",
            }
            blocks.append(
                f"[Assistant Tool Call]\n{TOOL_CALL_OPEN_TAG}"
                f"{json.dumps(tc_payload, ensure_ascii=False)}"
                f"{TOOL_CALL_CLOSE_TAG}"
            )
    elif role == "tool":
        call_id = str(message.get("tool_call_id") or "").strip()
        header = f"[Tool Result {call_id}]" if call_id else "[Tool Result]"
        blocks.append(f"{header}\n{content}")
    else:
        label = role.capitalize() if role else "User"
        if content:
            blocks.append(f"[{label}]\n{content}")

    return "\n\n".join(blocks)


def compute_messages_digest(messages: list[dict[str, Any]]) -> str:
    """Compute a stable SHA-256 digest over a list of OpenAI message dicts."""
    canonical = json.dumps(messages, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def format_prompt_for_session(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    previous_messages: list[dict[str, Any]] | None = None,
) -> tuple[str, bool]:
    """Build the prompt text for ACP ``session/prompt``.

    Returns ``(prompt_text, can_reuse_session)``.
    If ``previous_messages`` is a strict prefix of ``messages`` (and non-empty),
    only the newly appended messages since ``previous_messages`` are formatted
    and ``can_reuse_session`` is True. Otherwise, the full conversation is
    formatted and ``can_reuse_session`` is False (triggering a fresh session).
    """
    if not messages:
        return "", False

    can_reuse = False
    if previous_messages and len(messages) > len(previous_messages):
        prefix_len = len(previous_messages)
        if compute_messages_digest(messages[:prefix_len]) == compute_messages_digest(previous_messages):
            can_reuse = True

    if can_reuse and previous_messages is not None:
        delta_messages = messages[len(previous_messages) :]
        rendered = [render_single_message(m) for m in delta_messages]
        sections = [s for s in rendered if s]
        if tools and tool_choice != "none":
            sections.insert(0, f"[Tool Calling Reminder]\n{TOOL_CALL_CONTRACT}")
        return "\n\n".join(sections).strip(), True

    sections: list[str] = []
    system_msgs = [m for m in messages if str(m.get("role") or "").lower() == "system"]
    non_system_msgs = [m for m in messages if str(m.get("role") or "").lower() != "system"]

    for sys_msg in system_msgs:
        rendered_sys = render_single_message(sys_msg)
        if rendered_sys:
            sections.append(rendered_sys)

    sections.extend(render_tool_bridge_sections(tools, tool_choice))

    for msg in non_system_msgs:
        rendered_msg = render_single_message(msg)
        if rendered_msg:
            sections.append(rendered_msg)

    return "\n\n".join(sections).strip(), False


def estimate_usage(prompt_text: str, completion_text: str, reasoning_text: str = "") -> CompletionUsage:
    """Estimate token usage when ACP notifications omit explicit usage counters."""
    prompt_tokens = max(1, len(prompt_text) // 4) if prompt_text else 0
    comp_chars = len(completion_text) + len(reasoning_text)
    completion_tokens = max(1, comp_chars // 4) if comp_chars else 0
    return CompletionUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )


def build_chat_completion(
    *,
    model: str,
    content: str,
    reasoning_content: str = "",
    tool_calls: list[ChatCompletionMessageToolCall] | None = None,
    usage: CompletionUsage | None = None,
    finish_reason: str | None = None,
    completion_id: str | None = None,
) -> ChatCompletion:
    """Construct an OpenAI-compatible ``ChatCompletion`` response object."""
    resolved_tools = list(tool_calls) if tool_calls else []
    resolved_finish = finish_reason or ("tool_calls" if resolved_tools else "stop")
    if resolved_finish not in ("stop", "length", "tool_calls", "content_filter", "function_call"):
        resolved_finish = "tool_calls" if resolved_tools else "stop"

    message_kwargs: dict[str, Any] = {
        "role": "assistant",
        "content": content if (content or not resolved_tools) else None,
        "tool_calls": resolved_tools or None,
    }
    if reasoning_content:
        message_kwargs["reasoning_content"] = reasoning_content

    message = ChatCompletionMessage(**message_kwargs)
    return ChatCompletion(
        id=completion_id or f"chatcmpl-agy-{int(time.time() * 1000)}",
        object="chat.completion",
        created=int(time.time()),
        model=model,
        choices=[
            Choice(
                index=0,
                message=message,
                finish_reason=resolved_finish,  # type: ignore[arg-type]
            )
        ],
        usage=usage or CompletionUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
    )


def make_stream_chunk(
    *,
    chunk_id: str,
    created: int,
    model: str,
    content: str | None = None,
    reasoning_content: str | None = None,
    tool_calls: list[ChoiceDeltaToolCall] | None = None,
    finish_reason: str | None = None,
    usage: CompletionUsage | None = None,
    include_role: bool = False,
) -> ChatCompletionChunk:
    """Construct an OpenAI-compatible ``ChatCompletionChunk``."""
    delta_kwargs: dict[str, Any] = {}
    if include_role:
        delta_kwargs["role"] = "assistant"
    if content is not None:
        delta_kwargs["content"] = content
    if reasoning_content is not None:
        delta_kwargs["reasoning_content"] = reasoning_content
    if tool_calls is not None:
        delta_kwargs["tool_calls"] = tool_calls

    resolved_finish = finish_reason
    if resolved_finish is not None and resolved_finish not in (
        "stop",
        "length",
        "tool_calls",
        "content_filter",
        "function_call",
    ):
        resolved_finish = "tool_calls" if tool_calls else "stop"

    delta = ChoiceDelta(**delta_kwargs)
    return ChatCompletionChunk(
        id=chunk_id,
        object="chat.completion.chunk",
        created=created,
        model=model,
        choices=[
            ChunkChoice(
                index=0,
                delta=delta,
                finish_reason=resolved_finish,  # type: ignore[arg-type]
            )
        ],
        usage=usage,
    )


class StreamingToolCallBuffer:
    """Lookahead stream parser that streams normal prose immediately while holding back ``<tool_call>`` blocks.

    Guarantees that ``<tool_call>...</tool_call>`` XML never leaks into ``delta.content``
    chunks seen by the user, while regular text streams without waiting for turn completion.
    """

    def __init__(self, *, parse_tool_calls: bool = True) -> None:
        self._parse_tool_calls = parse_tool_calls
        self._pending = ""
        self._in_tool_block = False
        self._tool_block_buf = ""
        self._raw_tool_blocks: list[str] = []
        self._emitted_text_parts: list[str] = []

    @property
    def emitted_text(self) -> str:
        return "".join(self._emitted_text_parts)

    def feed(self, text_delta: str) -> list[str]:
        """Feed a new text delta and return safe prose chunks ready to emit immediately."""
        if not text_delta:
            return []
        if not self._parse_tool_calls:
            self._emitted_text_parts.append(text_delta)
            return [text_delta]

        self._pending += text_delta
        ready: list[str] = []

        while self._pending:
            if self._in_tool_block:
                close_idx = self._pending.find(TOOL_CALL_CLOSE_TAG)
                if close_idx == -1:
                    self._tool_block_buf += self._pending
                    self._pending = ""
                    break
                end_pos = close_idx + len(TOOL_CALL_CLOSE_TAG)
                self._tool_block_buf += self._pending[:end_pos]
                self._raw_tool_blocks.append(self._tool_block_buf)
                self._tool_block_buf = ""
                self._in_tool_block = False
                self._pending = self._pending[end_pos:]
                continue

            open_idx = self._pending.find(TOOL_CALL_OPEN_TAG)
            if open_idx != -1:
                if open_idx > 0:
                    safe = self._pending[:open_idx]
                    ready.append(safe)
                    self._emitted_text_parts.append(safe)
                self._in_tool_block = True
                self._tool_block_buf = TOOL_CALL_OPEN_TAG
                self._pending = self._pending[open_idx + len(TOOL_CALL_OPEN_TAG) :]
                continue

            # Check if the tail of _pending could be the start of "<tool_call>"
            hold_len = _matching_tag_prefix_suffix(self._pending, TOOL_CALL_OPEN_TAG)
            if hold_len > 0:
                safe_len = len(self._pending) - hold_len
                if safe_len > 0:
                    safe = self._pending[:safe_len]
                    ready.append(safe)
                    self._emitted_text_parts.append(safe)
                    self._pending = self._pending[safe_len:]
                break

            ready.append(self._pending)
            self._emitted_text_parts.append(self._pending)
            self._pending = ""

        return ready

    def finalize(self) -> tuple[list[str], list[ChatCompletionMessageToolCall]]:
        """Flush any remaining buffered text and extract all parsed tool calls."""
        trailing_text: list[str] = []
        if self._in_tool_block and self._tool_block_buf:
            # Unclosed <tool_call> tag: attempt JSON recovery or flush as text
            inner = self._tool_block_buf[len(TOOL_CALL_OPEN_TAG) :].strip()
            if inner.startswith("{") and inner.endswith("}"):
                self._raw_tool_blocks.append(f"{TOOL_CALL_OPEN_TAG}{inner}{TOOL_CALL_CLOSE_TAG}")
            else:
                trailing_text.append(self._tool_block_buf)
                self._emitted_text_parts.append(self._tool_block_buf)
            self._tool_block_buf = ""
            self._in_tool_block = False

        if self._pending:
            trailing_text.append(self._pending)
            self._emitted_text_parts.append(self._pending)
            self._pending = ""

        combined_blocks = "\n".join(self._raw_tool_blocks)
        _, tool_calls = extract_tool_calls_from_text(combined_blocks)
        return trailing_text, tool_calls


def _matching_tag_prefix_suffix(text: str, tag: str) -> int:
    """Return length of the longest suffix of ``text`` that is a prefix of ``tag``."""
    max_check = min(len(text), len(tag) - 1)
    for size in range(max_check, 0, -1):
        if text.endswith(tag[:size]):
            return size
    return 0


def _extract_response_from_json_text(candidate: str) -> str | None:
    """Parse a JSON object string and return its primary text field, or None."""
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    for key in ("response", "result", "content", "text", "output"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return None


def parse_print_fallback_output(raw_stdout: str) -> str:
    """Extract assistant response text from ``agy -p --output-format json`` stdout."""
    text = (raw_stdout or "").strip()
    if not text:
        return ""
    direct = _extract_response_from_json_text(text)
    if direct is not None:
        return direct

    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            extracted = _extract_response_from_json_text(candidate)
            if extracted is not None:
                return extracted
    return text
