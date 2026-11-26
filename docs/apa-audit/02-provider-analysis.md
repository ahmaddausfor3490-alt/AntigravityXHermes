# 02 — Provider System Reverse Engineering

**Project:** APA (Antigravity Personal Agent) — Phase 1 Audit  
**Repository:** `C:\Users\firdaus\APA` (`apa-development`)

---

## 1. Provider Architecture Overview

Hermes routes all LLM interactions through a declarative **Provider Profile** architecture (`providers/base.py::ProviderProfile`) paired with a runtime credential/transport resolver (`hermes_cli/runtime_provider.py`) and an OpenAI-SDK-shaped client lifecycle (`agent/client_lifecycle.py`).

```mermaid
flowchart LR
    PPlugin["plugins/model-providers/<name>/\nor providers/<name>.py"] -->|register_provider()| Reg["providers/__init__.py\n(_REGISTRY + _HOME_LAYERS)"]
    Reg -->|mirror at import| AuthReg["hermes_cli/auth.py\n(PROVIDER_REGISTRY)"]
    AuthReg -->|resolve_runtime_provider()| Runtime["hermes_cli/runtime_provider.py\n(api_mode, base_url, api_key)"]
    Runtime -->|create_openai_client()| Helper["agent/agent_runtime_helpers.py"]
    Helper -->|profile.create_client() or OpenAI()| Client["OpenAI / Custom Client\n(chat.completions.create)"]
    Client -->|ChatCompletionsTransport| Loop["agent/conversation_loop.py"]
```

---

## 2. 12-Point Provider Subsystem Analysis

### 2.1 Provider Registration
- **Interface:** [providers/base.py](file:///C:/Users/firdaus/APA/providers/base.py#L94-L530) defines `@dataclass(frozen=True) class ProviderProfile`.
- **Registration API:** [providers/__init__.py](file:///C:/Users/firdaus/APA/providers/__init__.py#L317-L370) exposes `register_provider(profile: ProviderProfile, *, replace: bool = False)`.
- **Auto-Mirroring:** [hermes_cli/auth_plugin_providers.py](file:///C:/Users/firdaus/APA/hermes_cli/auth_plugin_providers.py) bridges every registered `ProviderProfile` into `hermes_cli.auth.PROVIDER_REGISTRY` (`ProviderConfig`), `CANONICAL_PROVIDERS`, `PROVIDER_LABELS`, and `hermes doctor` without manual edits to `auth.py`.

### 2.2 Provider Discovery
- **Discovery Order ([providers/__init__.py](file:///C:/Users/firdaus/APA/providers/__init__.py#L460-L600)):**
  1. Built-in profiles in `providers/*.py` (`openrouter`, `nous`, `openai_codex`, `anthropic`, `kimi`, `glm`, `minimax`, `openni`, `copilot`, etc.).
  2. Bundled model-provider plugins in `plugins/model-providers/<name>/__init__.py` (e.g., [plugins/model-providers/gemini/__init__.py](file:///C:/Users/firdaus/APA/plugins/model-providers/gemini/__init__.py)).
  3. Profile-scoped user plugins in `get_hermes_home() / "plugins" / "model-providers" / "<name>"` and `get_hermes_home() / "plugins" / "<name>"` where `plugin.yaml` declares `kind: model-provider` (`_scan_home_layer()`).

### 2.3 Provider Resolution
- **Entry Point:** `resolve_runtime_provider(requested=..., explicit_api_key=..., explicit_base_url=...)` in [hermes_cli/runtime_provider.py](file:///C:/Users/firdaus/APA/hermes_cli/runtime_provider.py#L1168-L1305).
- **Resolution Ladder (`_ladder_rungs`):**
  1. Explicit CLI/caller `--provider` argument (`_normalize_requested_provider()`).
  2. `model.provider` in `get_hermes_home() / "config.yaml"` or `HERMES_INFERENCE_PROVIDER` env var.
  3. Active OAuth/external provider in `get_hermes_home() / "auth.json"`.
  4. Configured API key env vars across registered providers.
  5. OpenRouter fallback.
- **External Process Providers:** [hermes_cli/runtime_provider_backends.py](file:///C:/Users/firdaus/APA/hermes_cli/runtime_provider_backends.py#L225-L263) (`_is_external_process_provider()`, `_resolve_external_process_runtime()`) handles profiles with `auth_type="external_process"`, resolving binary path via `resolve_external_process_provider_credentials()` in [hermes_cli/auth.py](file:///C:/Users/firdaus/APA/hermes_cli/auth.py#L1614-L1646).

### 2.4 Model Selection
- **Catalog & Picker:** `ProviderProfile.models` (static tuple), `ProviderProfile.discover_models(base_url, api_key)` (sync probe), and `ProviderProfile.fetch_models(api_key, base_url)` (live catalog query for `hermes model` picker, [providers/base.py](file:///C:/Users/firdaus/APA/providers/base.py#L378-L410)).
- **Default Model:** `ProviderProfile.default_model` is written to `config.yaml` (`model.default`, `model.provider`, `model.base_url`) by `_update_config_for_provider()` in [hermes_cli/auth.py](file:///C:/Users/firdaus/APA/hermes_cli/auth.py#L2240-L2310).

### 2.5 Model Configuration & Capabilities
- **Metadata & Context Lengths:** [agent/model_metadata.py](file:///C:/Users/firdaus/APA/agent/model_metadata.py) resolves context windows and max completion tokens per model/provider.
- **Reasoning / Thinking Effort:** [agent/reasoning_params.py](file:///C:/Users/firdaus/APA/agent/reasoning_params.py) (`ReasoningParamsMixin`) maps `reasoning_effort` (`low`, `medium`, `high`, `xhigh`, `max`) to provider-specific request fields or `extra_body`.
- **Temperature Sentinel:** `ProviderProfile.default_temperature` (or `OMIT_TEMPERATURE` in [providers/base.py](file:///C:/Users/firdaus/APA/providers/base.py#L51)) controls whether temperature is sent or stripped for reasoning models.

### 2.6 Request Construction
- **Client Factory:** [agent/agent_runtime_helpers.py](file:///C:/Users/firdaus/APA/agent/agent_runtime_helpers.py#L1988-L2025) (`create_openai_client()`) checks `_provider_supplied_client(agent, client_kwargs)` at L1993, which invokes `ProviderProfile.create_client(**client_kwargs)`.
- **API Modes ([hermes_cli/runtime_provider.py](file:///C:/Users/firdaus/APA/hermes_cli/runtime_provider.py#L42-L54)):**
  - `chat_completions` (default OpenAI Chat Completions shape)
  - `codex_responses` (OpenAI Responses API via `agent/codex_responses_adapter.py`)
  - `anthropic_messages` (Anthropic native Messages adapter via `agent/anthropic_adapter.py`)
  - `acp_stdio` (External ACP process client via `agent/acp_client.py::ACPClient`)

### 2.7 Response Processing
- **Normalization:** All transports normalize responses into an OpenAI-compatible `ChatCompletion` / `ChatCompletionMessage` object (`choices[0].message.content`, `choices[0].message.tool_calls`, `reasoning_content`, `usage`).
- **Custom Client Pattern:** [plugins/model-providers/gemini/client.py](file:///C:/Users/firdaus/APA/plugins/model-providers/gemini/client.py#L1-L120) (`GeminiClient`) demonstrates how a custom `create_client()` returns a lightweight object exposing `.chat.completions.create(**kwargs)` and `.close()`.

### 2.8 Streaming
- **Transport:** [agent/transports/chat_completions.py](file:///C:/Users/firdaus/APA/agent/transports/chat_completions.py#L31-L85) (`ChatCompletionsTransport.stream()`) calls `client.chat.completions.create(**api_kwargs)` with `stream=True` and returns a `StreamHandle(events=raw_stream, abort=...)`.
- **Delivery:** [agent/stream_delivery.py](file:///C:/Users/firdaus/APA/agent/stream_delivery.py) (`StreamDeliveryMixin`) consumes chunks, emits `on_stream_delta` plugin hooks, and accumulates text, reasoning deltas, and streaming tool-call fragments.

### 2.9 Tool Calling
- **Schema Passing:** `api_kwargs["tools"]` contains OpenAI-format function schemas (`{"type": "function", "function": {"name", "description", "parameters"}}`).
- **Tool Call ID Sanitization:** [agent/message_sanitization.py](file:///C:/Users/firdaus/APA/agent/message_sanitization.py) (`uniquify_tool_call_ids`, `coalesce_tool_call_id`) ensures strict tool-call/tool-result pairing across turns.
- **ACP Tool Bridge:** For `acp_stdio` providers (`agent/acp_client.py`), [agent/acp_openai_bridge.py](file:///C:/Users/firdaus/APA/agent/acp_openai_bridge.py#L22-L120) serializes `tools[]` into an XML `<tool_call>` system instruction and parses `<tool_call>{...}</tool_call>` blocks from the response into synthetic `ChatCompletionMessageToolCall` objects.

### 2.10 Retry Mechanisms & Failover
- **Credential Pool & Fallback:** [agent/client_lifecycle.py](file:///C:/Users/firdaus/APA/agent/client_lifecycle.py) (`ClientLifecycleMixin`) and [hermes_cli/runtime_provider.py](file:///C:/Users/firdaus/APA/hermes_cli/runtime_provider.py#L1308-L1375) (`resolve_runtime_with_fallback()`) support multi-credential rotation (`CredentialPool`) and ordered fallback provider chains (`fallback_providers` in `config.yaml`).

### 2.11 Timeout Handling
- **Configuration:** [hermes_cli/timeouts.py](file:///C:/Users/firdaus/APA/hermes_cli/timeouts.py) provides `get_provider_request_timeout()` and `get_provider_stale_timeout()`.
- **Process Transport Stale Guard:** [agent/process_transport.py](file:///C:/Users/firdaus/APA/agent/process_transport.py) (`implicit_non_stream_stale_timeout`) and `ACPClient._prompt_timeout_s` (default `1800.0`s, configurable via `HERMES_ACP_PROMPT_TIMEOUT`) prevent indefinite hangs on subprocess transports, while `client.cancel()` ([agent/client_lifecycle.py](file:///C:/Users/firdaus/APA/agent/client_lifecycle.py#L510-L535)) aborts in-flight subprocess turns on Ctrl+C / `/stop`.

### 2.12 Error Handling & Classification
- **Classifier:** [agent/error_classifier.py](file:///C:/Users/firdaus/APA/agent/error_classifier.py) (`classify_api_error()`) categorizes failures into `FailoverReason` (`rate_limit`, `auth`, `billing`, `context_overflow`, `timeout`, `server_error`, etc.).
- **Provider Override Hook:** `ProviderProfile.classify_api_error(status_code, error_code, error_message)` ([providers/base.py](file:///C:/Users/firdaus/APA/providers/base.py#L521-L529)) and the `transform_api_error_classification` plugin hook allow provider-specific error mapping (e.g., mapping `AcpAuthRequiredError` to `auth` failure with actionable `agy login` guidance).

---

## 3. Extension Points & Minimum Modification Analysis for APA

Hermes provides **two** zero-core-edit extension mechanisms for introducing the APA Antigravity adapter:

| Mechanism | How It Works | Core Files Modified | Pros / Cons |
|---|---|---|---|
| **Option 1 (Recommended): Model-Provider Plugin with Custom `create_client()`** | Create `plugins/model-providers/antigravity/` (or `$HERMES_HOME/plugins/model-providers/antigravity/`) registering `ProviderProfile(id="antigravity", auth_type="external_process", ...)` with a custom `create_client()` that wraps `agy.exe` (`agy acp --stdio` or `agy -p --output-format stream-json`). | **0 core files modified** | Full control over `agy.exe` flags, session reuse, `agy models` discovery, and tool bridging without touching any core Hermes file. |
| **Option 2: Built-in `acp_stdio` External Process Profile** | Register `ProviderProfile(id="antigravity", auth_type="external_process", api_mode="acp_stdio", binary_env="HERMES_ANTIGRAVITY_BIN", default_binary="agy")` and let Hermes use `agent/acp_client.py::ACPClient`. | **0–1 file** (`agent/acp_client.py` line 188 spawns `[self._binary]` without `"acp"` subcommand unless configured via wrapper/args) | `ACPClient._ensure_started()` currently runs `[self._binary]` bare (`codex-acp` / `claude-code-acp` convention), whereas `agy.exe` requires `["agy", "acp", "--stdio"]`. |

> [!IMPORTANT]
> **Minimum Modification Verdict:** Using **Option 1** (`plugins/model-providers/antigravity/` implementing `ProviderProfile` + `create_client()`) requires **zero modifications** to existing Hermes core files while automatically integrating with `hermes model`, `hermes auth`, `hermes doctor`, and `AIAgent`.
