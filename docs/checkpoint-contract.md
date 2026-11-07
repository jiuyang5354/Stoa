# Checkpoint and continuation contract

This is a narrow hardening of the existing SQLite ledger, not a framework migration
or an exactly-once guarantee for arbitrary external effects.

## Step execution

With checkpointing enabled (the default):

- Failure to establish a run stops before any model or tool dispatch. It does not
  silently disable checkpointing. Explicit `AGENT_CHECKPOINT=off` is unchanged.
- A missing step is different from a failed database read or malformed checkpoint.
  Read failures raise `CheckpointError`; they are not cache misses.
- A completed tool step is replayed only when kind, name, arguments and the result
  field agree. A conflicting step is not overwritten by a newly dispatched tool.
- Tools in `SIDE_EFFECT_TOOLS` first atomically claim their `(run_id, step)` slot
  with `tool_pending`. Only the insert winner may dispatch. Completion replaces the
  marker with the recorded result. A pending marker alone never authorizes a retry.
- Result persistence and updating the run timestamp share one SQLite transaction.
  Failure rolls back both; a previously committed pending marker survives.
- Step writes only complete a matching pending intent or accept an identical stored
  result. A late model/read-only result cannot replace a different plan or intent.
- New asks and resumes hold a nonblocking OS lock for the same run through final
  status persistence. Process exit releases ownership; the losing executor does not
  modify the owner's status. Lock files are not unlinked, avoiding split ownership.
- Failed result persistence or an exception after an effect was dispatched pauses
  the run rather than feeding an ordinary tool error back into the planning loop.
- Model checkpoint failures also propagate, so an unrecorded plan cannot silently
  proceed to effects. The direct-debate shortcut has the same intent/pause rule.

When the run row can be updated, checkpoint uncertainty is recorded as `unknown`.
`interrupted()` does not include `unknown`, and `Agent.resume()` refuses it. If the
ledger remains inaccessible, status persistence itself can fail: the exception still
stops execution, and an existing intent marker remains the protection on reopening.
There is deliberately no automatic 'clear unknown and retry' action. Reconcile the
actual operation before deciding what to do next.

The task runner propagates checkpoint errors rather than falling back to a new chat.
Other resume errors also retain the original run instead of silently restarting it.
If a discovered interrupted run finishes before resume acquires ownership, the task
returns its stored completion or explicit stopped/partial status without rewriting
that terminal record. This does not prevent a later independent scheduled run.
It also checks unresolved session intents on later attempts, beyond the usual resume
age window; pruning preserves unresolved records. Explicit cancellation remains a
separate user decision. Composite deep analysis owns one outer checkpoint: internal
calls do not inherit its thread-local step, and fatal checkpoint/cancellation signals
propagate through debate wrappers instead of becoming ordinary tool output.

## Partial answers and continuation

Hard-timeout answers are `partial`, not `done`; they are not automatically resumed.
The user may explicitly continue. Restarted continuation reads the real `learn.log`
field, matches the trace to the last assistant message, and reapplies existing
sanity/taint checks. In-memory continuation also reapplies taint before write guards.
Persisted observations are labelled historical and potentially clipped, preserving
source/time fields rather than presenting them as freshly retrieved facts.

## Verification and limits

`tests/test_longrun.py` covers unreadable/wrong checkpoints, failed persistence,
unknown dispatch outcomes, competing claims, transaction rollback, scheduled-task
re-entry, nested/direct-debate failure propagation, and late conflicting writes.
Real child-process tests exercise ask/resume exclusion and lock release after killing
the owner. Two additional child-process tests call `os._exit`
before/after a synthetic local file append, then exercise `Agent.resume()`; the
pending effect is not dispatched again. This is real process termination, not a
power-loss test or a live webhook test.

The Windows `msvcrt` lock path is exercised locally; the Unix `flock` path requires
the Linux CI job. This is local-host ownership, not a cross-host lock service.

`tests/test_turn_timeout.py` covers partial status, both continuation paths, taint
restoration, and rejection of an unrelated older trace.

Important limits:

- A crash after intent commit but before dispatch can pause an operation that never
  happened. This deliberately trades automatic completion for duplicate prevention.
- The guarantee is scoped to the same run/step and the existing declared effect-tool
  set. A new run or an unclassified extension tool is not globally deduplicated.
- Existing pre-upgrade records have no pre-dispatch marker for their old crash window.
- Business writes and the ledger are not one distributed transaction. Remote
  notifications still use the existing send-then-mark outbox; receiver idempotency,
  receipt reconciliation and its expiry contract are separate unfinished work.
- Trace continuation is not a full raw-result archive or a paginated recovery API.
- These tests do not measure real-model answer quality, cost, or investment returns.
