# Personal Weixin: human decisions and major research progress

This optional bridge connects the **existing** Autolab human workflow to a personal
Weixin bot conversation. It uses HTTPS directly, makes no model calls, and needs no
public callback URL or OpenClaw installation. It is an Autolab implementation of the
[Tencent-published iLink protocol](https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol_zh_CN.md),
not a Tencent-supported SDK. The baseline is channel protocol 2.4.8. Account eligibility,
regional routing and real delivery must be verified with your own account.

## What is sent

- Open `00_human/ACTION_NEEDED.md` requests, including decisions, approvals, downloads
  and missing data. A changed request/config/plan produces a new confirmation code.
- New accepted findings and valid experimental outcomes that support or refute a
  hypothesis. Outcomes are explicitly distinguished from accepted findings.
- PI explanations and major milestones emitted with `autolab human notify "text"`.

The first listener cycle silently baselines old findings/outcomes/PI updates, but sends
all open requests. Start it before the research runner. Routine ticks and running-job
polls do not generate messages. The listener must remain running for delivery.

## Linux server or cluster setup

Use the same OS account, home directory and absolute project path for the listener and
PI runner. Follow the site's policy for persistent login-node processes; use a permitted
service node if required. The listener itself is lightweight and does not run experiments.

In the examples, `/path/to/autolab` is the framework checkout and `/path/to/project` is
the research project containing `autolab.toml` (they are usually different directories).

```bash
source /path/to/autolab/scripts/env.sh
cd /path/to/project
autolab skills mount
autolab weixin login
```

Open the displayed login link, scan with your own Weixin account, and confirm on the
phone. If Weixin requests a verification code, type it into that server terminal.
Credentials are saved locally; do not paste them into chat, commit them or send them
to a model. QR links are also sensitive while valid.

Start a persistent listener (for example in a permitted tmux session):

```bash
tmux new-session -s autolab-weixin
bash /path/to/autolab/scripts/run_weixin.sh /path/to/project
```

Send `状态` to the bot from your bound Weixin account to establish the reply context.
Then detach tmux with Ctrl+B, D, and run the normal research runner in its own session:

```bash
bash /path/to/autolab/scripts/run_pi.sh /path/to/project --interval 60
```

Restart an existing PI runner after upgrading so it imports queued replies. No binding
is required when Weixin is unused: the added import step is a no-op.

## Phone interaction

Copy the request number and confirmation code from the notification:

```text
回复 A016 ab12cd34 同意先做小规模对照；不要启动完整扫描
回复 A017 ef56ab78 不同意换方向，先排查边界条件
留言 F003 的提升能否由参数量差异解释？
状态
```

Only the exact QR-bound sender is accepted. Plain `同意` is deliberately insufficient:
several decisions may be pending. Codes expire after seven days and are renewed.
`状态` resends outstanding decision cards. `留言` creates a general human inbox note
without closing any decision request. The PI reads it and uses `human notify` to reply.

For a claim-grade disagreement notification, an explicit response such as
`回复 A018 1234abcd 评级 B` also writes `00_human/verdicts/Cxxx.toml`, binding the
grade to the current claim fingerprint. The normal claim gate still has to run.
Other decisions become inbox instructions; an answer closes a request because it was
answered, **not because the answer grants permission**. A refusal must be respected.

Two acknowledgements distinguish receipt from application:

1. The listener durably queues your reply and acknowledges receipt.
2. The runner imports it between turns; the listener reports whether it was applied or
   rejected because the plan changed. The PI processes the inbox in a subsequent turn.

This does not interrupt a running model turn or cluster job. Latency includes the
current turn, runner interval, and configured human-edit debounce (default five minutes).
Budget exhaustion still stops the runner; replying does not raise `autolab.toml` limits,
remove STOP, restart an exited runner, or bypass experiment/review gates. Files/uploads
and configuration edits still use the normal human workflow. Chat is text-only in this
first version; media and arbitrary command execution are not implemented.

## Delivery and isolation

State, credentials, cursors and reply queues live under
`~/.cache/autolab/weixin/<project-path-hash>/`, outside the research project. Generated
agent settings deny access to this entire directory and hooks reject `autolab weixin`
operator commands. Run `autolab skills mount` after upgrading. This retains Autolab's
existing OS-sandbox boundary, not strong isolation from hostile jobs with your Unix UID.

The listener never writes human inbox files while a PI turn is active. `run_pi.sh` runs
`autolab weixin apply` before each turn's integrity snapshot. Do not invoke `apply`
manually during an active turn. Manual orchestration must import at the same boundary.

Incoming message IDs are deduplicated; replies also bind the request/config/plans and,
for grade disagreements, the claim/evidence fingerprint. Before import these are checked
again. Failed outgoing sends remain pending and retry with the same client ID. A server
delivery followed by a crash can still produce a duplicate notification: no end-to-end
exactly-once guarantee is assumed. Replies themselves are imported idempotently.

Only Tencent iLink HTTPS hosts are accepted; HTTP redirects never receive bearer tokens.
An unfamiliar regional endpoint fails closed and requires review of the host allowlist.
Delivery requires a valid session and context token; autonomous push after a long idle
period is not guaranteed by the client protocol. Send `状态` to refresh context, or stop
the listener and re-login when necessary. A `prepare failed`/`unknown error` send rejection
keeps messages pending and suspends sends until a new authenticated inbound message;
it is not treated as a successful delivery or retried continuously. HTTP acknowledgement
does not prove the phone displayed a message. Stop
the listener and run login again if the session expires. Stop the listener before
re-binding; one listener owns a project. If a process crashes and leaves a lock, confirm
it is dead before manually removing that specific lock directory.

Offline tests cover sender checks, stale/repeated decisions, retries, progress filtering,
follow-up notes, grade verdicts and runner integrity boundaries. Real QR login and
server-to-phone delivery remain deployment checks; passing offline tests does not prove
that a particular Weixin account can connect.
