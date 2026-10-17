# Execution adapters and the audit event index

The PI runner uses `autolab.execution` to build and launch its backend command.
`[execution] backend = "claude-code"` is the default when omitted. Unknown backends
fail before a turn starts. DSH is not yet implemented. Adding an adapter requires
validation of sandbox/trust, tool policy, usage accounting and cancellation; a
command builder alone does not establish compatibility with a new runtime.

The existing Bash runner retains the 900-second interval, GNU timeout process-group
control, sandbox/trust checks, budget recording and integrity checks. The launch
adapter replaces its process with Claude Code. Execution sidecars under
`state/pi_logs/*.execution.json` record backend, model, state and exit status.
If the host dies before finalization, `running` means unresolved, not successful;
check process/job state before restarting. No automatic task replay is performed.

`state/events.jsonl` is a derived audit index with schema, sequence, event ID,
source, identity and content fingerprint. It indexes experiment outcome history,
claim-gate snapshots, human requests/progress, execution records and sanitized
Weixin delivery/reply status. API keys, context tokens, senders and reply bodies
are not copied from the private Weixin state.

From a project with the Autolab environment active:

```sh
PYTHONPATH="$AUTOLAB_HOME/src" "$AUTOLAB_PYTHON" -m autolab.events sync
PYTHONPATH="$AUTOLAB_HOME/src" "$AUTOLAB_PYTHON" -m autolab.events list
```

Normal tracked CLI calls, execution launch/finalization, Weixin listener cycles
and reply import synchronize the index. This index does not control sends.
Source records commit first. If indexing fails, a later sync discovers those
records. Repeated syncs do not duplicate unchanged records. Logical appends are
committed by atomic replacement under a process lock, so interrupted writes cannot
leave half a JSON line. This currently rewrites/scans the journal and source files;
large deployments will need incremental storage.

This is not full event sourcing or an authorization ledger. Snapshot-only sources
cannot recover every intermediate change made between syncs, and deleted source
files cannot be reconstructed from unobserved history. The index is rebuildable
and does not grant approval, accept findings or trigger repeated external actions.
Existing source records, evidence fingerprints and integrity checks remain
responsible for those decisions.

Design inspiration: [DeepSeek Harness architecture](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md).
No DeepSeek runtime dependency or upstream code is included in this change.
