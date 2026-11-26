# APA Phase 5 Bug Fix Report — Streaming Timeout Normalization

## 1. Executive Summary

This report documents the root-cause investigation, defense-in-depth architectural fix, and regression test suite resolving the recurring runtime error:

```text
TypeError: '<' not supported between instances of 'Timeout' and 'int'
```

The error occurred when sending messages (such as `hi`) through Google Antigravity (AGY) in the Hermes CLI/TUI interface under Python 3.14.9.

All changes were implemented strictly within the APA Antigravity provider plugin (`plugins/model-providers/antigravity/`). **Zero Hermes Core files were modified or monkey-patched.**

---

## 2. Root Cause Analysis & Trace

### 2.1 Complete Execution Call Chain

When an agent turn executes with streaming enabled (`apa` CLI / Hermes Agent turn loop):

1. **User Input**: The user types `hi` into the prompt.
2. **Turn Initiation**: Hermes initiates an agent turn via `run_agent.py` and `agent/chat_completion_helpers.py`.
3. **Timeout Construction**: In `agent/chat_completion_helpers.py:3040`:
   ```python
   timeout = _httpx.Timeout(connect=conn_cap, read=read_timeout, write=base_timeout, pool=conn_cap)
   return self._open_chat_stream({**next_api_kwargs, "stream": True, "timeout": timeout})
   ```
   Hermes follows the standard OpenAI Python SDK contract (`float | httpx.Timeout | None`), passing a composite `httpx.Timeout` object.
4. **Client Invocations**:
   - `client.chat.completions.create(..., stream=True, timeout=timeout)` calls `_AntigravityCompletions.create(...)`.
   - When callers or shims call `client.stream_completion(...)` or `client.create_completion(...)` directly, `_AntigravityCompletions.create()` is bypassed.
5. **Transport Prompt Call**:
   - `stream_completion()` delegates to `self._transport.prompt(..., timeout=timeout)`.
   - `prompt()` forwards `timeout` to `_execute_prompt_once(..., timeout=timeout)`.
6. **The Critical Falsy Check**:
   In `_execute_prompt_once()`:
   ```python
   result = self._request("session/prompt", ..., timeout=timeout or self._request_timeout)
   ```
   In Python, an instance of `httpx.Timeout` evaluates to `True` in boolean context (`bool(httpx.Timeout(...)) == True`). Therefore, `timeout or self._request_timeout` evaluated directly to the `httpx.Timeout` instance rather than falling back to numeric seconds.
7. **The Crash at `queue.Queue.get()`**:
   In `AGYACPTransport._request(..., timeout=timeout)`:
   ```python
   response = waiter.get(timeout=timeout)
   ```
   In Python 3 (standard library `queue.Queue.get`):
   ```python
   if timeout is not None:
       if timeout < 0:
           raise ValueError("'timeout' must be a non-negative number")
   ```
   Because `timeout` was an `httpx.Timeout` instance, Python evaluated `timeout < 0`. Since `httpx.Timeout` does not implement numeric ordering comparison operators with `int`, Python raised:
   ```text
   TypeError: '<' not supported between instances of 'Timeout' and 'int'
   ```
8. **Secondary Failure Point in Error Formatting**:
   In `_request`'s `queue.Empty` handler:
   ```python
   raise AntigravityACPError(f"AGY ACP request '{method}' timed out after {timeout:.1f}s{suffix}")
   ```
   Formatting `{timeout:.1f}s` would also fail with `TypeError: unsupported format string passed to Timeout` if `timeout` remained an `httpx.Timeout` object.

---

## 3. Defense-in-Depth Solution Architecture

To guarantee that this class of error can never occur regardless of caller or entry point, a defense-in-depth normalization pattern was implemented across both the transport and client layers.

### 3.1 Layer 1: ACP Transport Normalization (`acp_transport.py`)

A universal normalization function `coerce_timeout_seconds` was introduced:

```python
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
```

Applied at the transport boundary:
1. **`AGYACPTransport._request`**:
   Coerces `timeout` immediately with `effective_timeout = coerce_timeout_seconds(timeout, default=self._request_timeout)`.
   Passes `effective_timeout` to `waiter.get(timeout=effective_timeout)`.
   Uses `effective_timeout:.1f}` in the timeout error string.
2. **`AGYACPTransport.prompt`**:
   Coerces `timeout` with `resolved_timeout = coerce_timeout_seconds(timeout, default=self._request_timeout)` at the entry point of the turn lock, ensuring all downstream calls and unexpected process crash retries preserve the normalized `float`.
3. **`AGYACPTransport._execute_prompt_once`**:
   Guarantees `effective_timeout` is always a float before making the `"session/prompt"` JSON-RPC call.

### 3.2 Layer 2: Client Normalization (`client.py`)

1. **`_request_budget_seconds(timeout)`**:
   Re-implemented to safely leverage `coerce_timeout_seconds(timeout, default=-1.0)`: returns `float` if numeric components exist, or `None` if unconfigured (allowing transport defaults).
2. **`AntigravityClient.create_completion`**:
   Resolves `resolved_timeout = _request_budget_seconds(timeout)` and forwards it to `prompt()` and `_pm.run_print_prompt()`.
3. **`AntigravityClient.stream_completion`**:
   Resolves `resolved_timeout = _request_budget_seconds(timeout)` and forwards it to the streaming worker thread and `prompt()`.
4. **`_AntigravityCompletions.create`**:
   Maintains normalization for OpenAI SDK compatibility.

---

## 4. Verification and Regression Testing

### 4.1 Test Matrix

The regression test file [test_apa_antigravity_timeout.py](file:///C:/Users/firdaus/APA/tests/plugins/test_apa_antigravity_timeout.py) was expanded from 7 to 15 comprehensive tests covering all edge cases:

| Test Case | Scenario / Condition | Result |
| :--- | :--- | :---: |
| `test_request_budget_follows_openai_timeout_contract` | `None`, `int`, `float`, and `httpx.Timeout` normalization | **PASSED** |
| `test_streaming_hi_with_hermes_httpx_timeout` | User scenario: streaming `hi` with `httpx.Timeout(120, 1800, 30)` | **PASSED** |
| `test_non_streaming_with_httpx_timeout` | Synchronous completion with `httpx.Timeout` | **PASSED** |
| `test_request_timeout_still_raises_acp_error` | Short budget (`httpx.Timeout(0.5)`) raises `AntigravityACPError` | **PASSED** |
| `test_long_running_turn_uses_whole_request_budget_not_read` | Whole-turn budget (write=10s) honored over chunk read (0.5s) | **PASSED** |
| `test_retry_after_transient_process_crash` | Coerced timeout survives automatic subprocess restart | **PASSED** |
| `test_none_timeout_keeps_transport_default` | `timeout=None` uses `transport._request_timeout` (300.0s) | **PASSED** |
| `test_streaming_with_float_timeout` | Streaming with standard numeric float (45.0s) | **PASSED** |
| `test_direct_stream_completion_with_httpx_timeout` | Direct call to `client.stream_completion` with `httpx.Timeout` | **PASSED** |
| `test_direct_create_completion_with_httpx_timeout` | Direct call to `client.create_completion` with `httpx.Timeout` | **PASSED** |
| `test_direct_transport_prompt_with_httpx_timeout` | Direct call to `transport.prompt` with `httpx.Timeout` | **PASSED** |
| `test_direct_transport_request_with_httpx_timeout` | Direct call to `transport._request` with `httpx.Timeout` | **PASSED** |
| `test_coerce_timeout_seconds_edge_cases` | Edge cases: negative, zero, non-numeric strings, empty lists, None | **PASSED** |
| `test_recovery_after_timeout_expiration` | Immediate subsequent request recovery after a timeout failure | **PASSED** |
| `test_cancellation_during_streaming` | Graceful turn cancellation via `client.cancel()` | **PASSED** |

### 4.2 Full Provider & System Suite Results

1. **Timeout Regression Suite**:
   ```text
   pytest tests/plugins/test_apa_antigravity_timeout.py
   ============================= 15 passed in 13.92s =============================
   ```
2. **Antigravity Provider Integration Suite**:
   ```text
   pytest tests/agent/test_antigravity_provider.py
   ============================= 11 passed in 6.13s ==============================
   ```
3. **Core ACP Client Invariant Suite**:
   ```text
   pytest tests/agent/test_acp_client.py
   ============================== 5 passed in 8.68s ==============================
   ```
4. **APA Product Integration & Visual Customization Suite**:
   ```text
   pytest tests/plugins/test_apa_product_integration.py tests/plugins/test_apa_visual_customization.py
   ============================= 26 passed in 24.59s =============================
   ```

### 4.3 Quality Gates & Linter Validation

- **Ruff Lint Check**: `python scripts/check --only ruff` -> **PASSED (1 checks, ok)**.
- **Code Health Check**: `python scripts/check --only health` -> **PASSED (0 blocking, 0 advisory)**.
- **Boundary Lint**: `python scripts/boundary_lint.py --check` -> **PASSED (module_to_core: 839, core_to_module: 20)**.

---

## 5. Architectural Invariants Compliance

- **Zero Hermes Core Modification**: No core Hermes files modified.
- **Zero Monkey Patching**: Provider implementation conforms cleanly to the `ProviderProfile` and `ACPClient` contracts.
- **Zero Global Hermes Tampering**: Global `~/.hermes/` remains unmodified.
- **Zero Git Commit / Push**: Repository working tree remains uncommitted.
