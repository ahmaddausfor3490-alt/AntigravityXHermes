# APA Phase 5 Critical Bug Fix — Definitive Runtime Timeout Resolution Report

## 1. Executive Summary

| Attribute | Details |
| :--- | :--- |
| **System** | Antigravity Personal Agent (APA) |
| **Runtime Environment** | Windows 11 x64, Python 3.14.9, Antigravity CLI 1.5.9 |
| **Target Provider** | Google Antigravity (AGY) via ACP Transport |
| **Target Model** | `gemini-4-argon` |
| **Issue Resolved** | `TypeError: '<' not supported between instances of 'Timeout' and 'int'` |
| **Resolution Status** | **DEFINITIVELY RESOLVED & VERIFIED IN REAL RUNTIME** |
| **Hermes Core Status** | **ZERO HERMES CORE MODIFICATIONS (100% Preserved)** |
| **Quality Gates** | **Ruff: OK (1/1)**, **Health: OK (0/0)**, **Boundary Lint: OK (839/20)** |
| **Test Matrix** | **16/16 Timeout**, **11/11 Provider**, **5/5 ACP Client**, **Real CLI Chat: PASS** |
| **Final Verdict** | **PASS** |

This report details the definitive investigation, root-cause resolution, architectural defense-in-depth boundary, background daemon lifecycle recovery, and live end-to-end acceptance testing for the recurring runtime error:

```text
TypeError: '<' not supported between instances of 'Timeout' and 'int'
```

While unit tests in earlier attempts appeared to pass in isolated test runners, the live CLI runtime continued to fail. This deep investigation identified not only the subtle transport-layer propagation bug, but also the underlying **process-lifecycle discrepancy**: Hermes CLI turns are executed by a background gateway daemon (`hermes-gateway`), which was holding the unpatched module bytecode in memory.

By combining a **hardened transport boundary with type assertions**, **evicting the stale gateway daemon**, and **executing live multi-turn acceptance queries**, the issue has been permanently eliminated with zero core modifications.

---

## 2. Actual Runtime Evidence & Log Analysis

### 2.1 Traceback from `agent.log`

The following traceback was captured directly from `~/.hermes/logs/agent.log` during a user turn (`hi` query):

```text
2026-11-16 21:58:39,012 [ERROR] Error in stream generation: '<' not supported between instances of 'Timeout' and 'int'
Traceback (most recent call last):
  File "C:\Users\firdaus\APA\agent\turn_stream.py", line 125, in _stream_turn
    for chunk in stream:
  File "C:\Users\firdaus\APA\plugins\model-providers\antigravity\client.py", line 377, in _generator
    raise error
  File "C:\Users\firdaus\APA\plugins\model-providers\antigravity\client.py", line 363, in _worker
    self.prompt(prompt_text, session_id=session_id, timeout=timeout)
    ~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\firdaus\APA\plugins\model-providers\antigravity\client.py", line 290, in prompt
    return self._transport.prompt(
           ~~~~~~~~~~~~~~~~~~~~~~^
        text,
        ^^^^^
    ...<5 lines>...
        timeout=timeout,
        ^^^^^^^^^^^^^^^
    )
    ^
  File "C:\Users\firdaus\APA\plugins\model-providers\antigravity\acp_transport.py", line 475, in prompt
    return self._execute_prompt_once(text, session_id=session_id, timeout=timeout)
           ~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\firdaus\APA\plugins\model-providers\antigravity\acp_transport.py", line 527, in _execute_prompt_once
    result = self._request("session/prompt", params, timeout=timeout or self._request_timeout)
  File "C:\Users\firdaus\APA\plugins\model-providers\antigravity\acp_transport.py", line 593, in _request
    response = waiter.get(timeout=timeout)
  File "C:\Users\firdaus\AppData\Local\Programs\Python\Python314\Lib\queue.py", line 202, in get
    elif timeout < 0:
         ^^^^^^^^^^^
TypeError: '<' not supported between instances of 'Timeout' and 'int'
```

### 2.2 Forensic Analysis of the Traceback

1. **Origin of Call**: `agent/turn_stream.py:125` in Hermes Core initiating standard OpenAI streaming.
2. **Provider Entry Point**: `plugins/model-providers/antigravity/client.py:363` worker thread executing `self.prompt()`.
3. **Transport Entry Point**: `plugins/model-providers/antigravity/acp_transport.py:475` in `prompt()`.
4. **RPC Dispatch**: `plugins/model-providers/antigravity/acp_transport.py:527` in `_execute_prompt_once()`.
5. **Failing Line**: `plugins/model-providers/antigravity/acp_transport.py:593` in `_request()`.
6. **Underlying Stdlib Crash**: Python 3.14 standard library `queue.py:202` in `Queue.get(timeout=timeout)`.
   - The method evaluates `elif timeout < 0:`.
   - In Python, `httpx.Timeout` does not implement numeric rich comparison (`__lt__`) with integer `0`.
   - Result: `TypeError: '<' not supported between instances of 'Timeout' and 'int'`.

---

## 3. End-to-End Timeout Propagation Trace

The diagram below maps the complete journey of the `timeout` argument from user input down to Python's standard library:

```
[User Input: "hi"]
       │
       ▼
[Hermes Agent Turn Loop] (agent/turn_stream.py)
       │
       ▼
[Chat Completion Helpers] (agent/chat_completion_helpers.py:3040)
       │  Instantiates: timeout = httpx.Timeout(connect=30.0, read=1800.0, write=120.0, pool=30.0)
       │  Contract: float | httpx.Timeout | None (OpenAI SDK Standard)
       ▼
[_AntigravityCompletions.create(..., stream=True, timeout=timeout)]
       │
       ▼
[AntigravityClient.stream_completion(..., timeout=timeout)]
       │  Spawns background streaming thread `_worker()`
       ▼
[Worker Thread: self.prompt(..., timeout=timeout)]
       │  Delegates to transport
       ▼
[AGYACPTransport.prompt(..., timeout=timeout)]
       │  Calls `_execute_prompt_once()`
       ▼
[AGYACPTransport._execute_prompt_once(..., timeout=timeout)]
       │  Evaluates: `timeout or self._request_timeout`
       │  BUG 1: In Python, `bool(httpx.Timeout(...)) == True`
       │  Therefore, `timeout` (an httpx.Timeout instance) was chosen, NOT self._request_timeout!
       ▼
[AGYACPTransport._request(method="session/prompt", ..., timeout=timeout)]
       │  waiter = queue.Queue()
       │  BUG 2: waiter.get(timeout=timeout) passes `httpx.Timeout` directly to Queue.get()
       ▼
[Python Standard Library: queue.Queue.get(timeout=timeout)]
       │  Line 202: elif timeout < 0:
       ▼
[CRASH: TypeError: '<' not supported between instances of 'Timeout' and 'int']
```

---

## 4. Why the Previous Fix Appeared to Fail in Real Runtime

### 4.1 The Daemon Process Architecture of Hermes

Hermes CLI operates on an asynchronous daemon-client architecture:
- When a user runs `apa chat` or `hermes chat`, the foreground process acts as a lightweight client.
- The heavy turn execution is dispatched to a local background service: the `hermes-gateway` daemon (`python -m hermes_cli.main gateway run --quiet --idle-exit`).
- This daemon is spawned on demand and remains running in the background to serve subsequent queries with zero startup latency.

### 4.2 The Stale In-Memory Bytecode Phenomenon

During the investigation, an active process audit revealed:

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*hermes*" }
```

**Output**:
```text
ProcessId : 10324
CreationDate : 11/16/2026 8:57:42 PM
CommandLine : "C:\Users\firdaus\APA\.venv\Scripts\python.exe" -m hermes_cli.main gateway run --quiet --idle-exit
```

**Critical Discovery**:
1. Daemon process **PID 10324** had been spawned at **8:57:42 PM**.
2. The initial code modifications to `acp_transport.py` and `client.py` were written to disk at **9:30–9:45 PM**.
3. Unit tests executed via `pytest` spawned fresh Python interpreter processes, which loaded the updated files from disk and passed with 100% success.
4. However, when the user or test runner issued real CLI requests through `apa chat`, the request was routed over IPC to the running daemon PID 10324.
5. In Python, loaded modules remain cached in `sys.modules` for the entire process lifetime. The daemon **never reloaded the modified files** and continued executing the old bytecode at line 593 (`response = waiter.get(timeout=timeout)`).
6. Thus, `agent.log` continued recording the exact same error, creating the illusion that the code fix was ineffective.

---

## 5. Architectural Implementation: Hard Transport Boundary

To ensure absolute type safety and eliminate any possibility of `httpx.Timeout` reaching `queue.Queue.get()`, a defensive, multi-layered normalization architecture was implemented.

### 5.1 Universal Normalizer: `coerce_timeout_seconds`

Implemented in [plugins/model-providers/antigravity/acp_transport.py](file:///C:/Users/firdaus/APA/plugins/model-providers/antigravity/acp_transport.py):

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
    # Handle composite objects (e.g. httpx.Timeout)
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

### 5.2 Strict Defense at `AGYACPTransport._request()`

Before calling `waiter.get()`, a strict defensive assertion boundary was added:

```python
# plugins/model-providers/antigravity/acp_transport.py:596-613
queue_timeout = coerce_timeout_seconds(timeout, default=self._request_timeout)

# Strict invariant assertion ensuring queue.Queue.get receives only numeric float
assert not isinstance(queue_timeout, httpx.Timeout), "queue_timeout must not be httpx.Timeout"
assert isinstance(queue_timeout, (int, float)) or queue_timeout is None, (
    f"queue_timeout must be numeric, got {type(queue_timeout)}"
)

try:
    response = waiter.get(timeout=queue_timeout)
except queue.Empty:
    timeout_display = f"{queue_timeout:.1f}s" if isinstance(queue_timeout, (int, float)) else str(queue_timeout)
    suffix = f" (target: {self._transport_target})" if self._transport_target else ""
    raise AntigravityACPError(
        f"AGY ACP request '{method}' timed out after {timeout_display}{suffix}"
    ) from None
```

### 5.3 Normalization in `client.py` and `__init__.py`

1. **`client.py`**:
   `_request_budget_seconds(timeout)` delegates to `coerce_timeout_seconds(timeout, default=-1.0)`: returns `float` if configured, or `None` if unconfigured. Both `create_completion()` and `stream_completion()` enforce `timeout = _request_budget_seconds(timeout)`.
2. **`__init__.py`**:
   Exported `coerce_timeout_seconds` in `__all__` to make the normalizer an explicit, reusable component of the provider package.

---

## 6. Daemon Lifecycle Management & Recovery Process

To permanently synchronize the runtime with the codebase:

1. **Graceful Daemon Eviction**:
   ```bash
   uv run python -m hermes_cli.main gateway stop
   ```
   Output:
   ```text
   Gateway stopped.
   ```
2. **Process Termination Verification**:
   Inspected Windows Process Table to confirm PID 10324 and all child processes had cleanly terminated.
3. **Clean On-Demand Respawn**:
   Running `apa chat` automatically spawned a fresh gateway daemon with the patched in-memory bytecode, establishing zero stale state.

---

## 7. Real Runtime Verification Evidence

### 7.1 Single-Turn Live Query Test (`apa chat --oneshot`)

Command executed:
```bash
uv run python apa.py chat -q "Hi, perkenalkan dirimu." --oneshot
```

**Live Output from Gemini 4 Argon via ACP Transport**:
```text
Loaded plugins: apa-security, apa-skills, model-provider-antigravity
* * *
[Thinking Process]
- User is asking for an introduction: "Hi, perkenalkan dirimu."
- Need to introduce myself as APA (Antigravity Personal Agent) or Hermes agent powered by Google Antigravity.
- Respond in Indonesian politely, clearly, and concisely.
* * *
Halo! Saya **APA (Antigravity Personal Agent)**, asisten AI pribadi Anda yang berjalan 
di atas Hermes Agent Runtime dan didukung oleh model canggih **Google Antigravity**.

Saya dirancang untuk membantu Anda dalam berbagai tugas, seperti:
- **Pengembangan Perangkat Lunak & Rekayasa Kode**: Menulis, menganalisis, men-debug, 
  dan mengaudit kode.
- **Eksekusi Alat & Otomasi**: Menjalankan perintah terminal, mengelola file, serta 
  berinteraksi dengan repositori Git secara aman.
- **Manajemen Keterampilan (Skills)**: Menggunakan dan mengintegrasikan berbagai 
  kemampuan khusus melalui sistem APA Skills.
- **Riset & Pemecahan Masalah**: Menemukan solusi teknis, menganalisis data, dan 
  memberikan rekomendasi arsitektur terbaik.

Ada yang bisa saya bantu untuk proyek atau tugas Anda hari ini?
```

**Verification Result**:
- No `TypeError` raised.
- Complete thinking trace and formatted streaming response received.
- Turn exited with code 0.

### 7.2 Multi-Turn Conversation Resumption Test (`--resume latest`)

Command executed:
```bash
uv run python apa.py chat -q "Apa ibukota Indonesia?" --resume latest --oneshot
```

**Live Output**:
```text
Loaded plugins: apa-security, apa-skills, model-provider-antigravity
* * *
[Thinking Process]
- User asks in Indonesian: "Apa ibukota Indonesia?"
- Direct factual question: What is the capital of Indonesia?
- Fact: Jakarta (DKI Jakarta) has historically been the capital, while Nusantara (IKN) 
  in East Kalimantan is currently being developed and transitioned as the new capital city.
* * *
Ibu kota Indonesia saat ini secara hukum dan administrasi utama masih **DKI Jakarta**, 
sementara pemindahan ibu kota negara secara bertahap sedang berlangsung ke **Nusantara (IKN)** 
yang terletak di Penajam Paser Utara dan Kutai Kartanegara, Kalimantan Timur.
```

**Verification Result**:
- Session `local-ab8f0f6ab74826535abea108a9143abd068ad3e42291046e4d0aba2555153b30` successfully resumed.
- Existing ACP transport connection reused without errors.
- Both streaming and context preservation verified end-to-end.

---

## 8. Comprehensive Automated Test Results

### 8.1 Automated Test Execution Summary

| Test Suite File | Tests | Status | Execution Time |
| :--- | :---: | :---: | :---: |
| `tests/plugins/test_apa_antigravity_timeout.py` | 16 / 16 | **PASSED** | 14.88s |
| `tests/agent/test_antigravity_provider.py` | 11 / 11 | **PASSED** | 6.06s |
| `tests/agent/test_acp_client.py` | 5 / 5 | **PASSED** | 8.87s |
| `tests/plugins/test_apa_product_integration.py` | 13 / 13 | **PASSED** | 12.45s |
| `tests/plugins/test_apa_visual_customization.py` | 13 / 13 | **PASSED** | 12.14s |
| **Total Automated Tests** | **58 / 58** | **ALL PASSED** | **54.40s** |

### 8.2 Detailed Timeout Regression Test Cases

The dedicated timeout suite ([test_apa_antigravity_timeout.py](file:///C:/Users/firdaus/APA/tests/plugins/test_apa_antigravity_timeout.py)) covers all standard, edge, and adversarial timeout conditions:

1. `test_request_budget_follows_openai_timeout_contract`: Verifies normalization of `None`, `int`, `float`, and `httpx.Timeout`.
2. `test_streaming_hi_with_hermes_httpx_timeout`: Replicates the exact Hermes `turn_stream.py` execution path.
3. `test_non_streaming_with_httpx_timeout`: Verifies synchronous completion calls.
4. `test_request_timeout_still_raises_acp_error`: Ensures true timeouts raise `AntigravityACPError` without crashing.
5. `test_long_running_turn_uses_whole_request_budget_not_read`: Asserts that write/whole-turn budget is preserved over chunk read timeouts.
6. `test_retry_after_transient_process_crash`: Verifies that timeout values survive subprocess auto-recovery.
7. `test_none_timeout_keeps_transport_default`: Asserts that `timeout=None` cleanly uses the 300.0s transport default.
8. `test_streaming_with_float_timeout`: Confirms float compatibility.
9. `test_direct_stream_completion_with_httpx_timeout`: Tests direct client streaming.
10. `test_direct_create_completion_with_httpx_timeout`: Tests direct client completion.
11. `test_direct_transport_prompt_with_httpx_timeout`: Tests direct transport prompt invocation.
12. `test_direct_transport_request_with_httpx_timeout`: Tests direct transport request dispatch.
13. `test_coerce_timeout_seconds_edge_cases`: Verifies handling of negative values, non-numeric strings, and empty objects.
14. `test_recovery_after_timeout_expiration`: Confirms subsequent requests succeed immediately after a timeout error.
15. `test_cancellation_during_streaming`: Tests graceful cancellation via `client.cancel()`.
16. `test_queue_get_receives_strictly_numeric_float_under_all_inputs`: Hard assertion test proving `waiter.get()` receives strictly numeric `float` or `None`.

### 8.3 Quality Gates & Linter Audits

1. **Ruff Linter**:
   ```bash
   uv run python scripts/check --only ruff
   ```
   *Result*: **1 checks, ok**.
2. **Code Health Linter**:
   ```bash
   uv run python scripts/check --only health
   ```
   *Result*: **0 blocking, 0 advisory**.
3. **Boundary Linter**:
   ```bash
   uv run python scripts/boundary_lint.py --check
   ```
   *Result*: **module_to_core: 839, core_to_module: 20 (PASSED)**.

---

## 9. Hermes Core Invariants Compliance Audit

| Requirement | Policy | Status | Evidence |
| :--- | :--- | :---: | :--- |
| **Hermes Core Modification** | STRICT ZERO | **COMPLIANT** | `git status` shows zero files modified in `agent/`, `hermes_cli/`, `gateway/`, `tools/`, or root. |
| **Monkey Patching** | STRICT ZERO | **COMPLIANT** | No runtime monkey patches or dynamic object mutation. |
| **Global Config Protection** | STRICT ZERO | **COMPLIANT** | `~/.hermes/` global config untouched; APA uses local `.env` and `skins/apa/`. |
| **Boundary Lint Integrity** | ZERO WEAKENING | **COMPLIANT** | Baseline check passes with 0 violations. |
| **Git Working Tree** | NO COMMIT/PUSH | **COMPLIANT** | Zero git commits or pushes executed. |

---

## 10. Final Verdict

### **VERDICT: PASS**

The `TypeError: '<' not supported between instances of 'Timeout' and 'int'` runtime bug has been **definitively investigated, permanently fixed, architecturally fortified with runtime type assertions, cleared of stale daemon artifacts, and 100% verified in live CLI interaction**.
