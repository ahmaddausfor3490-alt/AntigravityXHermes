"""Behavioural and integration tests for the Google Antigravity model provider plugin (APA Phase 2)."""

from __future__ import annotations

from pathlib import Path
import sys
import textwrap
from types import SimpleNamespace

import pytest

from agent.acp_client import ACPClient
from agent.turn_api_call import _should_stream
from providers import get_provider_profile, list_providers


MOCK_AGY_SERVER_SCRIPT = textwrap.dedent(
    """\
    import json
    import os
    import sys

    args = sys.argv[1:]
    mode = os.environ.get("AGY_TEST_MODE", "normal")

    if "--version" in args:
        print("Antigravity CLI v1.5.9-test")
        sys.exit(0)

    if args and args[0] == "models":
        if mode == "unauthenticated":
            sys.stderr.write("Error: Not logged in. Please run agy login.\\n")
            sys.exit(1)
        print("gemini-4-argon-medium\\tGemini 4 Argon (Medium)")
        print("gemini-4-hydrogen-high\\tGemini 4 Hydrogen (High)")
        print("claude-sonnet-5-5-high\\tClaude Sonnet 5.5 (High)")
        sys.exit(0)

    if args and args[0] == "login":
        print("Logged in via Google OAuth")
        sys.exit(0)

    if args and args[0] == "logout":
        print("Logged out")
        sys.exit(0)

    if "-p" in args:
        prompt_arg = args[-1] if args else ""
        print(json.dumps({"response": f"Print fallback reply for: {prompt_arg[:40]}"}))
        sys.exit(0)

    # ACP stdio mode: agy acp --stdio
    session_counter = 0
    config_log = []

    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        msg = json.loads(line)
        req_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}

        if method == "initialize":
            send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": 1,
                    "agentInfo": {"name": "agy-mock", "version": "1.5.9"},
                    "agentCapabilities": {"loadSession": True},
                },
            })
        elif method == "initialized":
            continue
        elif method == "session/new":
            if mode == "auth_error":
                send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": "Authentication required. Please run agy login."},
                })
                continue
            session_counter += 1
            sid = f"sess-{session_counter}"
            send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "sessionId": sid,
                    "models": {
                        "currentModelId": "gemini-4-argon-medium",
                        "availableModels": [
                            {"modelId": "gemini-4-argon-medium", "name": "Gemini 4 Argon"},
                            {"modelId": "gemini-3.8-flash-low", "name": "Gemini 3.8 Flash"},
                        ],
                    },
                },
            })
        elif method == "session/load":
            send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"sessionId": params.get("sessionId", "sess-loaded")},
            })
        elif method == "session/set_config_option":
            config_log.append((params.get("configId"), params.get("value")))
            send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"configOptions": []},
            })
        elif method == "session/cancel":
            continue
        elif method == "session/prompt":
            sid = params.get("sessionId", "sess-1")
            prompt_items = params.get("prompt") or []
            prompt_text = prompt_items[0].get("text", "") if prompt_items else ""

            # Emit an unknown notification type first to verify graceful handling
            send({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": {"sessionUpdate": "custom_unknown_telemetry", "detail": "ignored"},
                },
            })

            if "TRIGGER_PERMISSION_REQUEST" in prompt_text:
                # Server->client request that must be cancelled by Hermes ACP transport
                send({
                    "jsonrpc": "2.0",
                    "id": 9001,
                    "method": "session/request_permission",
                    "params": {"sessionId": sid, "tool": "bash"},
                })
                perm_reply = json.loads(sys.stdin.readline().strip())
                outcome = ((perm_reply.get("result") or {}).get("outcome") or {}).get("outcome")
                send({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": f"Permission outcome={outcome}"},
                        },
                    },
                })
                send({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}})
                continue

            if "TRIGGER_SINGLE_TOOL" in prompt_text:
                send({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {
                            "sessionUpdate": "agent_thought_chunk",
                            "content": {"type": "text", "text": "Checking files first."},
                        },
                    },
                })
                chunks = [
                    "Let me inspect the workspace. ",
                    "<tool_",
                    'call>{"id": "call_abc", "name": "read_file", "arguments": {"path": "README.md"}}</tool_call>',
                ]
                for part in chunks:
                    send({
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": sid,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": part},
                            },
                        },
                    })
                send({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}})
                continue

            if "TRIGGER_MULTI_TOOL" in prompt_text:
                multi_text = (
                    '<tool_call>{"id": "call_1", "name": "search_files", "arguments": {"pattern": "*.py"}}</tool_call>\\n'
                    '<tool_call>{"id": "call_2", "name": "terminal", "arguments": {"command": "git status"}}</tool_call>'
                )
                send({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": multi_text},
                        },
                    },
                })
                send({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}})
                continue

            # Normal streaming response
            send({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "agent_thought_chunk",
                        "content": {"type": "text", "text": "Thinking step 1."},
                    },
                },
            })
            for word in ("Hello ", "from ", f"session {sid}!"):
                send({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": word},
                        },
                    },
                })
            send({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "usage_update",
                        "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
                    },
                },
            })
            send({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}})
    """
)


@pytest.fixture
def mock_agy_script(tmp_path: Path) -> Path:
    script = tmp_path / "mock_agy_server.py"
    script.write_text(MOCK_AGY_SERVER_SCRIPT, encoding="utf-8")
    return script


# ---------------------------------------------------------------------------
# Phase 2.1 — Provider Plugin Scaffold & Discovery
# ---------------------------------------------------------------------------


def test_antigravity_provider_discovered_and_registered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermes discovers the bundled antigravity provider plugin and resolves its aliases."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    profile = get_provider_profile("antigravity")
    assert profile is not None
    assert profile.name == "antigravity"
    assert profile.auth_type == "external_process"
    assert profile.base_url == "process://antigravity"
    assert profile.supports_streaming is True
    assert profile.process_command == "agy"
    assert profile.process_args == ("acp", "--stdio")
    assert "gemini-4-argon-medium" in profile.fallback_models

    # Alias lookup
    assert get_provider_profile("agy") is profile
    assert get_provider_profile("google-antigravity") is profile
    assert any(p.name == "antigravity" for p in list_providers())

    # Model alias & auxiliary model resolution
    assert profile.resolve_model("argon") == "gemini-4-argon-medium"
    assert profile.resolve_aux_model() == "gemini-3.8-flash-low"


def test_antigravity_enables_streaming_in_turn_api_call() -> None:
    """_should_stream() returns True for antigravity's process://antigravity + supports_streaming=True."""
    profile = get_provider_profile("antigravity")
    assert profile is not None
    fake_agent = SimpleNamespace(
        _streaming_enabled=True,
        provider="antigravity",
        base_url=profile.base_url,
        api_mode="chat_completions",
        provider_profile=profile,
        _provider_profile=profile,
        _has_stream_consumers=lambda: True,
    )
    assert _should_stream(fake_agent) is True


# ---------------------------------------------------------------------------
# Phase 2.2 — AGY Process Manager & Windows Environment Preservation
# ---------------------------------------------------------------------------


def test_process_manager_preserves_windows_env_and_strips_unrelated_secrets(
    tmp_path: Path,
) -> None:
    """build_agy_env preserves Windows critical vars while excluding unrelated provider API keys."""
    profile = get_provider_profile("antigravity")
    assert profile is not None
    client = profile.create_client(command=sys.executable, args=["-c", "pass"], acp_cwd=str(tmp_path))
    pm = client.process_manager

    sample_env = {
        "SYSTEMROOT": r"C:\Windows",
        "LOCALAPPDATA": r"C:\Users\test\AppData\Local",
        "APPDATA": r"C:\Users\test\AppData\Roaming",
        "USERPROFILE": r"C:\Users\test",
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "PATH": r"C:\Windows\System32",
        "AGY_CUSTOM_FLAG": "enabled",
        "OPENAI_API_KEY": "sk-secret-do-not-leak",
        "ANTHROPIC_API_KEY": "sk-ant-secret-do-not-leak",
    }
    from plugins.model_providers.antigravity import build_agy_env

    sanitized = build_agy_env(sample_env)
    assert sanitized["SYSTEMROOT"] == r"C:\Windows"
    assert sanitized["LOCALAPPDATA"] == r"C:\Users\test\AppData\Local"
    assert sanitized["APPDATA"] == r"C:\Users\test\AppData\Roaming"
    assert sanitized["USERPROFILE"] == r"C:\Users\test"
    assert sanitized["PATHEXT"] == ".COM;.EXE;.BAT;.CMD"
    assert sanitized["AGY_CUSTOM_FLAG"] == "enabled"
    assert "OPENAI_API_KEY" not in sanitized
    assert "ANTHROPIC_API_KEY" not in sanitized
    pm.stop()


def test_process_manager_version_and_live_models(mock_agy_script: Path, tmp_path: Path) -> None:
    """AGYProcessManager resolves executable, checks version, and parses `agy models`."""
    from plugins.model_providers.antigravity import AGYProcessManager

    wrapper = tmp_path / ("agy.cmd" if sys.platform == "win32" else "agy")
    if sys.platform == "win32":
        wrapper.write_text(f'@"{sys.executable}" "{mock_agy_script}" %*\r\n', encoding="utf-8")
    else:
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{mock_agy_script}" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)

    pm = AGYProcessManager(command=str(wrapper), cwd=str(tmp_path))
    ver = pm.verify_version()
    assert "1.5.9-test" in ver.version

    models = pm.fetch_live_models()
    assert models == [
        "gemini-4-argon-medium",
        "gemini-4-hydrogen-high",
        "claude-sonnet-5-5-high",
    ]


# ---------------------------------------------------------------------------
# Phase 2.3 & 2.4 — ACP Transport, Session Reuse & OpenAI Normalization
# ---------------------------------------------------------------------------


def test_acp_transport_session_reuse_and_compression_reset(
    mock_agy_script: Path,
    tmp_path: Path,
) -> None:
    """AntigravityClient reuses the persistent ACP session across turns and resets on compression."""
    profile = get_provider_profile("antigravity")
    assert profile is not None
    client = profile.create_client(
        command=sys.executable,
        args=[str(mock_agy_script), "acp", "--stdio"],
        acp_cwd=str(tmp_path),
    )
    assert isinstance(client, ACPClient)

    try:
        # Turn 1 creates sess-1
        messages_t1 = [
            {"role": "system", "content": "You are APA."},
            {"role": "user", "content": "First question"},
        ]
        resp1 = client.chat.completions.create(
            model="gemini-4-argon-medium",
            messages=messages_t1,
            stream=False,
            reasoning_effort="high",
        )
        assert "session sess-1!" in (resp1.choices[0].message.content or "")
        assert resp1.choices[0].message.reasoning_content == "Thinking step 1."
        assert resp1.usage.total_tokens == 18
        first_session_id = client.transport.active_session_id
        assert first_session_id == "sess-1"

        # Turn 2 appends to messages_t1 -> reuses sess-1
        messages_t2 = [
            *messages_t1,
            {"role": "assistant", "content": resp1.choices[0].message.content},
            {"role": "user", "content": "Follow-up question"},
        ]
        resp2 = client.chat.completions.create(
            model="gemini-4-argon-medium",
            messages=messages_t2,
            stream=False,
        )
        assert "session sess-1!" in (resp2.choices[0].message.content or "")
        assert client.transport.active_session_id == "sess-1"

        # Context compression replaces prior history -> triggers new session sess-2
        compressed_messages = [
            {"role": "system", "content": "You are APA. [Compressed summary]"},
            {"role": "user", "content": "Post-compression question"},
        ]
        resp3 = client.chat.completions.create(
            model="gemini-4-argon-medium",
            messages=compressed_messages,
            stream=False,
        )
        assert "session sess-2!" in (resp3.choices[0].message.content or "")
        assert client.transport.active_session_id == "sess-2"
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Phase 2.5 & 2.6 — Streaming, Lookahead Buffer & Tool Calling Bridge
# ---------------------------------------------------------------------------


def test_streaming_incremental_chunks_and_tool_call_lookahead(
    mock_agy_script: Path,
    tmp_path: Path,
) -> None:
    """Streaming yields prose & thoughts immediately while preventing <tool_call> XML leakage."""
    profile = get_provider_profile("antigravity")
    assert profile is not None
    client = profile.create_client(
        command=sys.executable,
        args=[str(mock_agy_script), "acp", "--stdio"],
        acp_cwd=str(tmp_path),
    )

    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file from disk",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ]

    try:
        stream = client.chat.completions.create(
            model="gemini-4-argon-medium",
            messages=[{"role": "user", "content": "TRIGGER_SINGLE_TOOL"}],
            tools=tools,
            stream=True,
        )
        chunks = list(stream)

        emitted_content = "".join(
            c.choices[0].delta.content for c in chunks if c.choices[0].delta.content
        )
        emitted_reasoning = "".join(
            getattr(c.choices[0].delta, "reasoning_content", "") or ""
            for c in chunks
        )
        tool_call_Verify = [
            tc
            for c in chunks
            for tc in (c.choices[0].delta.tool_calls or [])
        ]

        # Normal prose streamed cleanly, and zero <tool_call> XML leaked into content!
        assert emitted_content == "Let me inspect the workspace. "
        assert "<tool_call>" not in emitted_content
        assert "</tool_call>" not in emitted_content
        assert emitted_reasoning == "Checking files first."

        assert len(tool_call_Verify) == 1
        assert tool_call_Verify[0].id == "call_abc"
        assert tool_call_Verify[0].function.name == "read_file"
        assert '"README.md"' in tool_call_Verify[0].function.arguments
        assert chunks[-1].choices[0].finish_reason == "tool_calls"
    finally:
        client.close()


def test_multiple_tool_calls_and_permission_denial(
    mock_agy_script: Path,
    tmp_path: Path,
) -> None:
    """Multiple tool calls are extracted cleanly and AGY internal tool permission requests are cancelled."""
    profile = get_provider_profile("antigravity")
    assert profile is not None
    client = profile.create_client(
        command=sys.executable,
        args=[str(mock_agy_script), "acp", "--stdio"],
        acp_cwd=str(tmp_path),
    )
    tools = [
        {"type": "function", "function": {"name": "search_files", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "terminal", "parameters": {"type": "object"}}},
    ]

    try:
        resp = client.chat.completions.create(
            model="gemini-4-argon-medium",
            messages=[{"role": "user", "content": "TRIGGER_MULTI_TOOL"}],
            tools=tools,
            stream=False,
        )
        tcs = resp.choices[0].message.tool_calls or []
        assert len(tcs) == 2
        assert tcs[0].id == "call_1"
        assert tcs[0].function.name == "search_files"
        assert tcs[1].id == "call_2"
        assert tcs[1].function.name == "terminal"
        assert resp.choices[0].finish_reason == "tool_calls"

        # Verify AGY internal session/request_permission is strictly cancelled
        perm_resp = client.chat.completions.create(
            model="gemini-4-argon-medium",
            messages=[{"role": "user", "content": "TRIGGER_PERMISSION_REQUEST"}],
            stream=False,
        )
        assert "Permission outcome=cancelled" in (perm_resp.choices[0].message.content or "")
        assert client.transport.permission_denials == 1
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Phase 2.7 — Authentication Errors, Crash Recovery & Print Fallback
# ---------------------------------------------------------------------------


def test_auth_error_detection_and_classification(
    mock_agy_script: Path,
    tmp_path: Path,
) -> None:
    """ACP -32000 auth failure raises AntigravityAuthError with 'agy login' guidance."""
    from plugins.model_providers.antigravity import AntigravityAuthError

    profile = get_provider_profile("antigravity")
    assert profile is not None
    assert profile.classify_api_error("Google Antigravity authentication required (-32000)") == "auth"

    client = profile.create_client(
        command=sys.executable,
        args=[str(mock_agy_script), "acp", "--stdio"],
        acp_cwd=str(tmp_path),
        env={"AGY_TEST_MODE": "auth_error"},
    )
    try:
        with pytest.raises(AntigravityAuthError, match="agy login"):
            client.chat.completions.create(
                model="gemini-4-argon-medium",
                messages=[{"role": "user", "content": "Hello"}],
                stream=False,
            )
    finally:
        client.close()


def test_process_crash_recovery_and_print_fallback(
    mock_agy_script: Path,
    tmp_path: Path,
) -> None:
    """Transport recovers when subprocess is killed between turns, and print fallback works."""
    profile = get_provider_profile("antigravity")
    assert profile is not None
    client = profile.create_client(
        command=sys.executable,
        args=[str(mock_agy_script), "acp", "--stdio"],
        acp_cwd=str(tmp_path),
    )
    try:
        resp1 = client.chat.completions.create(
            model="gemini-4-argon-medium",
            messages=[{"role": "user", "content": "Before crash"}],
            stream=False,
        )
        assert "session sess-1!" in (resp1.choices[0].message.content or "")

        # Simulate unexpected process exit between turns
        client.process_manager.stop()
        assert not client.process_manager.is_running()

        resp2 = client.chat.completions.create(
            model="gemini-4-argon-medium",
            messages=[{"role": "user", "content": "After crash"}],
            stream=False,
        )
        assert "session sess-1!" in (resp2.choices[0].message.content or "")
        assert client.process_manager.is_running()

        # Verify print-mode fallback via AGYProcessManager (and flag ordering: -p is last before prompt)
        fallback_out = client.process_manager.run_print_prompt(
            "Hello fallback",
            model="gemini-4-argon-medium",
        )
        assert "Print fallback reply" in str(fallback_out["stdout"])

        # Verify Path / WindowsPath acp_cwd is coerced to str for JSON-RPC serialization
        path_client = profile.create_client(
            command=sys.executable,
            args=[str(mock_agy_script), "acp", "--stdio"],
            acp_cwd=tmp_path,
        )
        try:
            path_resp = path_client.chat.completions.create(
                model="gemini-4-argon-medium",
                messages=[{"role": "user", "content": "Path cwd test"}],
                stream=False,
            )
            assert "session sess-1!" in (path_resp.choices[0].message.content or "")
        finally:
            path_client.close()
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Phase 2.12 Regression — AGY-STREAM-CANCEL-01 Finish Reason Normalization
# ---------------------------------------------------------------------------


def test_make_stream_chunk_finish_reason_normalization() -> None:
    """Verify make_stream_chunk normalizes non-standard finish_reason and prevents Pydantic ValidationError."""
    from openai.types.chat.chat_completion_chunk import (
        ChoiceDeltaToolCall,
        ChoiceDeltaToolCallFunction,
    )
    from plugins.model_providers.antigravity.bridge import make_stream_chunk

    sample_tool_call = ChoiceDeltaToolCall(
        index=0,
        id="call_reg_1",
        type="function",
        function=ChoiceDeltaToolCallFunction(name="test_func", arguments='{"q": "apa"}'),
    )

    # 1. Normal streaming completion (intermediate & terminal)
    chunk_intermediate = make_stream_chunk(
        chunk_id="chunk-test-1",
        created=1000,
        model="gemini-4-argon-medium",
        content="Hello world",
        include_role=True,
    )
    assert chunk_intermediate.choices[0].finish_reason is None
    assert chunk_intermediate.choices[0].delta.content == "Hello world"
    assert chunk_intermediate.choices[0].delta.role == "assistant"

    chunk_normal_stop = make_stream_chunk(
        chunk_id="chunk-test-2",
        created=1000,
        model="gemini-4-argon-medium",
        finish_reason="stop",
    )
    assert chunk_normal_stop.choices[0].finish_reason == "stop"

    # 2. Tool-call completion
    chunk_tool_call = make_stream_chunk(
        chunk_id="chunk-test-3",
        created=1000,
        model="gemini-4-argon-medium",
        tool_calls=[sample_tool_call],
        finish_reason="tool_calls",
    )
    assert chunk_tool_call.choices[0].finish_reason == "tool_calls"
    assert chunk_tool_call.choices[0].delta.tool_calls is not None
    assert len(chunk_tool_call.choices[0].delta.tool_calls) == 1

    # 3. Mid-stream cancellation (ACP returns stopReason='cancelled')
    chunk_cancelled = make_stream_chunk(
        chunk_id="chunk-test-4",
        created=1000,
        model="gemini-4-argon-medium",
        finish_reason="cancelled",
    )
    assert chunk_cancelled.choices[0].finish_reason == "stop"

    chunk_cancelled_with_tool = make_stream_chunk(
        chunk_id="chunk-test-5",
        created=1000,
        model="gemini-4-argon-medium",
        tool_calls=[sample_tool_call],
        finish_reason="cancelled",
    )
    assert chunk_cancelled_with_tool.choices[0].finish_reason == "tool_calls"

    # 4. Invalid / unknown finish_reason values normalize to 'stop' or 'tool_calls'
    for invalid_reason in ("unknown_provider_code", "interrupted", "", "error"):
        chunk_invalid = make_stream_chunk(
            chunk_id="chunk-test-6",
            created=1000,
            model="gemini-4-argon-medium",
            finish_reason=invalid_reason,
        )
        assert chunk_invalid.choices[0].finish_reason == "stop"

    # 5. All standard OpenAI finish_reasons are preserved intact
    for standard_reason in ("stop", "length", "tool_calls", "content_filter", "function_call"):
        chunk_standard = make_stream_chunk(
            chunk_id="chunk-test-7",
            created=1000,
            model="gemini-4-argon-medium",
            finish_reason=standard_reason,
        )
        assert chunk_standard.choices[0].finish_reason == standard_reason


def test_finalize_stream_chunks_handles_cancellation_cleanly() -> None:
    """_finalize_stream_chunks emits valid ChatCompletionChunk when stopReason is 'cancelled'."""
    from typing import Any

    from plugins.model_providers.antigravity.bridge import StreamingToolCallBuffer
    from plugins.model_providers.antigravity.client import _finalize_stream_chunks

    lookahead = StreamingToolCallBuffer(parse_tool_calls=True)
    lookahead.feed("Prose before mid-stream cancel.")
    acc_mock = SimpleNamespace(
        usage={"prompt_tokens": 15, "completion_tokens": 8, "total_tokens": 23},
        full_thought="",
        stop_reason="cancelled",
    )

    class _MockTransport:
        def set_session_messages_snapshot(self, _snapshot: Any) -> None:
            pass

    chunks = _finalize_stream_chunks(
        chunk_id="chunk-cancel-finalize",
        created=2000,
        model="gemini-4-argon-medium",
        prompt_text="Test prompt",
        messages=[{"role": "user", "content": "Test prompt"}],
        acc_result=acc_mock,
        lookahead=lookahead,
        transport=_MockTransport(),  # type: ignore[arg-type]
        role_sent=False,
    )

    assert len(chunks) >= 1
    # Terminal chunk has finish_reason normalized to 'stop' without ValidationError
    assert chunks[-1].choices[0].finish_reason == "stop"
    assert chunks[-1].usage is not None

