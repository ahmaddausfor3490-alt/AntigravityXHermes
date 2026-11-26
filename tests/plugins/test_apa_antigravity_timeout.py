"""Antigravity provider: OpenAI-SDK ``timeout`` handling at the ACP boundary.

Regression for: ``'<' not supported between instances of 'Timeout' and 'int'``.
Hermes core's streaming path calls ``chat.completions.create(timeout=httpx.Timeout(...))``
(the OpenAI SDK contract is ``float | httpx.Timeout | None``). The Antigravity client forwarded
that object unchanged to ``queue.Queue.get(timeout=...)`` in the ACP transport.
"""

from __future__ import annotations

from pathlib import Path
import sys
import textwrap

import httpx
import pytest

from providers import get_provider_profile

MOCK_ACP = textwrap.dedent(
    """\
    import json, os, sys, time

    marker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crashed.once")

    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        msg = json.loads(raw)
        rid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1, "agentCapabilities": {}}})
        elif method == "session/new":
            send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "sess-1"}})
        elif method == "session/set_config_option":
            send({"jsonrpc": "2.0", "id": rid, "result": {"configOptions": []}})
        elif method == "session/cancel":
            continue
        elif method == "session/prompt":
            text = (params.get("prompt") or [{}])[0].get("text", "")
            if "CRASH_ONCE" in text and not os.path.exists(marker):
                open(marker, "w").close()
                sys.exit(1)
            if "SLOW:" in text:
                time.sleep(float(text.split("SLOW:")[1].split()[0]))
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "sess-1",
                  "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Hello!"}}}})
            send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
    """
)


def _hermes_stream_timeout(read: float, write: float, conn: float) -> httpx.Timeout:
    """The exact shape ``chat_completion_helpers._open_stream`` passes to the client."""
    return httpx.Timeout(connect=conn, read=read, write=write, pool=conn)


@pytest.fixture
def client(tmp_path: Path):
    script = tmp_path / "mock_acp.py"
    script.write_text(MOCK_ACP, encoding="utf-8")
    profile = get_provider_profile("antigravity")
    assert profile is not None
    c = profile.create_client(
        command=sys.executable,
        args=[str(script), "acp", "--stdio"],
        acp_cwd=str(tmp_path),
        enable_print_fallback=False,
    )
    try:
        yield c
    finally:
        c.close()


def _stream_text(client, prompt: str, timeout) -> str:
    stream = client.chat.completions.create(
        model="gemini-4-argon-medium",
        messages=[{"role": "user", "content": prompt}],
        stream=True,
        timeout=timeout,
    )
    return "".join(ch.choices[0].delta.content or "" for ch in stream if ch.choices)


def test_request_budget_follows_openai_timeout_contract():
    from plugins.model_providers.antigravity.client import _request_budget_seconds

    assert _request_budget_seconds(None) is None
    assert _request_budget_seconds(45) == 45.0
    assert _request_budget_seconds(12.5) == 12.5
    # Hermes defaults: read=120 (per-chunk idle), write=1800 (whole request) -> whole-turn budget.
    assert _request_budget_seconds(_hermes_stream_timeout(120.0, 1800.0, 30.0)) == 1800.0
    # A per-provider request_timeout_seconds sets every component to the same value.
    assert _request_budget_seconds(_hermes_stream_timeout(600.0, 600.0, 60.0)) == 600.0
    # No numeric component -> transport default, never 0 / never a crash.
    assert _request_budget_seconds(httpx.Timeout(None)) is None


def test_streaming_hi_with_hermes_httpx_timeout(client):
    """The failing user scenario: a plain 'hi' over the streaming path."""
    assert _stream_text(client, "hi", _hermes_stream_timeout(120.0, 1800.0, 30.0)) == "Hello!"


def test_non_streaming_with_httpx_timeout(client):
    resp = client.chat.completions.create(
        model="gemini-4-argon-medium",
        messages=[{"role": "user", "content": "hi"}],
        stream=False,
        timeout=_hermes_stream_timeout(120.0, 1800.0, 30.0),
    )
    assert resp.choices[0].message.content == "Hello!"


def test_request_timeout_still_raises_acp_error(client):
    from plugins.model_providers.antigravity import AntigravityACPError

    with pytest.raises(AntigravityACPError, match=r"timed out after 0\.5s"):
        _stream_text(client, "SLOW:3", httpx.Timeout(0.5))


def test_long_running_turn_uses_whole_request_budget_not_read(client):
    """read=0.5s alone would kill a 1.5s turn; the ACP wait spans the whole turn."""
    assert _stream_text(client, "SLOW:1.5", _hermes_stream_timeout(0.5, 10.0, 0.5)) == "Hello!"


def test_retry_after_transient_process_crash(client):
    """Transport restarts once on subprocess exit; the coerced timeout survives the retry."""
    # First turn succeeds
    assert _stream_text(client, "hi", _hermes_stream_timeout(120.0, 1800.0, 30.0)) == "Hello!"
    # Process is stopped between turns (transient failure/restart)
    client.process_manager.stop()
    assert not client.process_manager.is_running()
    # Next turn automatically restarts and processes with httpx.Timeout
    assert _stream_text(client, "hi again", _hermes_stream_timeout(120.0, 1800.0, 30.0)) == "Hello!"
    assert client.process_manager.is_running()


def test_none_timeout_keeps_transport_default(client, monkeypatch: pytest.MonkeyPatch):
    seen: list[object] = []
    original = client.transport._request

    def spy(method, params, *, timeout):
        if method == "session/prompt":
            seen.append(timeout)
        return original(method, params, timeout=timeout)

    monkeypatch.setattr(client.transport, "_request", spy)
    assert _stream_text(client, "hi", None) == "Hello!"
    assert _stream_text(client, "hi", _hermes_stream_timeout(120.0, 1800.0, 30.0)) == "Hello!"
    assert seen == [client.transport._request_timeout, 1800.0]
    assert all(isinstance(t, float) for t in seen)


def test_streaming_with_float_timeout(client):
    """Streaming with standard numeric float timeout works seamlessly."""
    assert _stream_text(client, "hi", 45.0) == "Hello!"


def test_streaming_with_integer_timeout(client):
    """Streaming with standard integer timeout works seamlessly."""
    assert _stream_text(client, "hi", 30) == "Hello!"


def test_direct_stream_completion_with_httpx_timeout(client):
    """Direct call to client.stream_completion normalizes httpx.Timeout without TypeError."""
    stream = client.stream_completion(
        model="gemini-4-argon-medium",
        messages=[{"role": "user", "content": "hi direct"}],
        timeout=_hermes_stream_timeout(120.0, 1800.0, 30.0),
    )
    chunks = list(stream)
    text = "".join(ch.choices[0].delta.content or "" for ch in chunks if ch.choices)
    assert text == "Hello!"


def test_direct_create_completion_with_httpx_timeout(client):
    """Direct call to client.create_completion normalizes httpx.Timeout without TypeError."""
    resp = client.create_completion(
        model="gemini-4-argon-medium",
        messages=[{"role": "user", "content": "hi direct sync"}],
        timeout=_hermes_stream_timeout(120.0, 1800.0, 30.0),
    )
    assert resp.choices[0].message.content == "Hello!"


def test_direct_transport_prompt_with_httpx_timeout(client):
    """Direct call to transport.prompt normalizes httpx.Timeout without TypeError."""
    acc = client.transport.prompt(
        "hi direct prompt",
        model="gemini-4-argon-medium",
        timeout=_hermes_stream_timeout(120.0, 1800.0, 30.0),
    )
    assert acc.full_text == "Hello!"


def test_direct_transport_request_with_httpx_timeout(client):
    """Direct call to transport._request normalizes httpx.Timeout before waiter.get()."""
    client.transport.ensure_initialized()
    sid = client.transport.create_session()
    res = client.transport._request(
        "session/set_config_option",
        {"sessionId": sid, "configId": "model", "value": "gemini-4-argon-medium"},
        timeout=_hermes_stream_timeout(120.0, 1800.0, 30.0),
    )
    assert isinstance(res, dict)


def test_coerce_timeout_seconds_edge_cases():
    """Unit validation for coerce_timeout_seconds helper across types and edge cases."""
    from plugins.model_providers.antigravity.acp_transport import coerce_timeout_seconds

    # None and invalid / non-positive numbers fall back to default
    assert coerce_timeout_seconds(None, default=300.0) == 300.0
    assert coerce_timeout_seconds(0, default=300.0) == 300.0
    assert coerce_timeout_seconds(-10.0, default=300.0) == 300.0
    assert coerce_timeout_seconds("not-a-number", default=300.0) == 300.0
    assert coerce_timeout_seconds([], default=300.0) == 300.0

    # Plain positive numeric
    assert coerce_timeout_seconds(30) == 30.0
    assert coerce_timeout_seconds(12.5) == 12.5

    # httpx.Timeout variations
    assert coerce_timeout_seconds(httpx.Timeout(connect=5.0, read=120.0, write=1800.0, pool=5.0)) == 1800.0
    assert coerce_timeout_seconds(httpx.Timeout(60.0)) == 60.0
    assert coerce_timeout_seconds(httpx.Timeout(None), default=300.0) == 300.0


def test_recovery_after_timeout_expiration(client):
    """After an ACP request times out, subsequent requests recover and succeed."""
    from plugins.model_providers.antigravity import AntigravityACPError

    # Slow request triggers timeout
    with pytest.raises(AntigravityACPError, match=r"timed out after 0\.5s"):
        _stream_text(client, "SLOW:2.0", httpx.Timeout(0.5))

    # Next request immediately succeeds
    assert _stream_text(client, "hi after timeout", _hermes_stream_timeout(120.0, 1800.0, 30.0)) == "Hello!"


def test_cancellation_during_streaming(client):
    """Calling cancel() on active client succeeds cleanly."""
    client.cancel()
    # Stream still works after cancel
    assert _stream_text(client, "hi after cancel", 30.0) == "Hello!"

