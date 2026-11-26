# APA Phase 3.2 — Hermes Security Architecture Analysis

## 1. Executive Summary

This document analyzes the tool execution pipeline, plugin hook lifecycle, built-in approval and file safety mechanisms, and subagent/bridge execution paths in Hermes Agent (`C:\Users\firdaus\APA`).

**Core Architectural Finding**: Hermes Agent already provides a **fail-closed, non-bypassable plugin policy hook (`pre_tool_call`)** and an **observer hook (`post_tool_call`)** inside `model_tools.handle_function_call()`. Consequently, **zero modifications to Hermes Core** are required to enforce APA Security Guard across all tool calls, Antigravity ACP/print sessions, bridged tool calls, and subagents.

---

## 2. Hermes Tool Execution Pipeline

Every tool execution in Hermes flows through `model_tools.handle_function_call()` (`model_tools.py:L912-1012`):

```
AIAgent._execute_tool_calls (agent/turn_execution.py)
  │
  ▼
model_tools.handle_function_call(function_name, function_args, ...)
  │
  ├─► 1. Legacy alias normalization (_LEGACY_TOOL_ALIASES)
  ├─► 2. Hosted tool policy check (tools.hosted_tool_policy.hosted_tool_refusal)
  ├─► 3. Bridge tool resolution (_dispatch_bridge_tool: tool_search / tool_describe / tool_call)
  │      └─► If function_name == "tool_call": unwraps underlying_name & underlying_args
  │          and recursively calls handle_function_call(..., skip_pre_tool_call_hook=False)
  ├─► 4. Request middleware (_apply_request_middleware)
  ├─► 5. Pre-dispatch guards (_pre_dispatch_guards, model_tools.py:L794-829)
  │      ├─► _dispatch_pre_tool_call_hooks(function_name, function_args, ...)
  │      │     └─► Invokes all registered "pre_tool_call" plugin hooks
  │      │     └─► Action "block"   -> returns tool_error(block_message), error_type="plugin_block"
  │      │     └─► Action "approve" -> calls tools.approval.request_tool_approval() (fail-closed)
  │      │     └─► Action "modify"  -> shallow-merges modified args
  │      └─► ACP edit approval (acp_adapter.edit_approval.maybe_require_edit_approval)
  ├─► 6. Tool execution (_execute_tool -> registry.dispatch)
  ├─► 7. Result middleware & transform_tool_result hooks
  └─► 8. Post-tool-call observer hook (_emit_post_tool_call_hook -> invoke_hook("post_tool_call", ...))
```

### Key Security Properties of `pre_tool_call` in Hermes (`hermes_cli/plugins_dispatch.py` & `hermes_cli/plugins.py`)
1. **Fail-Closed on Exception (`plugins_dispatch.py:L318-324`)**:
   `pre_tool_call` is registered in `_HOOK_TIMEOUT_FAIL_CLOSED_HOOKS`. If a `pre_tool_call` callback raises any exception (`Exception` or `SystemExit`), `_run_hook_callbacks` catches it and appends `_policy_error_block_directive(hook_name, cb, exc)` (`{"action": "block", "message": ...}`).
2. **Fail-Closed on Timeout (`plugins_dispatch.py:L306-313`)**:
   If a `pre_tool_call` callback exceeds `plugins.hook_callback_timeout` (default 30s) or is suppressed due to a hung worker, `_run_hook_callbacks` appends `{"action": "block", "message": "pre_tool_call plugin callback timed out or is still running"}`.
3. **Veto Precedence (`plugins.py:L1155-1184`)**:
   `_get_pre_tool_call_directive_details` scans all hook results: any `{"action": "block", "message": "..."}` immediately wins over any `"approve"` or `None` result.
4. **Fail-Closed Human Approval (`plugins.py:L1212-1241`)**:
   When a `pre_tool_call` callback returns `{"action": "approve", "message": "...", "rule_key": "..."}`, `_resolve_block_from_details` invokes `tools.approval.request_tool_approval()`. If the approval gate raises an exception, times out, or is denied by the user, the tool call is blocked before `_execute_tool` is ever reached.
5. **Post-Call Observability on Blocked Calls (`model_tools.py:L970-979`)**:
   Even when `_pre_dispatch_guards` blocks a tool call, `_emit_post_tool_call_hook` still fires with `status="error", error_type="plugin_block", error_message=block_message`, enabling complete audit trail correlation.

---

## 3. Existing Hermes Security Primitives

| Module | Existing Capability | How `apa-security` Integrates With It |
|---|---|---|
| `agent/file_safety.py` | Blocks Windows device/NT paths (`\\.\`, `\??\`, `\\?\UNC\`, ADS `file.txt:stream`, DOS device names `CON`, `NUL`, `COM1`) and Credential Manager / `~/.hermes/.env` writes | Imported via public plugin API (`get_read_block_error`, `is_write_denied`, `get_write_denied_error`) as Layer 1 of `PathValidator` and `FilesystemGuard`, extended with strict workspace containment and sensitive file/directory rules |
| `agent/redact.py` | Redacts API keys, tokens, assignments, and URL credentials (`redact_sensitive_text`) | Imported via public plugin API (`redact_sensitive_text`) and combined with legacy APA regex scrubbers inside `AuditLogger` |
| `agent/runtime_cwd.py` | Profile/session-scoped working directory resolution (`resolve_agent_cwd()`) | Imported via public plugin API (`resolve_agent_cwd`) to resolve default workspace root when not explicitly overridden in config |
| `tools/approval.py` | Dangerous command patterns and interactive CLI/gateway approval prompting (`request_tool_approval`) | Used automatically when `apa-security` returns `{"action": "approve", ...}` or overridden with a custom confirmation callback / fail-closed policy in `PermissionManager` |

---

## 4. Bypass Resistance Across Execution Surfaces

| Execution Surface | How Tool Calls Execute | Why `apa-security` Cannot Be Bypassed |
|---|---|---|
| **Antigravity ACP Transport (`agy acp --stdio`)** | In Phase 2 (`plugins/model-providers/antigravity/acp_transport.py`), AGY's internal `session/request_permission` is unconditionally cancelled (`outcome: cancelled`) and `fs/*` RPCs are rejected (`-32601`). Tool calls are emitted as `<tool_call>` blocks parsed by `bridge.py` and returned to `AIAgent`. | `AIAgent` dispatches every parsed tool call through `model_tools.handle_function_call()`, which runs `_dispatch_pre_tool_call_hooks`. |
| **Antigravity Fallback Print Mode (`agy -p`)** | `client.py` invokes `agy -p` with the same tool bridge prompt; `<tool_call>` blocks are parsed into OpenAI `ChatCompletionMessageToolCall` objects and returned to `AIAgent`. | `AIAgent` dispatches tool calls through `model_tools.handle_function_call()`, firing `pre_tool_call`. |
| **Deferred Tool Search Bridge (`tool_call`)** | When Tool Search is active, the model calls `tool_call(name=..., arguments=...)`. | `model_tools.py:L947-968` unwraps `underlying_name` and recursively calls `handle_function_call(..., skip_pre_tool_call_hook=False)`. Additionally, `ToolInterceptor` inspects `tool_call` arguments directly if invoked before unwrap. |
| **Programmatic Tool Calling (`execute_code`)** | `execute_code` spawns a Python script that can call tools over an internal RPC socket (`tools/code_execution_tool.py`), which calls `handle_function_call()` on the parent process. | First, `execute_code` itself is intercepted by `pre_tool_call` (classified as `EXECUTE` / `HIGH` risk, scanning `code` for dangerous operations). Second, every RPC tool call inside `execute_code` calls `handle_function_call()`, firing `pre_tool_call` again. |
| **Subagents (`delegate_task`)** | `delegate_task` spawns a child `AIAgent` in the same process/profile scope (`tools/delegate_tool.py`). | First, `delegate_task` is intercepted at spawn time. Second, the child `AIAgent` shares the active profile's `PluginManager` and calls `model_tools.handle_function_call()` for every subagent tool call. |
| **Background Process Management (`process` / `process_manage`)** | Used to poll/interact with background commands started via `terminal(background=True)`. | Initial `terminal(background=True)` is validated by `CommandValidator` before process creation; subsequent `process(action="submit"|"write", data=...)` calls are also inspected by `CommandValidator`. |

---

## 5. Hermes Core Modification Plan

- **Planned Modifications to Hermes Core**: **NONE (`0` files).**
- **Justification**:
  1. `hermes_cli/plugins.py` and `model_tools.py` already invoke `pre_tool_call` before every tool execution and `post_tool_call` after every tool execution (including blocked executions).
  2. `pre_tool_call` is already fail-closed on both exceptions and timeouts.
  3. All required helpers (`hermes_constants.get_hermes_home`, `hermes_cli.config.load_config`, `agent.runtime_cwd.resolve_agent_cwd`, `agent.file_safety.*`, `agent.redact.redact_sensitive_text`, `hermes_yaml.safe_load`) are already published in `hermes_cli/data/plugin_api.json`.
  4. Delivering APA Security Guard purely inside `plugins/apa-security/` satisfies `plugins/AGENTS.md` ("Plugins never touch core") and passes `scripts/boundary_lint.py --check` with zero new boundary crossings.
