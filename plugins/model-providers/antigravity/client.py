"""OpenAI-compatible client for Google Antigravity backed by ``AGYACPTransport``.

Subclasses ``agent.acp_client.ACPClient`` so all Hermes ACP runtime checks
recognize the client, while replacing the one-shot subprocess-per-turn session
with a persistent JSON-RPC 2.0 ACP transport, real-time incremental streaming,
lookahead ``<tool_call>`` buffering, and optional ``agy -p`` fallback.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Iterator, Sequence

from agent.acp_client import ACPClient
from agent.runtime_cwd import resolve_agent_cwd
from openai.types.chat.chat_completion import ChatCompletion
from openai.types.chat.chat_completion_chunk import (
    ChatCompletionChunk,
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
)
from openai.types.completion_usage import CompletionUsage

from .acp_transport import (
    AGYACPTransport,
    AntigravityACPError,
    AntigravityAuthError,
    TurnAccumulator,
    coerce_timeout_seconds,
)
from .bridge import (
    StreamingToolCallBuffer,
    build_chat_completion,
    estimate_usage,
    extract_tool_calls_from_text,
    format_prompt_for_session,
    make_stream_chunk,
    parse_print_fallback_output,
)
from .process_manager import AGYProcessManager, DEFAULT_ACP_ARGS

logger = logging.getLogger(__name__)

DEFAULT_ANTIGRAVITY_MODEL = "gemini-4-argon-medium"

ANTIGRAVITY_FALLBACK_MODELS: tuple[str, ...] = (
    "gemini-4-argon-medium",
    "gemini-4-argon-high",
    "gemini-4-argon-low",
    "gemini-4-hydrogen-high",
    "gemini-4-hydrogen-xhigh",
    "gemini-4-hydrogen-medium",
    "gemini-4-hydrogen-low",
    "gemini-pro-orchestrator-g1-pro",
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-low",
    "claude-opus-5-5-high",
    "claude-opus-5-5-medium",
    "claude-opus-5-5-low",
    "claude-sonnet-5-5-high",
    "claude-sonnet-5-5-medium",
    "claude-sonnet-5-5-low",
    "gpt-oss-120b-medium",
)


def _resolve_reasoning_effort(
    reasoning_effort: str | None,
    extra_body: dict[str, Any] | None,
) -> str | None:
    """Extract normalized reasoning effort from top-level kwargs or ``extra_body``."""
    if isinstance(reasoning_effort, str) and reasoning_effort.strip():
        return reasoning_effort.strip().lower()
    if not isinstance(extra_body, dict):
        return None
    for key in ("reasoning_effort", "thought_level", "effort"):
        val = extra_body.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().lower()
    reasoning_dict = extra_body.get("reasoning")
    if isinstance(reasoning_dict, dict):
        effort_val = reasoning_dict.get("effort")
        if isinstance(effort_val, str) and effort_val.strip():
            return effort_val.strip().lower()
    return None


def _build_usage(
    raw_usage: dict[str, int],
    prompt_text: str,
    completion_text: str,
    reasoning_text: str,
) -> CompletionUsage:
    """Build ``CompletionUsage`` from ACP usage metadata or fallback character estimate."""
    prompt_tokens = raw_usage.get("prompt_tokens") or raw_usage.get("inputTokens") or 0
    completion_tokens = raw_usage.get("completion_tokens") or raw_usage.get("outputTokens") or 0
    total_tokens = raw_usage.get("total_tokens") or (prompt_tokens + completion_tokens)
    if total_tokens > 0:
        return CompletionUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )
    return estimate_usage(prompt_text, completion_text, reasoning_text)


def _append_assistant_snapshot(
    messages: list[dict[str, Any]],
    content: str,
    tool_calls: list[Any],
) -> list[dict[str, Any]]:
    """Return updated message history snapshot including the assistant's turn."""
    assistant_entry: dict[str, Any] = {"role": "assistant", "content": content or ""}
    if tool_calls:
        assistant_entry["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in tool_calls
        ]
    return [*messages, assistant_entry]


def _finalize_stream_chunks(
    *,
    chunk_id: str,
    created: int,
    model: str,
    prompt_text: str,
    messages: list[dict[str, Any]],
    acc_result: TurnAccumulator,
    lookahead: StreamingToolCallBuffer,
    transport: AGYACPTransport,
    role_sent: bool,
) -> list[ChatCompletionChunk]:
    """Build the final trailing text, tool_call, and finish_reason chunks for a stream."""
    chunks: list[ChatCompletionChunk] = []
    trailing_chunks, tool_calls = lookahead.finalize()
    for trailing in trailing_chunks:
        if trailing:
            chunks.append(
                make_stream_chunk(
                    chunk_id=chunk_id,
                    created=created,
                    model=model,
                    content=trailing,
                    include_role=not role_sent,
                )
            )
            role_sent = True

    if tool_calls:
        delta_calls = [
            ChoiceDeltaToolCall(
                index=idx,
                id=tc.id,
                type="function",
                function=ChoiceDeltaToolCallFunction(
                    name=tc.function.name,
                    arguments=tc.function.arguments,
                ),
            )
            for idx, tc in enumerate(tool_calls)
        ]
        chunks.append(
            make_stream_chunk(
                chunk_id=chunk_id,
                created=created,
                model=model,
                tool_calls=delta_calls,
                include_role=not role_sent,
            )
        )
        role_sent = True

    if not role_sent:
        chunks.append(
            make_stream_chunk(
                chunk_id=chunk_id,
                created=created,
                model=model,
                content="",
                include_role=True,
            )
        )

    final_text = lookahead.emitted_text
    transport.set_session_messages_snapshot(
        _append_assistant_snapshot(messages, final_text, tool_calls)
    )
    usage = _build_usage(
        acc_result.usage,
        prompt_text,
        final_text,
        acc_result.full_thought,
    )
    finish_reason = "tool_calls" if tool_calls else acc_result.stop_reason
    chunks.append(
        make_stream_chunk(
            chunk_id=chunk_id,
            created=created,
            model=model,
            finish_reason=finish_reason,
            usage=usage,
        )
    )
    return chunks


def _request_budget_seconds(timeout: Any) -> float | None:
    """Wall-clock seconds for one ACP turn from an OpenAI-SDK ``timeout`` argument.

    Hermes passes ``httpx.Timeout`` on the streaming path (the SDK contract is
    ``float | httpx.Timeout | None``). ACP waits on the whole ``session/prompt`` RPC, not
    per socket read, so the largest component is the budget (same rule as core
    ``ACPClient``). ``None`` / no numeric component keeps the transport default.
    """
    if timeout is None:
        return None
    val = coerce_timeout_seconds(timeout, default=-1.0)
    return val if val > 0 else None


class _AntigravityCompletions:
    """Implements ``client.chat.completions.create(...)``."""

    def __init__(self, client: AntigravityClient) -> None:
        self._client = client

    def create(
        self,
        *,
        model: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        stream: bool = False,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        reasoning_effort: str | None = None,
        extra_body: dict[str, Any] | None = None,
        timeout: Any = None,
        **_kwargs: Any,
    ) -> ChatCompletion | Iterator[ChatCompletionChunk]:
        resolved_model = (model or self._client.default_model or DEFAULT_ANTIGRAVITY_MODEL).strip()
        resolved_messages = list(messages or [])
        effort = _resolve_reasoning_effort(reasoning_effort, extra_body)
        timeout = _request_budget_seconds(timeout)

        if stream:
            return self._client.stream_completion(
                model=resolved_model,
                messages=resolved_messages,
                tools=tools,
                tool_choice=tool_choice,
                reasoning_effort=effort,
                timeout=timeout,
            )
        return self._client.create_completion(
            model=resolved_model,
            messages=resolved_messages,
            tools=tools,
            tool_choice=tool_choice,
            reasoning_effort=effort,
            timeout=timeout,
        )


class _AntigravityChat:
    """Namespace wrapper exposing ``client.chat.completions``."""

    def __init__(self, client: AntigravityClient) -> None:
        self.completions = _AntigravityCompletions(client)


class AntigravityClient(ACPClient):
    """OpenAI-compatible client backed by persistent ``agy acp --stdio`` transport."""

    HERMES_SKIP_TRANSPORT_WRAP = True
    HERMES_SKIP_ASYNC_WRAP = True
    auth_required_hint = "Run 'agy login' to authenticate with Google OAuth."

    def __init__(
        self,
        *,
        command: str | None = None,
        args: Sequence[str] | None = None,
        acp_cwd: str | None = None,
        default_model: str = DEFAULT_ANTIGRAVITY_MODEL,
        enable_print_fallback: bool = True,
        request_timeout: float = 300.0,
        handshake_timeout: float = 30.0,
        env: dict[str, str] | None = None,
        **_kwargs: Any,
    ) -> None:
        resolved_cwd = str(acp_cwd or resolve_agent_cwd())
        resolved_args = list(args) if args is not None else list(DEFAULT_ACP_ARGS)
        super().__init__(
            command=command or "agy",
            args=resolved_args,
            acp_cwd=resolved_cwd,
        )
        self.default_model = default_model or DEFAULT_ANTIGRAVITY_MODEL
        self.enable_print_fallback = enable_print_fallback
        self._pm = AGYProcessManager(
            command=command,
            args=resolved_args,
            cwd=resolved_cwd,
            env=env,
        )
        self._transport = AGYACPTransport(
            self._pm,
            cwd=resolved_cwd,
            handshake_timeout=handshake_timeout,
            request_timeout=request_timeout,
        )
        self.chat = _AntigravityChat(self)

    @property
    def process_manager(self) -> AGYProcessManager:
        return self._pm

    @property
    def transport(self) -> AGYACPTransport:
        return self._transport

    def create_completion(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        reasoning_effort: str | None = None,
        timeout: Any = None,
    ) -> ChatCompletion:
        """Execute a synchronous chat completion via ACP (with optional print-mode fallback)."""
        resolved_timeout = _request_budget_seconds(timeout)
        parse_tools = bool(tools and tool_choice != "none")
        prompt_text, can_reuse = format_prompt_for_session(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            previous_messages=self._transport.session_messages_snapshot,
        )

        try:
            acc = self._transport.prompt(
                prompt_text,
                model=model,
                reasoning_effort=reasoning_effort,
                reuse_session=can_reuse,
                timeout=resolved_timeout,
            )
            raw_text = acc.full_text
            reasoning_text = acc.full_thought
            raw_usage = acc.usage
            stop_reason = acc.stop_reason
        except AntigravityAuthError:
            raise
        except (AntigravityACPError, FileNotFoundError, OSError, RuntimeError) as exc:
            if not self.enable_print_fallback or isinstance(exc, FileNotFoundError):
                raise
            logger.warning("AGY ACP prompt failed (%s); falling back to 'agy -p' print mode.", exc)
            full_prompt, _ = format_prompt_for_session(
                messages,
                tools=tools,
                tool_choice=tool_choice,
                previous_messages=None,
            )
            fallback_res = self._pm.run_print_prompt(
                full_prompt,
                model=model,
                reasoning_effort=reasoning_effort,
                timeout=resolved_timeout or 180.0,
            )
            raw_text = parse_print_fallback_output(str(fallback_res.get("stdout") or ""))
            reasoning_text = ""
            raw_usage = {}
            stop_reason = "stop"

        if parse_tools:
            cleaned_text, tool_calls = extract_tool_calls_from_text(raw_text)
        else:
            cleaned_text, tool_calls = raw_text, []

        self._transport.set_session_messages_snapshot(
            _append_assistant_snapshot(messages, cleaned_text, tool_calls)
        )
        usage = _build_usage(raw_usage, prompt_text, cleaned_text, reasoning_text)
        finish_reason = "tool_calls" if tool_calls else stop_reason
        return build_chat_completion(
            model=model,
            content=cleaned_text,
            reasoning_content=reasoning_text,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=finish_reason,
        )

    def stream_completion(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        reasoning_effort: str | None = None,
        timeout: Any = None,
    ) -> Iterator[ChatCompletionChunk]:
        """Stream incremental ``ChatCompletionChunk`` deltas in real time from ACP notifications."""
        resolved_timeout = _request_budget_seconds(timeout)
        parse_tools = bool(tools and tool_choice != "none")
        prompt_text, can_reuse = format_prompt_for_session(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            previous_messages=self._transport.session_messages_snapshot,
        )

        event_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        lookahead = StreamingToolCallBuffer(parse_tool_calls=parse_tools)
        chunk_id = f"chatcmpl-agy-{int(time.time() * 1000)}"
        created = int(time.time())

        def _on_text(delta: str) -> None:
            for safe_chunk in lookahead.feed(delta):
                if safe_chunk:
                    event_queue.put(("content", safe_chunk))

        def _on_thought(delta: str) -> None:
            if delta:
                event_queue.put(("thought", delta))

        def _worker() -> None:
            try:
                acc = self._transport.prompt(
                    prompt_text,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    reuse_session=can_reuse,
                    on_text=_on_text,
                    on_thought=_on_thought,
                    timeout=resolved_timeout,
                )
                event_queue.put(("done", acc))
            except BaseException as exc:  # health: allow BLE001 -- forward worker exception to consumer generator
                event_queue.put(("error", exc))

        worker_thread = threading.Thread(  # health: allow HX012 -- turn stream producer; pushes events to local queue only
            target=_worker,
            name="agy-acp-stream-turn",
            daemon=True,
        )
        worker_thread.start()

        role_sent = False
        completed = False
        try:
            while True:
                kind, payload = event_queue.get()
                if kind in ("content", "thought"):
                    yield make_stream_chunk(
                        chunk_id=chunk_id,
                        created=created,
                        model=model,
                        content=str(payload) if kind == "content" else None,
                        reasoning_content=str(payload) if kind == "thought" else None,
                        include_role=not role_sent,
                    )
                    role_sent = True
                    continue

                completed = True
                if kind == "error":
                    if isinstance(payload, BaseException):
                        raise payload
                    raise AntigravityACPError(str(payload))

                yield from _finalize_stream_chunks(
                    chunk_id=chunk_id,
                    created=created,
                    model=model,
                    prompt_text=prompt_text,
                    messages=messages,
                    acc_result=payload,
                    lookahead=lookahead,
                    transport=self._transport,
                    role_sent=role_sent,
                )
                break
        finally:
            if not completed:
                self.cancel()

    def list_models(self, *, timeout_seconds: float = 15.0) -> list[str]:
        """Return available Antigravity model IDs via ``agy models``, ACP session, or fallback."""
        try:
            live = self._pm.fetch_live_models(timeout=timeout_seconds)
            if live:
                return live
        except (OSError, RuntimeError) as exc:
            logger.debug("agy models CLI lookup failed, checking ACP session models: %s", exc)

        if self._transport.available_models:
            return self._transport.available_models

        return list(ANTIGRAVITY_FALLBACK_MODELS)

    def cancel(self) -> None:
        """Cancel any active ACP prompt turn."""
        self._transport.cancel()

    def close(self) -> None:
        """Cancel active requests and stop the persistent AGY subprocess."""
        self._transport.close()
