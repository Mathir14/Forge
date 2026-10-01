# ADR-017: Subprocess Lifecycle Invariants and Stream Completion Semantics

**Status:** Approved  
**Author:** Forge Architecture & Engineering  
**Date:** 2026-09-27  
**Target Version:** Forge 1.1.0  
**Extends:** ADR-014 (Machine Protocol Semantics)  

---

## 1. Context & Incident Analysis

In multiple historical autonomous runs across different underlying execution engines (OpenCode, Antigravity/Agy), stages terminated prematurely before agent work was complete. Symptoms included:

1. **Context Compaction Abort:** An intermediate `step_finish` with `reason="stop"` emitted during LLM context compaction caused the stage to abort before post-compaction generation could begin.
2. **Killed During Tool Execution:** Stages halted while the underlying agent was actively executing tools (e.g. background builds, test suites, or file searches), because the engine interpreted the end of an LLM text turn as task completion.
3. **Intermediate Chatter Saved as Final Artifact:** Statements such as:
   > *"I've started the build and I'm waiting for it to finish."*
   were captured in `03_executor.md` as the final stage artifact because the stage stopped upon the initial text turn.
4. **Adapter Divergence:** While `OpenCodeAdapter` received an ad-hoc patch to track compaction continuation, `AntigravityAdapter` remained unpatched and terminated immediately on any `step_finish (reason=stop)`.

### 1.1 Root Cause Analysis

The root cause across all these incidents is an architectural violation of subprocess lifecycle separation:

- **Conflation of LLM Step Completion with Process Completion:** Language models operate in multi-turn loops. An LLM completing a text delta or turn is **not** the completion of the agent subprocess.
- **In-Stream Premature COMPLETE Emission:** When an adapter yields `AgentEventType.COMPLETE` while reading from the stdout pipe:
  1. [`Stage._execute_stream_events`](file:///home/mathir14/forge/src/forge/stages/stage.py#L241) receives `COMPLETE` and breaks out of its event consumption loop.
  2. Breaking from the generator invokes Python's generator cleanup (`finally:` block).
  3. The adapter's `finally:` block executes `_kill_process_group(proc)`, actively sending `SIGTERM` and `SIGKILL` to the child process and its descendants!
  4. Any pending output in the OS pipe buffer is lost, unread stderr is lost, and the agent process is terminated mid-flight.

---

## 2. Fundamental Subprocess Lifecycle Invariant

We establish an immutable framework-wide invariant governing stage completion:

$$\mathbf{COMPLETE} \iff \begin{cases}
\text{1. Subprocess has terminated} & (proc.poll() \neq None) \\
\text{2. Standard output is fully consumed} & (stream \text{ reached EOF}) \\
\text{3. Standard error is completely drained} & (proc.stderr.read() \text{ completed}) \\
\text{4. Exit code is verified} & (returncode == 0)
\end{cases}$$

### 2.1 State Rules

1. **In-Stream Events Are Informational Only:**
   - Provider events such as `step_finish`, `result`, `complete`, `text`, or `step_update` provide observability, chunk streaming, and metadata collection.
   - An in-stream event **must never** emit `AgentEventType.COMPLETE`.
   - In-stream events may record internal state (e.g. `has_completed_step = True`, session IDs, token usage, or final message candidates), but the generator must continue reading until the underlying stream closes.

2. **Completion Is Strictly Post-Mortem:**
   - `AgentEventType.COMPLETE` is yielded **exclusively after EOF** on standard output and after `proc.wait()` has verified an exit code of `0`.
   - If stdout reaches EOF and the process exits with a non-zero code, the adapter yields `AgentEventType.ERROR`.
   - If stdout reaches EOF but no complete agent response or stopped step was observed, the adapter yields `AgentEventType.ERROR` with descriptive diagnostic metadata.

3. **Multi-Turn Chatter & Compaction Purging:**
   - When an agent generates text in turn $N$ (e.g. *"I will now run tests..."*), followed by a tool invocation in turn $N+1$, the intermediate text from turn $N$ must not contaminate the final deliverable.
   - Starting a subsequent tool call or assistant turn purges accumulated pre-tool conversational text, ensuring only the true final deliverable is preserved in `ExecutionResult.stdout`.

4. **Non-Destructive Normal Process Cleanup:**
   - Normal completion must observe an already-terminated process (`proc.poll() is not None`).
   - The generator `finally:` block must only invoke `_kill_process_group(proc)` if execution was aborted prematurely by an external interrupt (e.g. timeout, user cancellation `SIGINT`, or uncaught exception).

---

## 3. Universal Lifecycle State Machine

```
                   ┌───────────────────────────────┐
                   │   Spawn Subprocess (Popen)    │
                   └──────────────┬────────────────┘
                                  │
                                  ▼
                   ┌───────────────────────────────┐
                   │    Iterate stdout (NDJSON)    │◄────────┐
                   └──────────────┬────────────────┘         │
                                  │                          │
           ┌──────────────────────┼──────────────────────┐   │
           │                      │                      │   │
           ▼                      ▼                      ▼   │
    [type: text]          [type: tool_call]     [step_finish / result]
    Yield CHUNK           Yield TOOL_START      Record metadata &
    Accumulate output     Reset pre-tool text   flag stopped step
           │                      │             (DO NOT YIELD COMPLETE!)
           │                      │                      │   │
           └──────────────────────┼──────────────────────┘   │
                                  │                          │
                                  └──────────────────────────┘
                                  │
                                (EOF)
                                  │
                                  ▼
                   ┌───────────────────────────────┐
                   │ Drain proc.stderr completely  │
                   └──────────────┬────────────────┘
                                  │
                                  ▼
                   ┌───────────────────────────────┐
                   │    proc.wait(timeout=...)     │
                   └──────────────┬────────────────┘
                                  │
                  ┌───────────────┴───────────────┐
         exit_code == 0                  exit_code != 0
                  │                               │
                  ▼                               ▼
    ┌───────────────────────────┐   ┌───────────────────────────┐
    │ Did agent emit complete   │   │ Yield AgentEventType.ERROR│
    │ response/stopped step?    │   │ with captured stderr      │
    └─────────────┬─────────────┘   └───────────────────────────┘
            YES   │   NO
      ┌───────────┘   └───────────┐
      ▼                           ▼
┌───────────────────────────┐ ┌───────────────────────────┐
│ Yield AgentEvent.COMPLETE │ │ Yield AgentEvent.ERROR    │
│ with clean stdout & result│ │ ("Unexpected EOF")        │
└───────────────────────────┘ └───────────────────────────┘
```

---

## 4. Cross-Adapter Implementation Invariants

| Adapter | Old Behavior | New Behavior (ADR-017) |
|---|---|---|
| **`OpenCodeAdapter`** | Emitted `COMPLETE` on in-stream `result`/`complete`. Relied on ad-hoc compaction flag. | Records state in-stream; drains stdout to EOF; yields `COMPLETE` strictly post-mortem after process exit verification. |
| **`AntigravityAdapter`** | Emitted `COMPLETE` on in-stream `step_finish (reason=stop)` or `result`. Murdered subprocess via `_kill_process_group`. | Aligns with universal invariant: processes `step_update` and `step_finish` as state updates; purges intermediate chatter on continuation; waits for EOF and process termination before emitting `COMPLETE`. |
| **`CodexAdapter`** | Blocked via `proc.communicate()`. Non-streaming. | Already adheres to post-mortem invariant via `communicate()`; adopts unified error/result wrapping. |

---

## 5. Consequences & Migration

1. **Resolution of Premature Halts:** Stages will never terminate while underlying tools or compilers are running.
2. **Clean Artifacts:** Intermediate chatter (*"I am waiting for the build..."*) will never become the final stage artifact.
3. **Reliable Subprocess Cleanup:** Process groups are only signaled on genuine aborts, preventing zombie processes without killing legitimate work.
4. **Adapter Portability:** Any future agent adapter (Claude Code, Cursor, Aider) must conform to this exact lifecycle model.
