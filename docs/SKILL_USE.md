# Why skills go unused, and how autolab makes them run

Status: implemented in 0.7. Evidence: first-test, 171 ticks, 206 turn logs, 178 task cards.

## What was measured

- 16 of the 67 skills were ever invoked (397 calls). All six most used were written into the task card:
  `verification-before-completion` 138, `analyze-results` 54, `test-driven-development` 44, `run-experiment` 39,
  `experiment-plan` 36, `statistical-power` 36.
- Nothing on a menu was used, with few exceptions. `novelty-check`, `hypothesis-generation`, `idea-creator`,
  `semantic-scholar`, `openalex`, `paper-lookup` and `kill-argument`: 0 calls. `brainstorming-research-ideas`: 1.
- Every skill was visible: all 99 names (67 lab skills, 32 others) were in the session's skill list.

So the model did not lack access. An instruction to "invoke any that fit" does not produce invocations.

## Causes

| Cause | Evidence | Fix |
|---|---|---|
| Unknown task names pass silently | 28 cards named `writing`, `plan`, `analyze`, `draft`, `implement` or `claim`, which have no route, and got only the base skill | `autolab task new` refuses them and lists the valid tasks; routes added for `writing` and `plan` |
| A route with a stage was missed without `--stage` | `section` tasks; `paper-write` ran once | `skills._matches`: a task finds its route, the stage only narrows it |
| No trigger | "When a direction fails, brainstorm" was a protocol sentence; one call in 171 ticks | `due.py`: the situation is computed from the records and stops new plans until handled |
| Menus instead of situations | "More skills (invoke any that fit)" | Route `when` tables: the agent file names the skill and the situation; no menu |
| Check stops at the call | `research-lit` was invoked, then the web was searched by hand | Products are checked: `autolab lit` rows in the search log, ideation record and review, quotes in the audit |
| Broken skills | 29 skills call `mcp__codex__codex`, absent here; `novelty-check` searches ML venues | `_codex-mcp.md` overlay at mount; `autolab skills doctor`; `novelty-audit` replaces `novelty-check` |
| Listing budget | Claude Code keeps names but drops descriptions past 1 % of the context window, least-invoked skills first. Lab descriptions alone are 21,813 characters, so unused skills lose the text that could get them used | Spare skills are `name-only` in `skillOverrides`; `skillListingBudgetFraction = 0.02` |

The listing effect is documented behaviour; whether it fired in first-test cannot be read from the logs.

## The rule

A skill is mounted for one of five reasons, computed by `skills.activation` and shown by `autolab skills report`:

| Use | Meaning | How it runs |
|---|---|---|
| `gate` | required by a route or a due rule | task card, then the handoff, plan or claim gate reads the logs |
| `situation` | named with its condition in a route's `when` | agent file or CLAUDE.md; `surprise = true` makes `systematic-debugging` a gate |
| `reviewer` | routed to the reviewer | handed over by the review pack |
| `human` | for the human | listed by name only |
| `library` | a dependency of another skill, or spare | listed by name only |

To add a skill, decide which row it belongs to. A skill that is only "available" is a `library` skill: it stays
callable and costs almost nothing, but expect it not to run.

## Reading the numbers

`autolab skills report` lists calls per skill. A `gate` skill with no calls means its trigger is broken. A
`situation` skill with no calls is a question for the retrospective: the situation never arose, or the agents
missed it. `autolab report metrics` carries `forced_never_invoked`.
