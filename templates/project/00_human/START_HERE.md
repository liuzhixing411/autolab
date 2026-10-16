# Start here (initial human input)

Do these 5 steps in order; nothing is dispatched until they are done. In every **[REQUIRED]** place, replace the whole `<<FILL: ...>>` with your content (while `<<FILL` remains, the file counts as unfilled).
Each file ends with a filled-in example inside an HTML comment; the system ignores it.

| Step | File | What to write | Time |
|---|---|---|---|
| 1 | `GOAL.md` | Research questions, success criteria, scope, starting point | 10 min |
| 2 | `RULES.md` | Forbidden actions, required standards, what needs your approval, budget | 5 min |
| 3 | `TASTE.md` | What counts as valuable; examples in `taste/good/` and `taste/bad/` (can come later) | 5 min |
| 4 | `uploads/` | Data or reference results you already have: one subfolder each + `meta.toml` (copy `_TEMPLATE/`). Skip if none | varies |
| 5 | Create the empty file `READY` | Means "input complete, go". Without it the PI only tidies state and dispatches nothing | 1 s |

```bash
touch 00_human/READY
```

After the start you can edit GOAL / RULES / TASTE at any time (a change counts after the file has been still for 5 minutes; the PI confirms in `ACK.md`), drop notes into `inbox/`, or create `STOP` to pause everything.
