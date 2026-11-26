# 06 — Antigravity Integration Feasibility Analysis

**Project:** APA (Antigravity Personal Agent) — Phase 1 Audit  
**Repository:** `C:\Users\firdaus\APA` (`apa-development`)  
**Verified Host Binary:** `C:\Users\firdaus\AppData\Local\agy\bin\agy.exe` (`version 1.5.9`)

---

## 1. Evaluation of Integration Options

### Option A — Official AGY CLI (`agy -p` / `--print` Mode)

Verified directly against installed `agy.exe` `1.5.9` (`agy --help`, `agy models`):
- **Non-interactive execution:** Verified (`-p` / `--print`: *"Print response and exit (non-interactive)"*).
- **Prompt submission:** Verified (`agy -p [query]` or stdin with `--input-format text|stream-json`).
- **Structured output:** Verified (`--output-format text|json|stream-json` and `--json-schema` for validated JSON responses). *(Note: The older bundled skill `optional-skills/autonomous-ai-agents/antigravity-cli/SKILL.md` v0.2.0 stated `--output-format json` was unsupported; live verification on `agy.exe` v1.5.9 proves `text`, `json`, and `stream-json` are now officially supported.)*
- **Streaming:** Verified (`--output-format stream-json` emits real-time NDJSON events; `--include-partial-messages` includes partial chunks).
- **Persistent sessions:** Verified (`--conversation <id>` resumes a specific conversation ID; `-c` / `--continue` resumes the most recent conversation).
- **Model & Effort control:** Verified (`-m` / `--model <id>` and `--effort low|medium|high|xhigh|max`; `agy models` lists 18 live models across Gemini 4 Argon/Hydrogen, Gemini 3.8 Flash, Claude Opus/Sonnet 5.5, and GPT-OSS 120B).
- **Tool/Sandbox control:** Verified (`--sandbox` enables terminal restrictions; `--dangerously-skip-permissions` auto-approves internal tools; `--print-timeout` bounds execution time).
- **Windows compatibility:** Verified native Windows x64 binary (`agy.exe`).

### Option B — Officially Supported Python SDK (`google-antigravity`)

Evaluated from official documentation (`antigravity-guide/references/sdk.md`):
- **Architecture:** `from google.antigravity import Agent, LocalAgentConfig, CapabilitiesConfig` wraps the compiled Antigravity runtime binary over a local gRPC/IPC bridge.
- **Capabilities:** Supports `await agent.chat(...)`, `response.thoughts`, `response.tool_calls`, and custom `@agent.tool` Python function registration.
- **Availability Verification:** Not installed in the current workspace or public PyPI without internal Google artifact registry access (`google-antigravity` package is distributed separately from the standalone `agy.exe` CLI installer). Furthermore, the SDK executes its own internal agent loop rather than acting as a drop-in OpenAI-compatible model transport for Hermes's `AIAgent` loop.

### Option C — Agent Client Protocol (`agy acp --stdio`)

Verified directly against installed `agy.exe acp --help` and Legacy APA (`D:\Antigravity-Hermes\antigravity\acp_client.py`):
- **Official support:** Verified (`agy.exe acp --stdio` is a built-in first-class subcommand of `agy.exe` v1.5.9).
- **Authentication:** Uses the exact same official Google OAuth store as `agy login` (and supports `agy acp --login`).
- **Session management:** Full stateful JSON-RPC 2.0 session lifecycle (`initialize`, `session/new`, `session/load`, `session/list`, `session/set_config_option`, `session/prompt`, `session/cancel`).
- **Streaming:** Emits real-time `session/update` notifications (`agent_message_chunk`, `agent_thought_chunk`, `tool_call`, `tool_call_update`).
- **Hermes Compatibility:** Hermes already ships an OpenAI-compatible ACP bridge (`agent/acp_client.py::ACPClient` + `agent/acp_openai_bridge.py`) and Legacy APA already has a battle-tested Windows `AcpClient` (`D:\Antigravity-Hermes\antigravity\acp_client.py`).

---

## 2. Verified vs. Assumed Capabilities Matrix

| Capability | Status | Evidence Source |
|---|---|---|
| `agy.exe` v1.5.9 installed & authenticated on Windows | **Verified** | `C:\Users\firdaus\AppData\Local\agy\bin\agy.exe --version` → `1.5.9` |
| `agy models` dynamic catalog discovery (18 models) | **Verified** | Live execution of `agy models` returned 18 model IDs + display names |
| `agy -p --output-format text\|json\|stream-json` | **Verified** | Live `agy help` flag table on `agy.exe` v1.5.9 |
| `agy acp --stdio` JSON-RPC 2.0 server | **Verified** | Live `agy acp --help` + Legacy APA `antigravity/acp_client.py` implementation |
| `session/set_config_option` (`model`, `thought_level`) | **Verified** | Verified in Legacy APA `antigravity/adapter.py` & `acp_client.py` |
| Native OpenAI-format `tools[]` parameter in `agy acp` | **Verified Absent (Bridged)** | ACP `session/prompt` takes text/content blocks, not OpenAI `tools[]` schemas; requires Hermes's `agent/acp_openai_bridge.py` (`<tool_call>` XML bridge) so Hermes owns tool execution. |
| `google-antigravity` Python SDK availability on host | **Unverified / Unavailable** | Documented in `antigravity-guide/references/sdk.md`, but package is not present in local Python environment. |

---

## 3. Comprehensive Comparison Table

| Criteria | AGY CLI (`agy -p --output-format stream-json`) | Official SDK (`google-antigravity`) | ACP (`agy acp --stdio`) |
|---|---|---|---|
| **Official support** | **Verified** — First-class CLI mode in `agy.exe` v1.5.9 | **Documented** — Not installed in local Python env | **Verified** — First-class `agy.exe acp --stdio` server |
| **Authentication** | Official `agy login` Google OAuth (zero token exposure) | Inherits local AGY auth | Official `agy login` / `agy acp --login` (zero token exposure) |
| **Streaming** | NDJSON stdout (`--output-format stream-json`) | Async generator (`await agent.chat()`) | Real-time JSON-RPC `session/update` notifications |
| **Session management** | Per-process resume via `--conversation <id>` | In-memory `Agent` object | Persistent process with `session/new`, `session/load`, `session/list` |
| **Tool calling** | Internal to AGY unless bridged via prompt XML/JSON schema | SDK owns tool loop (`@agent.tool`), conflicts with Hermes `AIAgent` | Bridged cleanly to Hermes `handle_function_call` via `acp_openai_bridge.py` + permission denial for AGY-native tools |
| **Windows support** | **High** — Native `agy.exe`, one process per turn | **Medium** — Requires matching wheel/runtime binary | **High** — Native `agy.exe acp --stdio` with `windows_hide_flags()` |
| **Reliability** | **High** — Stateless per-turn process; immune to long-lived pipe stalls | **Low** (for Hermes) — Uninstalled & architectural mismatch | **High** — Persistent low-latency stdio with automatic reconnect & `-p` fallback |
| **Maintenance** | **Low** — Standard CLI flags | **High** — Tight coupling to SDK version | **Low** — Standardized ACP JSON-RPC 2.0 protocol |

---

## 4. Recommended Integration Method

### Primary Recommendation: **Hybrid ACP-First Model-Provider Plugin (`agy acp --stdio` Primary + `agy -p --output-format stream-json` Fallback)**

1. **Primary Transport (`agy acp --stdio`):**
   - Implement `plugins/model-providers/antigravity/` registering `ProviderProfile(id="antigravity", auth_type="external_process", ...)`.
   - `create_client()` returns `AntigravityACPClient`, which spawns `["agy", "acp", "--stdio"]` once per session using `windows_hide_flags()`.
   - Uses `session/new` + `session/set_config_option` (`model`, `thought_level`) + `session/prompt` for sub-second turn latency and real-time `agent_message_chunk` / `agent_thought_chunk` streaming.
   - Uses Hermes's `agent/acp_openai_bridge.py` (`format_tool_definitions` + `parse_tool_calls_from_text`) while denying `agy`-native tool permissions so **Hermes Agent Engine** executes all tools through `model_tools.handle_function_call()` and **APA Security Guard**.
2. **Resilient Fallback (`agy -p --output-format stream-json`):**
   - For single-shot auxiliary calls (`aux_task`: session titling, context compression, smart approval review) or if the persistent ACP stdio pipe ever wedges, automatically fall back to stateless `agy -p --model <model> --output-format json` subprocess invocation.
3. **Dynamic Model Catalog (`agy models`):**
   - Implement `ProviderProfile.fetch_models()` and `discover_models()` by parsing `agy models` stdout (`model_id\tdisplay_name`), keeping Hermes's `/model` picker synchronized with the user's live Antigravity tier.
