# 07 — Source Code Modification Map

**Project:** APA (Antigravity Personal Agent) — Phase 1 Audit  
**Repository:** `C:\Users\firdaus\APA` (`apa-development`)

---

## 1. Modification Strategy Summary

Because Hermes Agent enforces a strict **narrow-waist core + plugin edge** architecture ([AGENTS.md](file:///C:/Users/firdaus/APA/AGENTS.md#L22-L27), `tests/ci/test_prompt_surface.py`, `scripts/boundary_lint.py`), transforming Hermes into APA requires **almost zero changes to existing Hermes core files**. All Antigravity provider logic and APA Security Guard policies can be delivered as **Category C (New Modules / Plugins)**, with at most 2 optional, surgical **Category B** refinements.

| Category | File Count | Scope Summary |
|---|---|---|
| **Category A — Keep Untouched** | 100% of Core Engine | `run_agent.py`, `agent/*`, `hermes_state*.py`, `model_tools.py`, `tools/*`, `toolsets.py`, `gateway/*`, `cron/*` |
| **Category B — Modify (Optional / Minimal)** | 2 files | `agent/acp_client.py` (support multi-arg `binary_args` like `["agy", "acp", "--stdio"]`), `hermes_cli/default_config.yaml` (optional default provider comment/preset) |
| **Category C — New Modules (APA Plugins)** | 2 plugins (7 files) | `plugins/model-providers/antigravity/*` (Antigravity Provider Plugin) and `plugins/apa-security/*` (APA Security Guard Plugin) |
| **Category D — Optional Migration (Legacy APA)** | 4 modules | Ported from `D:\Antigravity-Hermes` into the two Category C plugins (`security/boundary.py`, `security/policy.py`, `security/backup.py`, `antigravity/timeout.py`) |

---

## 2. Category A — Keep Untouched (Core Hermes Engine)

These load-bearing files already expose the exact hooks and interfaces APA needs and **must not be modified**:

| File Path | Main Classes / Functions | Current Responsibility | Why No Modification Is Needed | Risk if Modified |
|---|---|---|---|---|
| [run_agent.py](file:///C:/Users/firdaus/APA/run_agent.py) | `AIAgent` | Top-level agent runner facade and mixin composition. | Already delegates client creation to `ClientLifecycleMixin` and tool calls to `model_tools.handle_function_call()`. | **High** (Facade size/complexity ratchet `HX0xx`) |
| [agent/conversation_loop.py](file:///C:/Users/firdaus/APA/agent/conversation_loop.py) | `run_conversation_loop()` | Multi-turn conversation execution, streaming, and tool dispatch. | Already supports `pre_llm_call`, `post_llm_call`, `pre_api_request`, and custom provider clients. | **High** (Core turn loop invariants) |
| [agent/prompt_builder.py](file:///C:/Users/firdaus/APA/agent/prompt_builder.py) | `build_system_prompt()` | System prompt construction and caching. | Modifying stock prompt breaks `tests/ci/test_prompt_surface.py`. Plugins inject prompt sections via `ctx.register_system_prompt_section()`. | **High** (Breaks prompt surface CI snapshot) |
| [hermes_state.py](file:///C:/Users/firdaus/APA/hermes_state.py) | `SessionDB` | SQLite WAL session and FTS5 message storage. | Fully handles session persistence, lineage compression, and FTS5 search on Windows. | **High** (SQLite locking & WAL invariants) |
| [agent/memory_manager.py](file:///C:/Users/firdaus/APA/agent/memory_manager.py) | `MemoryManager` | Orchestrates built-in `MEMORY.md`/`USER.md` and external `MemoryProvider` plugins. | Built-in memory + `session_search` + pluggable `MemoryProvider` cover all APA memory requirements with automatic secret redaction. | **Medium** |
| [model_tools.py](file:///C:/Users/firdaus/APA/model_tools.py) | `handle_function_call()`, `get_tool_definitions()` | Tool discovery, schema sanitization, and `pre_tool_call` / `post_tool_call` hook dispatch. | Line 808 (`_dispatch_pre_tool_call_hooks`) already blocks any tool call when a plugin returns `{"block": True, "reason": "..."}`. | **High** |
| [tools/registry.py](file:///C:/Users/firdaus/APA/tools/registry.py) | `ToolRegistry`, `discover_builtin_tools()` | Central registry for built-in and plugin tools. | Plugins register tools via `ctx.register_tool()` without editing `tools/`. | **Medium** |
| [tools/approval.py](file:///C:/Users/firdaus/APA/tools/approval.py) | `check_all_command_guards()`, `request_tool_approval()` | Dangerous command detection, human approval gates, and smart guardian breaker. | Complements APA Security Guard; plugins can veto before `approval.py` via `pre_tool_call` and observe via `pre_approval_request`. | **High** |
| [providers/base.py](file:///C:/Users/firdaus/APA/providers/base.py) & [providers/__init__.py](file:///C:/Users/firdaus/APA/providers/__init__.py) | `ProviderProfile`, `register_provider()`, `_discover_providers()` | Declarative provider contract and plugin discovery (`plugins/model-providers/<name>/`). | Already supports `auth_type="external_process"`, `create_client()`, `fetch_models()`, `auth_handler`, and `classify_api_error()`. | **Medium** |
| [hermes_cli/auth.py](file:///C:/Users/firdaus/APA/hermes_cli/auth.py) & [hermes_cli/runtime_provider.py](file:///C:/Users/firdaus/APA/hermes_cli/runtime_provider.py) | `resolve_runtime_provider()`, `PROVIDER_REGISTRY` | Auth resolution and runtime provider selection. | `hermes_cli/auth_plugin_providers.py` automatically bridges every `ProviderProfile` plugin into `auth.py` and `runtime_provider.py`. | **High** |

---

## 3. Category B — Modify (Minimal / Optional Refinements)

*Note: Even these 2 modifications are optional if `plugins/model-providers/antigravity/client.py` implements its own `create_client()` method. They are listed here in case APA elects to reuse Hermes's built-in `agent/acp_client.py::ACPClient` directly.*

| File Path | Main Classes / Functions | Current Responsibility | Reason for Modification | Proposed Modification | Dependencies | Risk Level |
|---|---|---|---|---|---|---|
| [agent/acp_client.py](file:///C:/Users/firdaus/APA/agent/acp_client.py#L127-L205) | `ACPClient.__init__()`, `ACPClient._ensure_started()` | Spawns an external ACP binary over stdio and bridges it to `chat.completions.create()`. | Line 188 calls `acp.spawn_agent_process(client, self._binary)` with zero CLI args (`codex-acp` convention), whereas `agy.exe` requires `["agy", "acp", "--stdio"]` and `windows_hide_flags()` on Windows. | Allow `binary_args: Sequence[str] = ()` (or split `self._binary` / detect `agy` to pass `"acp", "--stdio"`) in `acp.spawn_agent_process(client, binary, *binary_args, ...)` and pass `windows_hide_flags()` on `win32`. | `acp`, `hermes_cli._subprocess_compat` | **Low** |
| [agent/acp_openai_bridge.py](file:///C:/Users/firdaus/APA/agent/acp_openai_bridge.py#L64-L140) | `format_incremental_turn()`, `parse_tool_calls_from_text()` | Formats OpenAI messages/tools for ACP prompts and parses `<tool_call>` XML blocks. | When Hermes compresses context mid-session, `messages` shrinks, which should trigger a fresh ACP session so the subprocess doesn't retain stale uncompressed turns. | Track message count/prefix digest in `ACPClient._prepare_prompt()` and reset `_session_id` if `len(messages)` decreases (compression event). | `openai.types.chat` | **Low** |

---

## 4. Category C — New Modules (APA-Specific Plugins)

All new APA functionality lives inside Hermes's official plugin directories (`plugins/model-providers/antigravity/` and `plugins/apa-security/`):

| Proposed File Path | Main Classes / Functions | Responsibility | Dependencies | Risk Level |
|---|---|---|---|---|
| `plugins/model-providers/antigravity/plugin.yaml` | Manifest (`kind: model-provider`) | Declares the `antigravity` model provider plugin metadata and schema. | `hermes_cli.plugins_manifest` | **Low** |
| `plugins/model-providers/antigravity/__init__.py` | `PROFILE` (`ProviderProfile`), `register_provider(PROFILE)`, `antigravity_auth_handler()`, `antigravity_setup_status()`, `antigravity_fetch_models()` | Registers the `antigravity` provider (`auth_type="external_process"`, `default_model="gemini-4-argon-medium"`), discovers `agy.exe` on Windows/PATH, runs `agy login` / `agy logout`, and parses `agy models` for `/model`. | `providers.base`, `providers`, `shutil`, `subprocess` | **Low** |
| `plugins/model-providers/antigravity/client.py` | `AntigravityClient`, `AntigravityChatCompletions` | OpenAI-compatible client returned by `PROFILE.create_client()`. Connects to `agy.exe acp --stdio` (with `agy -p --output-format json` fallback for one-shot `aux_task` calls), manages session reuse, maps `reasoning_effort` to `thought_level` / `--effort`, and bridges Hermes tool calls via `agent/acp_openai_bridge.py`. | `agent.acp_openai_bridge`, `hermes_cli._subprocess_compat` | **Low** |
| `plugins/apa-security/plugin.yaml` | Manifest (`name: apa-security`) | Declares the APA Security Guard plugin, config schema (`strict_boundary`, `auto_backup_on_delete`, `audit_log_file`), and hooks. | `hermes_cli.plugins` | **Low** |
| `plugins/apa-security/__init__.py` | `register(ctx: PluginContext)`, `on_pre_tool_call()`, `on_post_tool_call()`, `apa_security_command()` | Registers `pre_tool_call` (enforces workspace boundary, UNC/null-byte rejection, destructive command policy, pre-delete backup) and `post_tool_call` (writes structured JSONL audit log), plus `/security` slash command. | `hermes_cli.plugins_context`, `agent.redact` | **Low** |
| `plugins/apa-security/guard.py` | `APASecurityGuard`, `BoundaryValidator`, `PolicyChecker`, `BackupManager`, `AuditLogger` | Consolidated security enforcement engine adapted from Legacy APA (`D:\Antigravity-Hermes\security\`). | `pathlib`, `re`, `json`, `shutil` | **Low** |

---

## 5. Category D — Optional Migration from Legacy APA (`D:\Antigravity-Hermes`)

| Legacy File Path | Legacy Classes / Functions | Target APA Location | Migration Action & Rationale | Risk Level |
|---|---|---|---|---|
| [D:/Antigravity-Hermes/security/boundary.py](file:///D:/Antigravity-Hermes/security/boundary.py) & [policy.py](file:///D:/Antigravity-Hermes/security/policy.py) | `BoundaryValidator`, `PolicyChecker`, `ActionSeverity` | `plugins/apa-security/guard.py` | **Adapt:** Port strict workspace containment, UNC/null-byte checks, and destructive command regexes into the `pre_tool_call` plugin hook. | **Low** |
| [D:/Antigravity-Hermes/security/audit.py](file:///D:/Antigravity-Hermes/security/audit.py) & [backup.py](file:///D:/Antigravity-Hermes/security/backup.py) | `AuditLogger`, `BackupManager` | `plugins/apa-security/guard.py` | **Adapt:** Port JSONL audit trail and pre-deletion snapshot backups into `pre_tool_call` / `post_tool_call` hooks, storing logs/backups under `get_hermes_home()`. | **Low** |
| [D:/Antigravity-Hermes/antigravity/acp_client.py](file:///D:/Antigravity-Hermes/antigravity/acp_client.py) & [timeout.py](file:///D:/Antigravity-Hermes/antigravity/timeout.py) | `AcpClient`, `classify_prompt_complexity()`, `resolve_prompt_timeout()` | `plugins/model-providers/antigravity/client.py` | **Adapt:** Reuse the NDJSON stdio `agy acp --stdio` transport, `session/set_config_option` (`model`, `thought_level`), and adaptive timeout classifier behind Hermes's `chat.completions.create()` interface. | **Low** |
| [D:/Antigravity-Hermes/antigravity/fake_adapter.py](file:///D:/Antigravity-Hermes/antigravity/fake_adapter.py) & `tests/` | `FakeAntigravityAdapter`, security & ACP unit tests | `tests/plugins/test_antigravity_provider.py`, `tests/plugins/test_apa_security.py` | **Adapt:** Convert legacy unit tests into Hermes behaviour-contract tests (`scripts/run_tests.sh`). | **Low** |
