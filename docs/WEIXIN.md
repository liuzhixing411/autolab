# Personal Weixin: human decisions and major research progress

This optional bridge connects the **existing** Autolab human workflow to a personal
Weixin bot conversation. It uses HTTPS directly, makes no model calls except the optional
read-only question answering (see Phone interaction), and needs no
public callback URL or OpenClaw installation. It is an Autolab implementation of the
[Tencent-published iLink protocol](https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol_zh_CN.md),
not a Tencent-supported SDK. The baseline is channel protocol 2.4.8. Account eligibility,
regional routing and real delivery must be verified with your own account.

## What is sent

Phone notifications use concise Chinese: one sentence stating the decision/action
or progress, then `背景：` with necessary evidence and qualifications. The existing
PI produces the copy during its normal turn; the package listener formats and sends
it on the cluster without this desktop chat or an additional model agent.

Use `autolab human summaries` to list source events needing copy, then
`autolab human summarize A029 --summary "请决定是否开始撰写报告。" --background "说明证据、选项、成本与限制。"`.
An exact event key also works for findings/outcomes/PI updates. Copy is bound to the
source version; changing a request invalidates its old copy. Keep summaries below
100 characters and background below 360. New `human notify` messages can directly
use `进展：中文一句话。背景：必要背景。`. Unknown English records produce a short Chinese
notice pointing to the original record, not an invented translation or approval.
PI must fill missing copy each turn. Existing pending records can be summarized
without changing their original evidence or granting approval.

- Open `00_human/ACTION_NEEDED.md` requests, including decisions, approvals, downloads
  and missing data. A changed request/config/plan produces a new confirmation code.
- New accepted findings and valid experimental outcomes that support or refute a
  hypothesis. Outcomes are explicitly distinguished from accepted findings.
- PI explanations and major milestones emitted with `autolab human notify "text"`.

The first listener cycle silently baselines old findings/outcomes/PI updates, but sends
all open requests. Start it before the research runner. Routine ticks and running-job
polls do not generate messages. The listener must remain running for delivery.

Notifications are packed by priority: open decisions first, reply receipts second,
then a progress digest. Each decision shows its deadline and how to answer; its original full
text remains in ACTION_NEEDED.md while the phone shows the concise Chinese copy.
Progress entries use concise Chinese summaries and background; full records stay
in the project. A digest is at most one group every 30 minutes. Any notification
uses at most one message per minute, with up to 2800 characters per chunk. Long
decisions resume across cycles, and are marked sent only after all chunks succeed.
New decisions do not wait for the progress digest window. Queued items that do not
fit remain pending. This reduces consumption of observed per-conversation send
allowances; it cannot replenish an allowance enforced by Weixin.

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
bash /path/to/autolab/scripts/run_pi.sh /path/to/project --interval 900
```

Restart an existing PI runner after upgrading so it imports queued replies. No binding
is required when Weixin is unused: the added import step is a no-op.

## Phone interaction

Write naturally; nothing you send is dropped:

```text
A016 同意先做小规模对照；不要启动完整扫描
同意A017
更正 A017 不同意换方向，先排查边界条件
为什么 E013 停了？
这个方向先放一放，优先真实算例
进展
```

Only the exact QR-bound sender is accepted.

- **Decisions**: a message that names an open request (`A` plus its number, anywhere in the
  text) is the answer to it. No confirmation code is needed (old-style codes are ignored).
  A later message about the same request corrects the earlier one (`更正 A017 …`); the PI
  follows the latest. The receipt repeats how the message was understood.
- **A plan change does not void your answer.** Answers are bound to the request itself. If
  the request text changed after you answered, the PI is told to check that the answer
  still fits. A request that was closed meanwhile (answered or timed out) still receives
  your message as a note, and the PI follows you if it differs from what was done.
- **Questions** (`为什么`, `怎么`, `是否`, a question mark, …) are answered within one or two
  minutes by a read-only model run over the project files (`[weixin.qa]` in autolab.toml:
  model, at most `usd_per_question` per answer and `usd_per_day` per day, charged to the
  budget). It can only read files: no shell, no writes, no web, credentials denied, and the
  project protocol is not loaded. The question also goes to the PI as a note.
- **Anything else** becomes a note to the PI, which replies with `human notify`.
- `进展` / `状态` / `做到哪了` returns the latest recorded progress; `状态` also resends
  open decision cards.

Unanswered blocking requests are carried out after `[autonomy] decision_timeout_hours`
(default 4) with the PI's recommendation, except the keys in `[autonomy] always_ask`; each
card shows the deadline, and you are reminded every `reminder_hours`.

For a claim-grade disagreement notification, an explicit response such as
`A018 评级 B` also writes `00_human/verdicts/Cxxx.toml`, binding the grade to the claim
version you were shown; if the claim changed after the notification, the grade is not
recorded (you are told) and your message is delivered as a note. The normal claim gate
still has to run. An answer closes a request because it was answered, **not because the
answer grants permission**. A refusal must be respected.

Two acknowledgements distinguish receipt from application:

1. The listener durably queues your message and acknowledges receipt.
2. The runner imports it between turns and reports that it reached the PI.

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
period is not guaranteed by the client protocol. A `prepare failed`/`unknown error`
rejection keeps the individual message pending with automatic exponential backoff
(60 seconds up to one hour). No new `状态` message is required to resume attempts.
The failed group and all original items remain pending; acknowledged chunks are checkpointed.
Failed sends impose a global backoff to avoid spending requests on the same rejected
conversation. Other eligible groups are reconsidered after the backoff.
A fresh authenticated inbound message makes pending items eligible immediately.
HTTP acknowledgement (`sent` in the internal queue) means gateway acceptance only,
not proof that the phone displayed the message. There is no delivery receipt API here.

**This channel alone is not a guaranteed unattended alert channel.** Upstream reports
[309](https://github.com/Tencent/openclaw-weixin/issues/309) and
[286](https://github.com/Tencent/openclaw-weixin/issues/286) describe context-dependent
send windows; their durations differ and are not a documented service contract.
The published protocol provides no explicit context renewal endpoint. Automatic
retry repairs transient failures but cannot extend a server-expired context. For
important unattended alerts, configure a separately authorized push service or
channel that supports proactive delivery; this bridge does not silently forward
research data to a third party. Keep Weixin for authenticated decision replies.

Stop the listener and run login again if the session expires. Stop the listener before
re-binding; one listener owns a project. If a process crashes and leaves a lock, confirm
it is dead before manually removing that specific lock directory.

Offline tests cover sender checks, stale/repeated decisions, retries, progress filtering,
follow-up notes, grade verdicts and runner integrity boundaries. Real QR login and
server-to-phone delivery remain deployment checks; passing offline tests does not prove
that a particular Weixin account can connect.
