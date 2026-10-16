# 00_human — the human zone

**First time here? Read `START_HERE.md`.**

This folder is yours. Agents can only read it; the only files they write are `ACTION_NEEDED.md` (things they ask you to do) and `ACK.md` (their receipts for your changes).

| File / folder | Purpose | When to edit |
|---|---|---|
| `GOAL.md` | Overall aim, research questions, success criteria, target venue | Before the start (required); any time later |
| `RULES.md` | Hard rules: what is forbidden, standards that must hold, budget, what needs your approval | Any time; **takes effect immediately**, conflicting tasks are cancelled |
| `TASTE.md` | What you consider a valuable result | Any time; used from the next review on |
| `taste/good/`, `taste/bad/` | ~3 examples each of good and bad results (figures, tables, paragraphs) to calibrate value grading | The earlier the better |
| `inbox/` | Ad-hoc instructions or feedback, one .md file per note. To answer a request from ACTION_NEEDED, put its ID in the file name (e.g. `2026-10-16-A016-answer.md`): the request is then closed automatically | Any time |
| `uploads/` | Your material: experimental data, reference solutions, figures, notes. One subfolder each, with a `meta.toml` (copy `_TEMPLATE/`) | Any time |
| `pdf_drop/` | Papers the agents asked you to download, named by the ID in the request (e.g. `L0007.pdf`) | See ACTION_NEEDED |
| `ACTION_NEEDED.md` | What the agents need from you (downloads, decisions, data). Change `[ ]` to `[x]` when done | Check often |
| `ACK.md` | The PI's receipt for each of your changes: what it understood and changed | Read only |
| `READY` | Create this empty file when the initial input is complete; nothing is dispatched before | Once, at the start |
| `STOP` | Create an empty file named STOP to pause all agents at once; delete it to resume | Emergencies |
| `DELEGATED` | Optional: lets the PI make the decisions RULES reserve for you until a given time (e.g. overnight). TOML: `until = "2026-10-16T08:00:00-04:00"`, `to = "pi"`. Each decision is logged in ACTION_NEEDED for your review | When you will be away |

You don't need to worry about a half-saved edit being read: a change counts only after the file has been untouched for 5 minutes (configurable in autolab.toml).
