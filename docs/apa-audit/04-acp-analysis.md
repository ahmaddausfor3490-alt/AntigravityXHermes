# 04 — Agent Client Protocol (ACP) Analysis

**Project:** APA (Antigravity Personal Agent) — Phase 1 Audit  
**Repository:** `C:\Users\firdaus\APA` (`apa-development`)  
**Reference:** `D:\Antigravity-Hermes\antigravity\acp_client.py` (Legacy APA) & `C:\Users\firdaus\APA\agent\acp_client.py` (Hermes)

---

## 1. Dual ACP Architecture in Hermes

Hermes contains **two distinct ACP subsystems** serving opposite directions:

| Direction | Module Path | Primary Classes | Role |
|---|---|---|---|
| **Inbound ACP Server** (IDE → Hermes) | [acp_adapter/server.py](file:///C:/Users/firdaus/APA/acp_adapter/server.py), [acp_adapter/session.py](file:///C:/Users/firdaus/APA/acp_adapter/session.py), [acp_adapter/events.py](file:///C:/Users/firdaus/APA/acp_adapter/events.py), [acp_adapter/permissions.py](file:///C:/Users/firdaus/APA/acp_adapter/permissions.py) | `HermesACPAgent`, `SessionManager`, `SessionState` | Exposes Hermes as an ACP server (`hermes acp`) over stdio so code editors (VS Code, Zed, Cursor, JetBrains) can drive Hermes. |
| **Outbound ACP Client** (Hermes → External CLI) | [agent/acp_client.py](file:///C:/Users/firdaus/APA/agent/acp_client.py), [agent/acp_openai_bridge.py](file:///C:/Users/firdaus/APA/agent/acp_openai_bridge.py) | `ACPClient`, `_ACPChatNamespace`, `_ACPCompletions`, `_CapturingClient` | Drives an external ACP-capable CLI binary over stdio JSON-RPC while presenting an OpenAI-compatible `client.chat.completions.create()` interface to Hermes's `AIAgent` loop. |

For APA's goal (**Hermes Engine → AGY → Antigravity**), the **Outbound ACP Client** (`agent/acp_client.py`) and Legacy APA's `antigravity/acp_client.py` are the relevant bridges.

---

## 2. Deep-Dive Inspection of Outbound ACP (`agent/acp_client.py` vs `agy.exe acp`)

### 2.1 Initialization & Process Spawning
- **Hermes `ACPClient` ([agent/acp_client.py](file:///C:/Users/firdaus/APA/agent/acp_client.py#L172-L246)):**
  - Uses a dedicated background daemon thread running an `asyncio` event loop (`_loop`).
  - `_ensure_started()` calls `acp.spawn_agent_process(client, self._binary, cwd=str(self._cwd), env=env, transport_kwargs={"limit": 16 * 1024 * 1024})` and `await conn.initialize(protocol_version=acp.PROTOCOL_VERSION)`.
  - **Critical Gap Found (L188):** `acp.spawn_agent_process(client, self._binary)` invokes `self._binary` **without arguments** (designed for standalone `codex-acp` / `claude-code-acp` binaries). However, `agy.exe` requires subcommand arguments: `["agy", "acp", "--stdio"]`!
- **Legacy APA `AcpClient` ([D:/Antigravity-Hermes/antigravity/acp_client.py](file:///D:/Antigravity-Hermes/antigravity/acp_client.py#L89-L112)):**
  - Directly spawns `subprocess.Popen([self.binary_path, "acp", "--stdio"], stdin=PIPE, stdout=PIPE, stderr=PIPE, text=True, bufsize=1)` and runs NDJSON JSON-RPC 2.0 `initialize` + `initialized`.

### 2.2 Session Creation & Management
- **Hermes `ACPClient` ([agent/acp_client.py](file:///C:/Users/firdaus/APA/agent/acp_client.py#L211-L212)):** Calls `await conn.new_session(cwd=str(self._cwd), mcp_servers=[])` once when spawning the subprocess and reuses `_session_id` across subsequent `.create()` turns.
- **Stateful Continuity Risk:** Because the ACP subprocess maintains its own internal conversation history per `sessionId`, sending the full `messages[]` transcript on every turn would duplicate prior context. `ACPClient._prepare_prompt()` ([agent/acp_client.py](file:///C:/Users/firdaus/APA/agent/acp_client.py#L298-L355)) solves this by tracking `_system_sent` (injecting system prompt + tool definitions only on the first turn of the ACP session) and formatting only the incremental delta (`format_Incremental_turn` in [agent/acp_openai_bridge.py](file:///C:/Users/firdaus/APA/agent/acp_openai_bridge.py#L64-L98)) on subsequent turns!

### 2.3 Prompt Execution, Message Handling & Tool Execution Bridge
How does Hermes execute its own tools (`terminal`, `read_file`, `write_file`, `memory`, `delegate_task`, MCP tools) when talking to an ACP backend?
- **Auto-Denying Subprocess Native Tools ([agent/acp_client.py](file:///C:/Users/firdaus/APA/agent/acp_client.py#L51-L65)):** `_CapturingClient.request_permission()` returns `DeniedOutcome(outcome="cancelled")` for any internal tool the ACP subprocess tries to run itself, forcing the model to use Hermes's tool bridge instead of bypassing Hermes's approval/security layer.
- **XML Tool Bridge ([agent/acp_openai_bridge.py](file:///C:/Users/firdaus/APA/agent/acp_openai_bridge.py#L22-L140)):**
  1. `format_tool_definitions(tools)` embeds Hermes's OpenAI tool schemas into the first prompt with instructions to emit `<tool_call>{"name": "...", "arguments": {...}}</tool_call>`.
  2. When the model responds, `parse_tool_calls_from_text(text)` extracts `<tool_call>` blocks and converts them into OpenAI `ChatCompletionMessageToolCall` objects (`finish_reason="tool_calls"`).
  3. Hermes's `AIAgent` loop executes the tools through `model_tools.handle_function_call()` (preserving all Hermes hooks, approvals, and APA Security Guard) and sends the `<tool_result>` back in the next incremental ACP prompt turn.

### 2.4 Streaming
- **Hermes `ACPClient._stream_completion()` ([agent/acp_client.py](file:///C:/Users/firdaus/APA/agent/acp_client.py#L465-L539)):** Bridges `_CapturingClient.session_update()` (`AgentMessageChunk`, `AgentThoughtChunk`) via a thread-safe `queue.Queue` into OpenAI `ChatCompletionChunk` objects (`delta.content`, `delta.reasoning_content`).
- **Caveat when `tools[]` are active (L472–L481):** When `tools` are present, `ACPClient._stream_completion()` buffers the full turn via `_complete_sync()` before emitting chunks so `<tool_call>` XML tags are parsed cleanly rather than leaked as raw text to the user.

### 2.5 Timeout Behavior, Cancellation & Process Lifecycle
- **Timeout:** `HERMES_ACP_PROMPT_TIMEOUT` (default `1800.0`s in [agent/acp_client.py](file:///C:/Users/firdaus/APA/agent/acp_client.py#L44)). On timeout, `ACPClient._reset_process()` kills the wedged subprocess so the next turn starts cleanly.
- **User Interrupt (`Ctrl+C` / `/stop`):** `ACPClient.cancel()` ([agent/acp_client.py](file:///C:/Users/firdaus/APA/agent/acp_client.py#L541-L553)) cancels the in-flight `concurrent.futures.Future` and resets the subprocess (`client_lifecycle.py` calls `client.cancel()` from the signal/interrupt handler).
- **Cleanup:** `atexit.register(self.close)` terminates/kills the subprocess on interpreter exit.

---

## 3. Comparison: `agy acp --stdio` vs Direct `agy -p --output-format stream-json`

Both `agy acp --stdio` and `agy -p` (`--print`) were verified on the installed `agy.exe` (`v1.5.9`):

| Dimension | `agy acp --stdio` (Persistent ACP Server) | `agy -p --output-format stream-json` (CLI Print Mode) |
|---|---|---|
| **Process Overhead** | **Low:** Spawns `agy.exe` once per session; zero cold-start latency on turn 2+. | **Medium:** Spawns a new `agy.exe` process per turn (using `--conversation <id>` or `--continue` to resume). |
| **Session Synchronization** | Stateful `sessionId` over stdio; supports `session/new`, `session/load`, `session/set_config_option` (`model`, `thought_level`). | Stateless process invocation; relies on `--conversation <id>` disk state or passing full prompt. |
| **Streaming Granularity** | Real-time JSON-RPC `session/update` notifications (`agent_message_chunk`, `agent_thought_chunk`, `tool_call`). | Real-time NDJSON stdout stream (`--output-format stream-json`). |
| **In-Turn Cancellation** | Supports graceful JSON-RPC `session/cancel` notification without killing the server process. | Requires terminating the `agy.exe` subprocess (`TerminateProcess` on Windows). |
| **Model & Effort Switching** | Dynamic mid-session switching via `session/set_config_option` (`model`, `thought_level`). | Per-invocation flags `--model <name>` and `--effort low\|medium\|high\|xhigh\|max`. |
| **Subprocess Crash Recovery** | Requires detecting EOF/timeout and re-running `initialize` + `session/load` or `session/new`. | Naturally isolated per turn; a failed process does not poison subsequent turns. |

---

## 4. Identified Risks & Mitigation Recommendations

| Risk Area | Technical Cause | Recommended Mitigation in APA Provider Adapter |
|---|---|---|
| **Subprocess Native Tool Collision** | `agy acp` has its own built-in tools (`read_file`, `run_command`, etc.). If `agy` runs them directly, Hermes's `pre_tool_call` hooks, `tools/approval.py`, and APA Security Guard are bypassed. | Deny/disable `agy`-internal tool execution in ACP permission callbacks (`DeniedOutcome`) or run `agy acp --sandbox` while routing tool calls through Hermes's `acp_openai_bridge.py` (`handle_function_call`). |
| **Session Desynchronization after Compression** | When Hermes's `ContextCompressor` summarizes history, the `agy acp` session still holds the uncompressed history in memory. | Track a hash of `messages[0..n]` (or detect compression reset) in the adapter; when Hermes compresses context, open a fresh `session/new` in `agy acp` seeded with the compressed summary. |
| **Streaming Buffering with Tools** | `ACPClient._stream_completion()` buffers the whole turn when `tools` are passed so `<tool_call>` XML tags aren't printed raw. | Use a streaming lookahead scanner (hold back only once `<tool_call>` prefix is detected) so normal prose streams token-by-token in real time while `<tool_call>` blocks are captured for Hermes tool execution. |
| **Windows Orphan Processes** | `proc.terminate()` on Windows calls `TerminateProcess` on the parent PID only, potentially leaving child workers if `--process-per-session` is used. | Spawn `agy.exe` with `windows_hide_flags()` ([hermes_cli/_subprocess_compat.py](file:///C:/Users/firdaus/APA/hermes_cli/_subprocess_compat.py#L39)) and attach a Windows Job Object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) or avoid `--process-per-session`. |
