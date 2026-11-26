# APA Phase 2.12.7 — Streaming & Runtime Reliability Validation

## 1. Executive Summary

Live reliability testing was conducted against the Google Antigravity provider (`plugins/model-providers/antigravity/`) running inside the local Hermes runtime (`C:\Users\firdaus\APA`, Python `3.14.9`) on Windows 10/11 x64 with `C:\Users\firdaus\AppData\Local\agy\bin\agy.exe` (`v1.5.9`).

- **Process Crash Recovery**: **PASS** (PID `8768` terminated -> automatically respawned as PID `1644` with a new ACP session `834a612f-6294-4984-8dd7-58f097e6e297` and returned `RECOVERED_OK`).
- **ACP Process Cleanup (Orphan/Zombie Check)**: **PASS** (`orphan_process_after_close: false`; `client.close()` cleanly terminates the `agy.exe` child process).
- **Post-Cancellation Session Recovery**: **PASS** (`POST_CANCEL_OK` returned on the very next turn after `cancel()`).
- **Fallback Activation (`agy -p` Print Mode)**: **PASS** (simulated ACP transport failure before output triggered `agy -p` fallback exactly once without duplicate requests, returning `PRINT_FALLBACK_LIVE_OK`).
- **Mid-Stream Cancellation Edge Case**: **PARTIAL (Minor Bug Found & Documented per Rule 14 — No Auto-Edit Applied)**. When `session/cancel` interrupts a streaming turn (`stream=True`), `agy acp --stdio` resolves the turn with `stopReason: "cancelled"`, which `make_stream_chunk` passes raw into OpenAI's `ChunkChoice(finish_reason=...)`, raising `pydantic.ValidationError` instead of normalizing `"cancelled"` to `"stop"` as `build_chat_completion` already does.

---

## 2. Live Reliability Test Matrix & Evidence

| Test Scenario | Measured Runtime Evidence | Status |
|---|---|---|
| **1. Initial ACP Handshake & Session Creation** | Spawned `agy.exe acp --stdio` (`PID 8768`), created session `6936b421-9446-4de0-92fb-aa35679cc13d` | **PASS** |
| **2. Subprocess Crash Recovery** | Terminated `PID 8768` mid-session; next `client.chat.completions.create` detected dead process, spawned `PID 1644`, created session `834a612f-6294-4984-8dd7-58f097e6e297`, and returned `"RECOVERED_OK"` | **PASS** |
| **3. Mid-Stream Cancellation (`session/cancel`)** | Sent `session/cancel` 0.4s into a streaming turn. `agy.exe` stopped generation immediately and remained responsive, but `make_stream_chunk` raised `pydantic.ValidationError` on `finish_reason="cancelled"` (see Section 3 below) | **PARTIAL** |
| **4. Post-Cancellation Session Continuity** | Immediately after cancellation, sent a new prompt on the same client/session (`rel-crash-sess`); model replied `"POST_CANCEL_OK"` without session desynchronization | **PASS** |
| **5. ACP Subprocess Cleanup (`client.close()`)** | Verified `proc.poll() is not None` after `client.close()`; `orphan_process_after_close = False` (0 zombie/orphan `agy.exe` processes) | **PASS** |
| **6. Print-Mode Fallback (`agy -p`)** | Simulated `AntigravityACPError` before output emission; client logged fallback once, invoked `agy -p --output-format json`, and returned `"PRINT_FALLBACK_LIVE_OK"` without duplicate requests | **PASS** |
| **7. Duplicate Request / Duplicate Tool Call Guard** | Verified in `client.py:L349-355` and `test_antigravity_provider.py`: fallback only triggers when no output was emitted, preventing duplicate side effects | **PASS** |

---

## 3. Bug Report (Validation-First Rule — Awaiting Approval Before Code Change)

### Bug ID: `AGY-STREAM-CANCEL-01`
- **Symptom**: When `client.cancel()` / `transport.cancel()` is invoked during a streaming turn (`stream=True`), `agy acp --stdio` finishes the prompt RPC with `{"stopReason": "cancelled"}`. At stream finalization, `ChunkChoice` raises `pydantic_core._pydantic_core.ValidationError` because OpenAI's `ChunkChoice.finish_reason` type only allows `Literal['stop', 'length', 'tool_calls', 'content_filter', 'function_call'] | None`.
- **Root Cause**:
  In `plugins/model-providers/antigravity/bridge.py:L296-297`, `build_chat_completion` (used for `stream=False`) normalizes non-OpenAI `finish_reason` values:
  ```python
  if resolved_finish not in ("stop", "length", "tool_calls", "content_filter", "function_call"):
      resolved_finish = "tool_calls" if resolved_tools else "stop"
  ```
  However, `make_stream_chunk` (`plugins/model-providers/antigravity/bridge.py:L332-358`) and `_finalize_stream_chunks` (`plugins/model-providers/antigravity/client.py:L205-214`) pass `acc_result.stop_reason` directly into `ChunkChoice(finish_reason=finish_reason)` without that normalization check.
- **Affected Files**:
  - `plugins/model-providers/antigravity/bridge.py` (lines 332–358)
  - `plugins/model-providers/antigravity/client.py` (line 205)
- **Recommended Fix (2 lines in `bridge.py`)**:
  In `make_stream_chunk` (`plugins/model-providers/antigravity/bridge.py:L348`), normalize `finish_reason` before passing it to `ChunkChoice`:
  ```python
  if finish_reason is not None and finish_reason not in ("stop", "length", "tool_calls", "content_filter", "function_call"):
      finish_reason = "tool_calls" if tool_calls else "stop"
  ```
- **Compliance Note**: Per Section 14 ("Jangan melakukan perubahan source code untuk memperbaiki masalah secara otomatis... Tunggu persetujuan sebelum mengubah kode"), no source code was modified.
