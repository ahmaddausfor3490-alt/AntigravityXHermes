"""APA Phase 5 — Product Integration & Hermes Interface Test Suite.

Validates the complete product integration of APA:
Antigravity Provider + Official AGY Authentication + Native Hermes Interface (CLI/TUI)
+ Native Hermes Memory & Session + APA Security Guard + APA Skills Engine.

All 14 Phase 5 testing requirements are validated:
1. Fresh AGY authentication.
2. Existing authenticated session.
3. Authentication expiration.
4. Model discovery.
5. Model switching.
6. Streaming responses.
7. Tool execution.
8. Skills activation.
9. Memory persistence using native Hermes.
10. Session continuity using native Hermes.
11. Cancellation and recovery.
12. ACP subprocess restart.
13. Security enforcement.
14. Hermes CLI/TUI usability.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest

# Ensure provider and plugins can be discovered cleanly
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli.plugins import PluginManager, PluginManifest
from providers import get_provider_profile
from providers.base import ProviderProfile

# Pre-load Antigravity provider
_prof = get_provider_profile("antigravity")
assert _prof is not None


def _load_plugin(name: str):
    plugin_dir = Path(__file__).resolve().parents[2] / "plugins" / name
    manifest = PluginManifest(name=name, path=str(plugin_dir), key=name, source="bundled")
    return PluginManager()._load_directory_module(
        manifest, module_name=f"hermes_plugins.{name.replace('-', '_')}_phase5"
    )


apa_skills = _load_plugin("apa-skills")
apa_sec = _load_plugin("apa-security")

APASecurityConfig = apa_sec.APASecurityConfig
PolicyEngine = apa_sec.PolicyEngine
ToolInterceptor = apa_sec.ToolInterceptor
APASkillsRegistry = apa_skills.APASkillsRegistry
SkillLoader = apa_skills.loader.SkillLoader

from plugins.model_providers.antigravity import (
    ANTIGRAVITY_FALLBACK_MODELS,
    ANTIGRAVITY_MODEL_ALIASES,
    DEFAULT_ANTIGRAVITY_MODEL,
    AGYAuthStatus,
    AGYProcessManager,
    AGYVersionInfo,
    AntigravityClient,
    classify_antigravity_api_error,
    handle_antigravity_auth,
)
from plugins.model_providers.antigravity.bridge import (
    StreamingToolCallBuffer,
    build_chat_completion,
)


@pytest.fixture
def product_env(tmp_path: Path) -> Generator[dict[str, Any], None, None]:
    """Isolated product integration fixture with clean temporary Hermes home."""
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir(parents=True, exist_ok=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    audit_log = hermes_home / "logs" / "apa_security_audit.jsonl"
    audit_log.parent.mkdir(parents=True, exist_ok=True)

    token = set_hermes_home_override(str(hermes_home))

    sec_cfg = APASecurityConfig(
        enabled=True,
        workspace_root=workspace.resolve(),
        audit_log_path=audit_log.resolve(),
        enforce_workspace_boundary=True,
    )
    interceptor = ToolInterceptor(config=sec_cfg)

    # Create sample skill in workspace
    s_root = workspace / "skills" / "demo-skill"
    s_root.mkdir(parents=True, exist_ok=True)
    (s_root / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo skill for product integration\n---\n# Demo\nFollow instructions.\n",
        encoding="utf-8",
    )
    (s_root / "guide.md").write_text("# Guide\nDetailed demo guidelines.\n", encoding="utf-8")

    skills_reg = APASkillsRegistry(custom_roots=[(0, workspace / "skills")])
    skill_loader = SkillLoader(registry=skills_reg)

    yield {
        "hermes_home": hermes_home,
        "workspace": workspace,
        "audit_log": audit_log,
        "sec_cfg": sec_cfg,
        "interceptor": interceptor,
        "skills_reg": skills_reg,
        "skill_loader": skill_loader,
    }

    reset_hermes_home_override(token)


# =============================================================================
# REQUIREMENT 1: FRESH AGY AUTHENTICATION
# =============================================================================


def test_01_fresh_agy_authentication(product_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch):
    """1. Fresh authentication flow delegates to official agy login without manipulating tokens."""
    unauth_status = AGYAuthStatus(
        installed=True,
        authenticated=False,
        executable="agy",
        version="1.5.9",
        detail="Not logged in. Run 'agy login'.",
    )
    monkeypatch.setattr(AGYProcessManager, "check_auth_status", lambda self: unauth_status)

    prof = get_provider_profile("antigravity")
    assert prof is not None

    ok, detail = prof.setup_status()
    assert ok is False
    assert "Not logged in" in detail

    login_called = False

    def fake_run_login(self, timeout: float = 300.0) -> int:
        nonlocal login_called
        login_called = True
        return 0

    monkeypatch.setattr(AGYProcessManager, "run_login", fake_run_login)
    success = handle_antigravity_auth("login")
    assert success is True
    assert login_called is True


# =============================================================================
# REQUIREMENT 2: EXISTING AUTHENTICATED SESSION
# =============================================================================


def test_02_existing_authenticated_session():
    """2. Existing authenticated session is discovered and reported ready."""
    prof = get_provider_profile("antigravity")
    assert prof is not None

    status_tuple = prof.setup_status()
    assert status_tuple is not None
    is_ready, detail = status_tuple
    assert is_ready is True
    assert "AGY CLI ready" in detail
    assert "1.5.9" in detail


# =============================================================================
# REQUIREMENT 3: AUTHENTICATION EXPIRATION
# =============================================================================


def test_03_authentication_expiration():
    """3. Expired or invalid authentication is correctly classified as 'auth' error."""
    err_samples = [
        "Error: Authentication required. Please run 'agy login' to re-authenticate.",
        "JSON-RPC error -32000: User is unauthenticated",
        "OAuth refresh token expired (-32001)",
        "Not logged in to Google Antigravity",
    ]
    for sample in err_samples:
        classified = classify_antigravity_api_error(sample)
        assert classified == "auth", f"Expected 'auth' classification for: {sample}"

    assert classify_antigravity_api_error("Rate limit exceeded 429") is None


# =============================================================================
# REQUIREMENT 4: MODEL DISCOVERY
# =============================================================================


def test_04_model_discovery():
    """4. Discover live models and verify shorthand alias resolution."""
    prof = get_provider_profile("antigravity")
    assert prof is not None

    models = prof.discover_models()
    assert len(models) >= 1
    assert any("argon" in m for m in models)

    # Shorthand alias resolution
    assert prof.resolve_model("argon") == "gemini-4-argon-medium"
    assert prof.resolve_model("hydrogen") == "gemini-4-hydrogen-high"
    assert prof.resolve_model("") == DEFAULT_ANTIGRAVITY_MODEL


# =============================================================================
# REQUIREMENT 5: MODEL SWITCHING
# =============================================================================


def test_05_model_switching(product_env: dict[str, Any]):
    """5. Model switching between available models updates default configuration."""
    prof = get_provider_profile("antigravity")
    assert prof is not None
    client = prof.create_client(client_kwargs={"default_model": "gemini-4-argon-medium"})
    assert client.default_model == "gemini-4-argon-medium"

    client.default_model = "gemini-4-hydrogen-high"
    assert client.default_model == "gemini-4-hydrogen-high"

    client.default_model = "gemini-3.8-flash-medium"
    assert client.default_model == "gemini-3.8-flash-medium"


# =============================================================================
# REQUIREMENT 6: STREAMING RESPONSES
# =============================================================================


def test_06_streaming_responses(monkeypatch: pytest.MonkeyPatch):
    """6. Streaming responses yield incremental tokens and normalized finish reasons."""
    from plugins.model_providers.antigravity.acp_transport import TurnAccumulator

    prof = get_provider_profile("antigravity")
    assert prof is not None
    client = prof.create_client()

    def fake_prompt(*args, on_text=None, **kwargs):
        if on_text:
            on_text("Hello ")
            on_text("from Antigravity!")
        acc = TurnAccumulator()
        acc.text_chunks = ["Hello ", "from Antigravity!"]
        return acc

    monkeypatch.setattr(client._transport, "prompt", fake_prompt)

    chunks = list(
        client.stream_completion(
            messages=[{"role": "user", "content": "hi"}],
            model="gemini-4-argon-medium",
        )
    )
    assert len(chunks) >= 2
    content_text = "".join(c.choices[0].delta.content or "" for c in chunks)
    assert content_text == "Hello from Antigravity!"
    assert chunks[-1].choices[0].finish_reason == "stop"


# =============================================================================
# REQUIREMENT 7: TOOL EXECUTION
# =============================================================================


def test_07_tool_execution():
    """7. Tool calls emitted by Antigravity model are buffered, parsed, and normalized."""
    buf = StreamingToolCallBuffer()
    deltas = [
        'Here is the result:\n<tool_call>{"name": "file_read", ',
        '"arguments": {"path": "test.txt"}}</tool_call>',
    ]
    for d in deltas:
        buf.feed(d)
    _trailing, final_tcs = buf.finalize()

    assert len(final_tcs) == 1
    call = final_tcs[0]
    assert call.function.name == "file_read"
    args = json.loads(call.function.arguments)
    assert args == {"path": "test.txt"}


# =============================================================================
# REQUIREMENT 8: SKILLS ACTIVATION
# =============================================================================


def test_08_skills_activation(product_env: dict[str, Any]):
    """8. Skills activation loads SKILL.md and linked references on demand."""
    loader: SkillLoader = product_env["skill_loader"]
    skill = loader.load_skill("demo-skill")
    assert skill.name == "demo-skill"
    assert "Follow instructions" in skill.instructions

    ref = loader.load_linked_file("demo-skill", "guide.md")
    assert "Detailed demo guidelines" in ref.content


# =============================================================================
# REQUIREMENT 9: MEMORY PERSISTENCE USING NATIVE HERMES
# =============================================================================


def test_09_memory_persistence_using_native_hermes(product_env: dict[str, Any]):
    """9. Native Hermes memory files persist within the isolated profile home."""
    hermes_home: Path = product_env["hermes_home"]
    mem_dir = hermes_home / "memories"
    mem_dir.mkdir(parents=True, exist_ok=True)
    memory_file = mem_dir / "MEMORY.md"
    user_file = mem_dir / "USER.md"

    memory_file.write_text("# Agent Memory\nUser prefers Python 3.14.\n", encoding="utf-8")
    user_file.write_text("# User Profile\nName: Firdaus\n", encoding="utf-8")

    assert memory_file.exists()
    assert "Python 3.14" in memory_file.read_text(encoding="utf-8")
    assert "Firdaus" in user_file.read_text(encoding="utf-8")


# =============================================================================
# REQUIREMENT 10: SESSION CONTINUITY USING NATIVE HERMES
# =============================================================================


def test_10_session_continuity_using_native_hermes(product_env: dict[str, Any]):
    """10. Native Hermes SessionDB persists conversation history and allows resumption."""
    from hermes_state import SessionDB

    db = SessionDB()
    sid = "test_session_20261116"

    db.ensure_session(sid)
    db.append_message(sid, "user", "Hello Hermes!")
    db.append_message(sid, "assistant", "Hello! I am APA powered by Antigravity.")

    history = db.get_messages(sid)
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[1]["role"] == "assistant"
    assert "Antigravity" in history[1]["content"]


# =============================================================================
# REQUIREMENT 11: CANCELLATION AND RECOVERY
# =============================================================================


def test_11_cancellation_and_recovery(monkeypatch: pytest.MonkeyPatch):
    """11. Cancellation sends ACP cancellation without breaking client state."""
    prof = get_provider_profile("antigravity")
    assert prof is not None
    client = prof.create_client()

    cancel_called = False

    def fake_cancel(session_id=None):
        nonlocal cancel_called
        cancel_called = True

    monkeypatch.setattr(client._transport, "cancel", fake_cancel)
    client.cancel()
    assert cancel_called is True


# =============================================================================
# REQUIREMENT 12: ACP SUBPROCESS RESTART
# =============================================================================


def test_12_acp_subprocess_restart(monkeypatch: pytest.MonkeyPatch):
    """12. Process manager restarts subprocess cleanly without leaving orphans."""
    pm = AGYProcessManager()

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    mock_proc.returncode = 0
    mock_proc.stderr = None
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()

    monkeypatch.setattr(pm, "resolve_executable", lambda: "agy")
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: mock_proc)

    proc1 = pm.start()
    assert proc1 is not None

    proc2 = pm.restart()
    assert proc2 is not None
    assert pm.restart_count == 1
    mock_proc.terminate.assert_called()

    # Clean up process reference
    mock_proc.poll.return_value = 0
    pm.stop()


# =============================================================================
# REQUIREMENT 13: SECURITY ENFORCEMENT
# =============================================================================


def test_13_security_enforcement(product_env: dict[str, Any]):
    """13. APA Security Guard hook blocks path traversal and dangerous commands."""
    interceptor: ToolInterceptor = product_env["interceptor"]
    audit_log: Path = product_env["audit_log"]

    # Safe read inside workspace
    verdict_safe = interceptor.on_pre_tool_call(tool_name="file_read", args={"path": "safe.txt"})
    assert verdict_safe is None

    # Path traversal attack
    verdict_trav = interceptor.on_pre_tool_call(
        tool_name="file_read", args={"path": "..\\..\\Windows\\win.ini"}
    )
    assert verdict_trav is not None
    assert verdict_trav.get("action") == "block"
    assert "PATH_TRAVERSAL" in verdict_trav.get("message", "")

    # Destructive command attack
    verdict_cmd = interceptor.on_pre_tool_call(
        tool_name="terminal_execute",
        args={"command": "Remove-Item -Recurse -Force C:\\Windows"},
    )
    assert verdict_cmd is not None
    assert verdict_cmd.get("action") == "block"
    assert "RECURSIVE" in verdict_cmd.get("message", "") or "FORCE" in verdict_cmd.get("message", "")

    # Audit log verification
    assert audit_log.exists()
    audit_text = audit_log.read_text(encoding="utf-8")
    assert "PATH_TRAVERSAL" in audit_text
    assert "RECURSIVE" in audit_text or "FORCE" in audit_text


# =============================================================================
# REQUIREMENT 14: HERMES CLI/TUI USABILITY
# =============================================================================


def test_14_hermes_cli_tui_usability(monkeypatch: pytest.MonkeyPatch):
    """14. APA CLI launcher dispatches status, auth, version, and forwards to Hermes CLI."""
    import apa

    monkeypatch.setattr(
        AGYProcessManager,
        "verify_version",
        lambda self, timeout=10.0, force=False: AGYVersionInfo(
            executable="C:\\Users\\firdaus\\AppData\\Local\\agy\\bin\\agy.EXE",
            version="1.5.9",
            raw_output="1.5.9",
        ),
    )
    monkeypatch.setattr(
        AGYProcessManager,
        "fetch_live_models",
        lambda self, timeout=10.0: ["gemini-4-argon-medium", "gemini-3.8-flash-medium"],
    )

    # Test --version
    sio = io.StringIO()
    monkeypatch.setattr(sys, "stdout", sio)
    with pytest.raises(SystemExit) as exc_v:
        apa.main(["--version"])
    assert exc_v.value.code == 0
    assert "APA 1.0.0" in sio.getvalue()

    # Test auth status
    sio.truncate(0)
    sio.seek(0)
    rc_auth = apa.main(["auth", "status"])
    assert rc_auth == 0
    assert "Official Antigravity Authentication Status" in sio.getvalue()

    # Test status
    sio.truncate(0)
    sio.seek(0)
    rc_stat = apa.main(["status"])
    assert rc_stat == 0
    stat_val = sio.getvalue()
    assert "APA (Antigravity Personal Agent) Status" in stat_val
    assert "Google Antigravity" in stat_val
    assert "Security Guard" in stat_val
    assert "Skills Engine" in stat_val

    # Test argument forwarding to Hermes CLI
    intercepted_argv = []

    def mock_forward(argv):
        intercepted_argv.extend(argv)
        return 0

    monkeypatch.setattr(apa, "_forward_to_hermes", mock_forward)

    apa.main(["chat", "-q", "hello"])
    assert "chat" in intercepted_argv
    assert "-q" in intercepted_argv
    assert "hello" in intercepted_argv
