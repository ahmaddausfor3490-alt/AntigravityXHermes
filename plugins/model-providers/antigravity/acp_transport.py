"""Persistent ACP JSON-RPC 2.0 stdio transport for Google Antigravity (``agy acp --stdio``).

Implements JSON-RPC 2.0 framing, request/response correlation, streaming
``session/update`` notification dispatch, strict denial of AGY-internal tool
permission requests (``session/request_permission``), session lifecycle
management, cancellation, and automatic process recovery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
import queue
import threading
from typing import Any, Callable

from agent.redact import redact_sensitive_text

from .process_manager import AGYProcessManager

logger = logging.getLogger(__name__)

ACP_PROTOCOL_VERSION = 1

REASONING_EFFORT_TO_THOUGHT_LEVEL: dict[str, str] = {
    "none": "low",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "max",
    "max": "max",
}


class AntigravityACPError(RuntimeError):
    """Raised when the AGY ACP server returns a JSON-RPC or transport error."""

    def __init__(self, message: str, *, code: int | None = None, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class AntigravityAuthError(AntigravityACPError):
    """Raised when AGY reports missing or expired Google OAuth authentication."""


@dataclass
class TurnAccumulator:
    """Collects streaming text, reasoning deltas, and usage for an active prompt turn."""

    text_chunks: list[str] = field(default_factory=list)
    thought_chunks: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "stop"
    on_text: Callable[[str], None] | None = None
    on_thought: Callable[[str], None] | None = None

    @property
    def full_text(self) -> str:
        return "".join(self.text_chunks)

    @property
    def full_thought(self) -> str:
        return "".join(self.thought_chunks)


def _extract_chunk_text(update: dict[str, Any]) -> str:
    """Extract string text from an ACP ``session/update`` payload."""
    content = update.get("content")
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
    if isinstance(content, list):
        parts = [
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        return "".join(parts)
    direct = update.get("text")
    return direct if isinstance(direct, str) else ""


def _handle_message_chunk(acc: TurnAccumulator, update: dict[str, Any]) -> None:
    text = _extract_chunk_text(update)
    if not text:
        return
    acc.text_chunks.append(text)
    if acc.on_text is not None:
        acc.on_text(text)


def _handle_thought_chunk(acc: TurnAccumulator, update: dict[str, Any]) -> None:
    text = _extract_chunk_text(update)
    if not text:
        return
    acc.thought_chunks.append(text)
    if acc.on_thought is not None:
        acc.on_thought(text)


def _handle_usage_update(acc: TurnAccumulator, update: dict[str, Any]) -> None:
    usage = update.get("usage")
    if not isinstance(usage, dict):
        return
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "inputTokens", "outputTokens"):
        val = usage.get(key)
        if isinstance(val, int):
            acc.usage[key] = val


_UPDATE_DISPATCH: dict[str, Callable[[TurnAccumulator, dict[str, Any]], None]] = {
    "agent_message_chunk": _handle_message_chunk,
    "agent_thought_chunk": _handle_thought_chunk,
    "usage_update": _handle_usage_update,
    "usage": _handle_usage_update,
}


def _is_auth_failure(code: int | None, message: str, data: Any) -> bool:
    """Determine if a JSON-RPC error indicates missing/expired AGY authentication."""
    if code in (-32000, -32001, 401, 403):
        return True
    combined = f"{message} {data or ''}".lower()
    auth_markers = (
        "auth required",
        "authentication required",
        "not authenticated",
        "unauthenticated",
        "unauthorized",
        "not logged in",
        "please login",
        "please sign in",
        "agy login",
        "oauth",
        "token expired",
    )
    return any(marker in combined for marker in auth_markers)


def coerce_timeout_seconds(timeout: Any, default: float = 300.0) -> float:
    """Normalise a timeout value (float, int, httpx.Timeout, or None) to positive float seconds.

    Hermes passes ``httpx.Timeout`` on the streaming path (OpenAI SDK contract is
    ``float | httpx.Timeout | None``). ACP stdio waits on the whole RPC response, so the
    largest positive component is used. If timeout is None or cannot be resolved to a
    positive number, returns ``default``.
    """
    if timeout is None:
        return default
    if isinstance(timeout, (int, float)):
        val = float(timeout)
        return val if val > 0 else default
    candidates = [
        getattr(timeout, attr, None)
        for attr in ("read", "write", "connect", "pool", "timeout")
    ]
    numeric = [
        float(v)
        for v in candidates
        if isinstance(v, (int, float)) and float(v) > 0
    ]
    return max(numeric) if numeric else default


class AGYACPTransport:
    """Persistent JSON-RPC 2.0 client over ``agy acp --stdio``."""

    def __init__(
        self,
        process_manager: AGYProcessManager,
        *,
        cwd: str | None = None,
        handshake_timeout: float = 30.0,
        request_timeout: float = 300.0,
    ) -> None:
        self._pm = process_manager
        self._cwd = str(cwd or os.getcwd())
        self._handshake_timeout = handshake_timeout
        self._request_timeout = request_timeout

        self._io_lock = threading.RLock()
        self._init_lock = threading.Lock()
        self._turn_lock = threading.Lock()
        self._next_request_id = 0
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._reader_thread: threading.Thread | None = None
        self._reader_proc_id: int | None = None

        self._initialized = False
        self._agent_capabilities: dict[str, Any] = {}
        self._active_session_id: str | None = None
        self._active_model: str | None = None
        self._active_thought_level: str | None = None
        self._available_models: list[str] = []
        self._session_messages_snapshot: list[dict[str, Any]] | None = None
        self._active_turn: TurnAccumulator | None = None
        self._permission_denials = 0

    @property
    def process_manager(self) -> AGYProcessManager:
        return self._pm

    @property
    def active_session_id(self) -> str | None:
        return self._active_session_id

    @property
    def available_models(self) -> list[str]:
        return list(self._available_models)

    @property
    def permission_denials(self) -> int:
        return self._permission_denials

    @property
    def session_messages_snapshot(self) -> list[dict[str, Any]] | None:
        return self._session_messages_snapshot

    def set_session_messages_snapshot(self, messages: list[dict[str, Any]] | None) -> None:
        self._session_messages_snapshot = list(messages) if messages is not None else None

    def ensure_initialized(self) -> dict[str, Any]:
        """Start the AGY process if needed and perform the ACP ``initialize`` handshake."""
        with self._init_lock:
            with self._io_lock:
                if self._pm.is_running() and self._initialized:
                    return self._agent_capabilities
                self._reset_session_state_locked()
                proc = self._pm.start()
                self._ensure_reader_thread_locked(proc)

            result = self._request(
                "initialize",
                {
                    "protocolVersion": ACP_PROTOCOL_VERSION,
                    "clientInfo": {"name": "apa-hermes", "version": "1.0.0"},
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    },
                },
                timeout=self._handshake_timeout,
            )
            self._notify("initialized", {})
            with self._io_lock:
                self._initialized = True
                self._agent_capabilities = result if isinstance(result, dict) else {}
                return self._agent_capabilities

    def create_session(self, *, cwd: str | None = None) -> str:
        """Create a new ACP session via ``session/new`` and cache available models."""
        self.ensure_initialized()
        result = self._request(
            "session/new",
            {
                "cwd": str(cwd or self._cwd),
                "mcpServers": [],
            },
            timeout=self._handshake_timeout,
        )
        session_id = str((result or {}).get("sessionId") or "").strip()
        if not session_id:
            raise AntigravityACPError("AGY ACP session/new response did not include a sessionId.")

        self._active_session_id = session_id
        self._active_model = None
        self._active_thought_level = None
        self._session_messages_snapshot = None
        self._record_session_models(result if isinstance(result, dict) else {})
        return session_id

    def load_session(self, session_id: str, *, cwd: str | None = None) -> str:
        """Load an existing ACP session via ``session/load``."""
        self.ensure_initialized()
        result = self._request(
            "session/load",
            {
                "sessionId": session_id,
                "cwd": str(cwd or self._cwd),
                "mcpServers": [],
            },
            timeout=self._handshake_timeout,
        )
        loaded_id = str((result or {}).get("sessionId") or session_id).strip()
        self._active_session_id = loaded_id
        if isinstance(result, dict):
            self._record_session_models(result)
        return loaded_id

    def set_config_option(
        self,
        session_id: str,
        config_id: str,
        value: str,
    ) -> dict[str, Any]:
        """Set a session configuration option (``model``, ``thought_level``) via ``session/set_config_option``."""
        self.ensure_initialized()
        result = self._request(
            "session/set_config_option",
            {
                "sessionId": session_id,
                "configId": config_id,
                "value": value,
            },
            timeout=self._handshake_timeout,
        )
        return result if isinstance(result, dict) else {}

    def configure_session(
        self,
        session_id: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        """Apply model and thought_level settings to ``session_id`` when changed."""
        if model and model != self._active_model:
            try:
                self.set_config_option(session_id, "model", model)
                self._active_model = model
            except AntigravityAuthError:
                raise
            except AntigravityACPError as exc:
                logger.debug("session/set_config_option(model=%s) non-fatal notice: %s", model, exc)

        if reasoning_effort:
            thought_level = REASONING_EFFORT_TO_THOUGHT_LEVEL.get(
                reasoning_effort.strip().lower(),
                reasoning_effort.strip().lower(),
            )
            if thought_level and thought_level != self._active_thought_level:
                try:
                    self.set_config_option(session_id, "thought_level", thought_level)
                    self._active_thought_level = thought_level
                except AntigravityAuthError:
                    raise
                except AntigravityACPError as exc:
                    logger.debug(
                        "session/set_config_option(thought_level=%s) non-fatal notice: %s",
                        thought_level,
                        exc,
                    )

    def prompt(
        self,
        prompt_text: str,
        *,
        session_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        reuse_session: bool = True,
        on_text: Callable[[str], None] | None = None,
        on_thought: Callable[[str], None] | None = None,
        timeout: float | Any = None,
    ) -> TurnAccumulator:
        """Execute a ``session/prompt`` turn, recovering once on unexpected process crash."""
        resolved_timeout = coerce_timeout_seconds(timeout, default=self._request_timeout)
        with self._turn_lock:
            try:
                return self._execute_prompt_once(
                    prompt_text,
                    session_id=session_id,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    reuse_session=reuse_session,
                    on_text=on_text,
                    on_thought=on_thought,
                    timeout=resolved_timeout,
                )
            except AntigravityAuthError:
                raise
            except AntigravityACPError as exc:
                if not self._pm.is_running():
                    logger.warning("AGY ACP process exited unexpectedly (%s); restarting once.", exc)
                    self.restart()
                    return self._execute_prompt_once(
                        prompt_text,
                        session_id=None,
                        model=model,
                        reasoning_effort=reasoning_effort,
                        reuse_session=False,
                        on_text=on_text,
                        on_thought=on_thought,
                        timeout=resolved_timeout,
                    )
                raise

    def _execute_prompt_once(
        self,
        prompt_text: str,
        *,
        session_id: str | None,
        model: str | None,
        reasoning_effort: str | None,
        reuse_session: bool,
        on_text: Callable[[str], None] | None,
        on_thought: Callable[[str], None] | None,
        timeout: float | Any = None,
    ) -> TurnAccumulator:
        self.ensure_initialized()
        target_session = session_id or (self._active_session_id if reuse_session else None)
        if not target_session:
            target_session = self.create_session()

        self.configure_session(target_session, model=model, reasoning_effort=reasoning_effort)

        accumulator = TurnAccumulator(on_text=on_text, on_thought=on_thought)
        self._active_turn = accumulator
        effective_timeout = coerce_timeout_seconds(timeout, default=self._request_timeout)
        try:
            result = self._request(
                "session/prompt",
                {
                    "sessionId": target_session,
                    "prompt": [{"type": "text", "text": prompt_text}],
                },
                timeout=effective_timeout,
            )
            if isinstance(result, dict):
                stop_reason = result.get("stopReason") or result.get("finishReason")
                if isinstance(stop_reason, str) and stop_reason:
                    accumulator.stop_reason = "stop" if stop_reason == "end_turn" else stop_reason
                if not accumulator.text_chunks:
                    fallback_text = _extract_chunk_text(result)
                    if fallback_text:
                        accumulator.text_chunks.append(fallback_text)
                        if on_text is not None:
                            on_text(fallback_text)
            return accumulator
        finally:
            self._active_turn = None

    def cancel(self, session_id: str | None = None) -> None:
        """Send ``session/cancel`` for the active session."""
        target = session_id or self._active_session_id
        if not target or not self._pm.is_running():
            return
        try:
            self._notify("session/cancel", {"sessionId": target})
        except (OSError, RuntimeError) as exc:
            logger.debug("ACP session/cancel notification failed: %s", exc)

    def restart(self) -> None:
        """Restart the underlying AGY subprocess and clear session state."""
        with self._io_lock:
            self._reset_session_state_locked()
            proc = self._pm.restart()
            self._ensure_reader_thread_locked(proc)

    def close(self) -> None:
        """Cancel any active session and terminate the AGY subprocess."""
        with self._io_lock:
            if self._active_session_id and self._pm.is_running():
                self.cancel(self._active_session_id)
            self._reset_session_state_locked()
            self._pm.stop()

    def _reset_session_state_locked(self) -> None:
        self._initialized = False
        self._agent_capabilities = {}
        self._active_session_id = None
        self._active_model = None
        self._active_thought_level = None
        self._session_messages_snapshot = None
        self._fail_all_pending_locked("AGY ACP session reset.")

    def _record_session_models(self, payload: dict[str, Any]) -> None:
        models_block = payload.get("models")
        discovered: list[str] = []
        if isinstance(models_block, dict):
            for item in models_block.get("availableModels") or ():
                if isinstance(item, dict):
                    mid = str(item.get("modelId") or item.get("id") or "").strip()
                    if mid and mid not in discovered:
                        discovered.append(mid)
        for opt in payload.get("configOptions") or ():
            if isinstance(opt, dict) and opt.get("id") == "model":
                for choice in opt.get("options") or ():
                    if isinstance(choice, dict):
                        val = str(choice.get("value") or "").strip()
                        if val and val not in discovered:
                            discovered.append(val)
        if discovered:
            self._available_models = discovered

    def _ensure_reader_thread_locked(self, proc: Any) -> None:
        proc_id = id(proc)
        if self._reader_thread is not None and self._reader_thread.is_alive() and self._reader_proc_id == proc_id:
            return

        stdout_pipe = proc.stdout
        if stdout_pipe is None:
            raise AntigravityACPError("AGY subprocess stdout pipe is unavailable.")

        def _reader_loop() -> None:
            try:
                for raw_line in iter(stdout_pipe.readline, ""):
                    line = raw_line.strip()
                    if not line or not line.startswith("{"):
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError as exc:
                        logger.debug("Ignored malformed ACP JSON line: %s", exc)
                        continue
                    if isinstance(msg, dict):
                        self._dispatch_incoming(msg)
            except (OSError, ValueError) as exc:
                logger.debug("AGY ACP stdout reader closed: %s", exc)
            finally:
                with self._io_lock:
                    self._fail_all_pending_locked("AGY ACP subprocess stdout closed.")

        # Daemon thread only parses JSON-RPC stdout frames into queues; touches no profile scope.
        thread = threading.Thread(  # health: allow HX012 -- JSON-RPC frame demux thread; accesses no profile ContextVars
            target=_reader_loop,
            name="agy-acp-stdout",
            daemon=True,
        )
        thread.start()
        self._reader_thread = thread
        self._reader_proc_id = proc_id

    def _dispatch_incoming(self, msg: dict[str, Any]) -> None:
        has_id = "id" in msg and msg.get("id") is not None
        method = msg.get("method")

        if has_id and not method:
            req_id = msg.get("id")
            if isinstance(req_id, int):
                with self._io_lock:
                    waiter = self._pending.get(req_id)
                if waiter is not None:
                    waiter.put(msg)
            return

        if has_id and isinstance(method, str):
            self._handle_server_request(msg["id"], method, msg.get("params") or {})
            return

        if isinstance(method, str):
            self._handle_notification(method, msg.get("params") or {})

    def _handle_server_request(self, req_id: Any, method: str, params: Any) -> None:
        """Handle agent->client requests.

        CRITICAL (Phase 2.6): ``session/request_permission`` is always cancelled so AGY
        never executes internal file/shell/MCP tools outside Hermes's ``model_tools``.
        """
        if method == "session/request_permission":
            self._permission_denials += 1
            logger.debug("Denied AGY internal tool permission request (id=%s, params=%s)", req_id, params)
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"outcome": {"outcome": "cancelled"}},
                }
            )
            return

        logger.debug("Rejected unsupported AGY server->client request %s (id=%s)", method, req_id)
        self._send_json(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32601,
                    "message": f"Method '{method}' is disabled; Hermes executes tools via model_tools.",
                },
            }
        )

    def _handle_notification(self, method: str, params: Any) -> None:
        if method != "session/update" or not isinstance(params, dict):
            logger.debug("Ignored ACP notification method=%s", method)
            return

        update = params.get("update")
        if not isinstance(update, dict):
            return

        kind = str(update.get("sessionUpdate") or "").strip()
        acc = self._active_turn
        if acc is None:
            return

        handler = _UPDATE_DISPATCH.get(kind)
        if handler is not None:
            handler(acc, update)
        else:
            logger.debug("Ignored ACP sessionUpdate kind=%s", kind)

    def _request(self, method: str, params: dict[str, Any], *, timeout: float | Any) -> Any:
        effective_timeout = coerce_timeout_seconds(timeout, default=self._request_timeout)
        with self._io_lock:
            self._next_request_id += 1
            req_id = self._next_request_id
            waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            self._pending[req_id] = waiter
            try:
                self._send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "method": method,
                        "params": params,
                    }
                )
            except Exception:
                self._pending.pop(req_id, None)
                raise

        # HARD DEFENSIVE BOUNDARY (Phase 5):
        # 1. Strictly enforce numeric float (or None) before queue.Queue.get().
        # 2. Never allow an httpx.Timeout or any non-numeric object to reach the queue.
        # 3. Guard timeout string formatting against any unexpected types.
        queue_timeout: float | None
        if effective_timeout is None:
            queue_timeout = None
        elif isinstance(effective_timeout, (int, float)):
            queue_timeout = float(effective_timeout)
            if queue_timeout <= 0:
                queue_timeout = float(self._request_timeout)
        else:
            logger.error(
                "ACP transport received non-numeric timeout %s (%r); enforcing default %ss",
                type(effective_timeout).__name__,
                effective_timeout,
                self._request_timeout,
            )
            queue_timeout = float(self._request_timeout)

        assert queue_timeout is None or isinstance(queue_timeout, float), (
            f"Defensive assertion failed: queue_timeout must be float or None, got {type(queue_timeout).__name__}"
        )
        assert not hasattr(queue_timeout, "read"), (
            f"Defensive assertion failed: queue_timeout must not be an httpx.Timeout object: {queue_timeout!r}"
        )

        try:
            response = waiter.get(timeout=queue_timeout)
        except queue.Empty as exc:
            with self._io_lock:
                self._pending.pop(req_id, None)
            if method == "session/prompt":
                self.cancel()
            stderr_tail = self._pm.recent_stderr()
            suffix = f" (stderr: {stderr_tail})" if stderr_tail else ""
            timeout_display = f"{queue_timeout:.1f}s" if isinstance(queue_timeout, (int, float)) else "indefinite"
            raise AntigravityACPError(
                f"AGY ACP request '{method}' timed out after {timeout_display}{suffix}"
            ) from exc

        with self._io_lock:
            self._pending.pop(req_id, None)

        if "error" in response and response["error"] is not None:
            err = response["error"]
            if isinstance(err, dict):
                code = err.get("code")
                msg = str(err.get("message") or "Unknown ACP error")
                data = err.get("data")
            else:
                code = None
                msg = str(err)
                data = None
            redacted_msg = redact_sensitive_text(msg)
            if _is_auth_failure(code, redacted_msg, data):
                raise AntigravityAuthError(
                    f"Google Antigravity authentication required: {redacted_msg}. "
                    "Run 'agy login' to authenticate with Google OAuth.",
                    code=code,
                    data=data,
                )
            raise AntigravityACPError(
                f"AGY ACP '{method}' error ({code}): {redacted_msg}",
                code=code,
                data=data,
            )

        return response.get("result")

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send_json({"jsonrpc": "2.0", "method": method, "params": params})

    def _send_json(self, payload: dict[str, Any]) -> None:
        with self._io_lock:
            stdin_pipe = self._pm.stdin()
            line = json.dumps(payload, ensure_ascii=False)
            stdin_pipe.write(line + "\n")
            stdin_pipe.flush()

    def _fail_all_pending_locked(self, reason: str) -> None:
        stderr_tail = self._pm.recent_stderr()
        detail = f"{reason} (stderr: {stderr_tail})" if stderr_tail else reason
        for req_id, waiter in list(self._pending.items()):
            try:
                waiter.put_nowait(
                    {
                        "id": req_id,
                        "error": {"code": -32099, "message": detail},
                    }
                )
            except queue.Full as exc:
                logger.debug("Pending waiter %s already full during reset: %s", req_id, exc)
        self._pending.clear()
