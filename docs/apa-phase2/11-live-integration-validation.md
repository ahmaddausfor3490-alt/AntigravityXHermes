# APA Phase 2.11 — Live Antigravity Integration Validation

- **Project**: APA (Antigravity Personal Agent)
- **Repository**: `C:\Users\firdaus\APA`
- **Branch**: `apa-development`
- **Validation Date**: 2026-11-15
- **Target Provider**: `plugins/model-providers/antigravity/` (`provider_id = "antigravity"`)

---

## 1. Test Environment

| Component | Value |
|---|---|
| **OS / Platform** | Windows (`win32`, Windows 10/11 x64) |
| **Shell** | PowerShell |
| **Hermes Runtime Python** | `3.14.9` (`%LOCALAPPDATA%\hermes\tools\python-3.14.9+20261021-win32-x64\python.exe`) |
| **AGY CLI Executable** | `C:\Users\firdaus\AppData\Local\agy\bin\agy.EXE` |
| **AGY CLI Version** | `1.5.9` |
| **Primary Transport** | `agy acp --stdio` (JSON-RPC 2.0 over stdio, protocolVersion `1`) |
| **Fallback Transport** | `agy --output-format json --model <model> -p <prompt>` |
| **Live Test Model** | `gemini-3.8-flash-low` (plus catalog verification of all 18/23 models) |
| **Core Files Modified** | `0` (strictly zero Hermes core edits) |

---

## 2. Summary of Live Validation Results

| Test # | Test Name | Status | Latency / Metric | Notes |
|---|---|---|---|---|
| **TEST 1** | Authentication | **PASS** | `18` CLI models / `23` ACP models | Official Google OAuth valid; live ACP requests authorized. |
| **TEST 2** | Provider Resolution | **PASS** | `< 10 ms` | `get_provider_profile("antigravity")` + aliases (`agy`, `google-antigravity`) resolve to `AntigravityProfile` and `AntigravityClient(ACPClient)`. |
| **TEST 3** | Live ACP Connection | **PASS** | `2,292 ms` | `initialize`, `session/new`, and `session/set_config_option` (`model`) succeeded over persistent `agy acp --stdio`. |
| **TEST 4** | Live Model Conversation | **PASS** | `4,451 ms` | Model responded accurately identifying itself as Antigravity on Gemini 3.8 Flash. |
| **TEST 5** | Streaming | **PASS** | `10` chunks (`4.84s`–`6.55s`) | Incremental text delivery verified; zero duplicated chunks; zero leaked `<tool_call>` tags; `finish_reason="stop"`. |
| **TEST 6** | Tool Calling | **PASS** | 2 turns (`125 * 48 = 6,000`) | Model emitted `<tool_call>` for `calculator(a=125, b=48, operation="multiply")`; Hermes executed tool (`6000`); model returned final answer `'125 * 48 = 6,000.'`. Zero AGY internal tool bypasses. |
| **TEST 7** | Session Persistence | **PASS** | 2 turns on `a11225b9-...` | Same ACP `sessionId` reused across turns; model recalled `"ORBITAL-742"` from Turn 1. |
| **TEST 8** | Cancellation | **PASS** | Cancelled after `2` chunks | Early stream closure sent `session/cancel`; process remained healthy during session and terminated cleanly (`is_running() == False`) on `client.close()` with zero orphans. |
| **TEST 9** | Fallback | **PASS** | 1-shot JSON fallback | Controlled ACP failure detected; `agy --output-format json --model gemini-3.8-flash-low -p ...` returned `'FALLBACK_OK\n'` and preserved conversation snapshot (`len=2`). |
| **TEST 10** | Regression | **PASS** | `29 passed in 12.4s` | All Antigravity, plugin discovery, ACP client, streaming, `boundary_lint`, `ruff`, and `code_health` checks pass (`0` blocking, `0` advisory). |

---

## 3. Detailed Test Execution Records

### TEST 1 — Authentication
- **Status**: **PASS**
- **Execution Output**:
  ```text
  Executable: C:\Users\firdaus\AppData\Local\agy\bin\agy.EXE
  Version: 1.5.9
  AuthStatus(installed, authenticated, models_count): True True 18
  ```
- **Verification**: Confirmed via `AGYProcessManager().verify_version()`, `AGYProcessManager().check_auth_status()`, live `agy models` enumeration (`18` models), and live authorized ACP session creation & prompt execution (`session/new` + `session/prompt`). No internal credential files or SQLite databases were accessed.

---

### TEST 2 — Provider Resolution
- **Status**: **PASS**
- **Execution Output**:
  ```text
  Profile found: True Name: antigravity AuthType: external_process
  Client type: AntigravityClient Is AntigravityClient: True Is ACPClient: True
  ```
- **Verification**: `get_provider_profile("antigravity")`, `get_provider_profile("agy")`, and `get_provider_profile("google-antigravity")` all return the registered `AntigravityProfile` instance, and `profile.create_client()` returns `AntigravityClient` (an `ACPClient` subclass with `HERMES_SKIP_TRANSPORT_WRAP = True` and `HERMES_SKIP_ASYNC_WRAP = True`).

---

### TEST 3 — Live ACP Connection
- **Status**: **PASS** *(after fixing Issue #1 documented in Section 4)*
- **Execution Output**:
  ```text
  ACP Initialized caps keys: ['protocolVersion', 'agentCapabilities', 'agentInfo', 'authMethods']
  Created session ID: 89d01416-d8ed-4ee0-9fb3-820646940fda
  Available models in session: 23
  Config option response keys: ['configOptions'] Elapsed ms: 2292
  Process alive after config: True Active session ID: 89d01416-d8ed-4ee0-9fb3-820646940fda
  ```
- **Verification**: Launched `agy.exe acp --stdio` with `CREATE_NO_WINDOW`, completed JSON-RPC 2.0 `initialize` + `initialized`, created session `89d01416-d8ed-4ee0-9fb3-820646940fda` via `session/new`, configured model via `session/set_config_option`, and verified persistent process liveness.

---

### TEST 4 — Live Model Conversation
- **Status**: **PASS**
- **Prompt**: `"Hello. Introduce yourself briefly and identify the model you are running."`
- **Execution Output**:
  ```text
  Finish reason: stop Elapsed ms: 4451
  Response content: 'Hello! I am **Antigravity**, an agentic AI assistant developed by Google DeepMind. I am currently running on the **Gemini 3.8 Flash** model. \n\nHow can I help you today?'
  Reasoning content len: 0
  Session ID after turn: 82e7f219-f0fc-44d8-85b9-f081709a85c9
  ```
- **Verification**: Request reached Google Antigravity over ACP stdio and returned a normalized OpenAI `ChatCompletion` response.

---

### TEST 5 — Streaming
- **Status**: **PASS**
- **Prompt**: `"Explain in 4 concise bullet points how JSON-RPC 2.0 requests, responses, and notifications work over stdio."`
- **Execution Output**:
  ```text
  Content chunk count: 10 Thought chunk count: 0
  First 5 chunk timestamps (s): [4.847, 4.983, 5.201, 5.433, 5.559] Last timestamp (s): 6.553
  Leaked <tool_call> tag: False
  Finish reasons: ['stop']
  Streamed text preview: '* **Transport & Framing:** Messages are serialized JSON objects sent through standard streams (typically client writes to server `stdin`, reads from server `stdout`), framed either line-by-line (`\n`-delimited JSON) or w'
  ```
- **Verification**: Verified incremental `ChatCompletionChunk` delivery across 10 distinct arrival timestamps (`4.847s` to `6.553s`), zero duplicated chunks, zero leaked `<tool_call>` tags, and clean stream termination with `finish_reason="stop"`.

---

### TEST 6 — Tool Calling
- **Status**: **PASS**
- **Prompt**: `"Calculate 125 * 48 using an available calculator tool."`
- **Execution Output**:
  ```text
  === TEST 6: LIVE TOOL CALLING ===
  Turn 1 finish_reason: tool_calls
  Turn 1 tool_calls: [ChatCompletionMessageFunctionToolCall(id='call_1', function=Function(arguments='{"a": 125, "b": 48, "operation": "multiply"}', name='calculator'), type='function')]
  Parsed tool call: call_1 calculator {'a': 125, 'b': 48, 'operation': 'multiply'}
  Hermes executed tool result: 6000
  Turn 2 finish_reason: stop
  Turn 2 final answer: '125 * 48 = 6,000.'
  Permission denials count: 0
  ```
- **Verification**:
  1. Antigravity generated `<tool_call>{"name": "calculator", "arguments": {"a": 125, "b": 48, "operation": "multiply"}}</tool_call>`.
  2. `AntigravityClient` parsed it into an OpenAI `ChatCompletionMessageToolCall` (`id='call_1'`, `finish_reason='tool_calls'`).
  3. Hermes executed the tool (`125 * 48 = 6000`) and returned the tool result (`role="tool"`, `tool_call_id="call_1"`).
  4. Antigravity produced the final answer `'125 * 48 = 6,000.'`.
  5. Zero AGY internal tools bypassed Hermes permissions.

---

### TEST 7 — Session Persistence
- **Status**: **PASS**
- **Execution Output**:
  ```text
  === TEST 7: LIVE SESSION PERSISTENCE ===
  Turn 1 session ID: a11225b9-fa83-4c0e-a832-8b390ac8c268 Reply 1: 'Acknowledged.'
  Turn 2 session ID: a11225b9-fa83-4c0e-a832-8b390ac8c268 Session reused: True
  Reply 2: 'The exact secret code word is **ORBITAL-742**.'
  Recalled ORBITAL-742: True
  ```
- **Verification**: Confirmed that multi-turn conversations reuse the exact same ACP session (`a11225b9-fa83-4c0e-a832-8b390ac8c268`), sending only incremental turn deltas while preserving full server-side context continuity.

---

### TEST 8 — Cancellation
- **Status**: **PASS**
- **Execution Output**:
  ```text
  === TEST 8: LIVE CANCELLATION ===
  Chunks received before cancel: 2
  Subprocess still alive after cancel: True
  Subprocess alive after client.close(): False
  ```
- **Verification**: Started a 15-paragraph streaming generation, consumed 2 chunks, and closed the stream early (`stream.close()`), triggering `session/cancel`. The ACP process remained alive for session continuity until `client.close()` terminated the process tree cleanly (`is_running() == False`) with zero orphan processes.

---

### TEST 9 — Fallback
- **Status**: **PASS** *(after fixing Issue #2 documented in Section 4)*
- **Execution Output**:
  ```text
  AGY ACP prompt failed (AGY ACP 'initialize' error (-32099): AGY ACP subprocess stdout closed. (stderr: ... Error: flags provided but not defined: -nonexistent-acp-flag-for-fallback-test)); falling back to 'agy -p' print mode.
  === TEST 9: CONTROLLED ACP FAILURE -> LIVE PRINT FALLBACK ===
  Fallback finish_reason: stop
  Fallback response: 'FALLBACK_OK\n'
  Snapshot preserved len: 2
  ```
- **Verification**: Simulated an ACP transport startup failure on an isolated client (`args=['acp', '--nonexistent-acp-flag-for-fallback-test']`). `AntigravityClient` detected the ACP failure, invoked the secondary `agy --output-format json --model gemini-3.8-flash-low -p ...` fallback once without duplicating requests, returned `'FALLBACK_OK\n'`, and preserved the 2-message conversation snapshot.

---

### TEST 10 — Regression
- **Status**: **PASS**
- **Execution Output**:
  ```text
  tests/agent/test_antigravity_provider.py (9 passed)
  tests/providers/test_plugin_discovery.py (3 passed)
  tests/agent/test_acp_client.py (5 passed)
  tests/agent/test_external_process_streaming.py (12 passed)
  ============================= 29 passed in 12.40s =============================
  python scripts/boundary_lint.py --check -> OK
  python scripts/check --only ruff -> OK
  python scripts/check --only health -> 0 blocking, 0 advisory
  ```

---

## 4. Issues Discovered During Live Validation & Fixes Applied

### Issue #1 — `WindowsPath` Serialization in `session/new` When `acp_cwd` Is Omitted
- **Observed In**: Initial run of **TEST 3 (Live ACP Connection)**.
- **Error Log**:
  ```text
  TypeError: Object of type WindowsPath is not JSON serializable
  when serializing dict item 'cwd'
  when serializing dict item 'params'
  ```
- **Root Cause**: `agent.runtime_cwd.resolve_agent_cwd()` returns a `pathlib.WindowsPath` instance when `create_client()` is called with no explicit `acp_cwd`. Passing that `Path` object directly into JSON-RPC `session/new` (`params.cwd`) caused `json.dumps()` to raise `TypeError`.
- **Fix Applied**: Coerced `cwd` to `str` at initialization and request construction in [client.py](file:///C:/Users/firdaus/APA/plugins/model-providers/antigravity/client.py#L287), [acp_transport.py](file:///C:/Users/firdaus/APA/plugins/model-providers/antigravity/acp_transport.py#L157), and [process_manager.py](file:///C:/Users/firdaus/APA/plugins/model-providers/antigravity/process_manager.py#L219), and added a regression assertion passing `acp_cwd=tmp_path` (`Path`) in [test_antigravity_provider.py](file:///C:/Users/firdaus/APA/tests/agent/test_antigravity_provider.py#L612-L625).

### Issue #2 — `agy -p` Flag Ordering in Secondary Print-Mode Fallback
- **Observed In**: Initial run of **TEST 9 (Fallback)** against real `agy.exe`.
- **Error Log**:
  ```text
  RuntimeError: AGY print fallback failed (exit 2): Error: -p took "--output-format" as its prompt, so the intended prompt was left as an argument and ignored.
  Attach the prompt to the flag (-p='your prompt') and move --output-format elsewhere on the command line.
  ```
- **Root Cause**: `agy.exe`'s CLI flag parser treats `-p` as taking the immediately following token as the prompt string (`-p <prompt>`). Placing `--output-format json` after `-p` caused `agy.exe` to consume `"--output-format"` as the prompt argument.
- **Fix Applied**: Ordered the print-mode fallback arguments in [process_manager.py](file:///C:/Users/firdaus/APA/plugins/model-providers/antigravity/process_manager.py#L522-L528) as `[*prefix, "--output-format", "json", "--model", model, ..., "-p", prompt_text]` so `-p` is always immediately followed by `prompt_text`.

---

## 5. Final Readiness Assessment

- **Overall Status**: **READY FOR PHASE 3 (10 / 10 LIVE & REGRESSION TESTS PASSED)**
- Live authentication, provider resolution, persistent ACP JSON-RPC 2.0 sessions, live model conversation, real-time incremental streaming with `<tool_call>` lookahead buffering, end-to-end Hermes tool calling (`model_tools`), multi-turn session persistence, stream cancellation, and `agy -p` fallback are all verified against the live Google Antigravity CLI (`v1.5.9`) on Windows with zero Hermes core modifications.
