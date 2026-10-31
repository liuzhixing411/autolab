---
name: novelty-audit
description: Audit whether a research contribution is already in the literature, statement by statement and paper by paper, before compute is spent or a claim is made. Use for an `autolab novelty` audit, a task card of route `novelty`, or a reviewer task named `novelty`.
---

# Novelty audit

An audit answers two questions: what exactly would be new, and how sure can we be after searching.
It cannot prove that nobody did something. It records what was searched, what was read and what the
closest work says. Never write "novel" or "first" anywhere; write what was searched and what differs.

The audit folder is `state/novelty/<N>/`. Scale: `references/novelty-scale.md`. Verdict limits:
`references/verdict-limits.md`. Read both before judging.

## If you are the PI: open and close

1. Reduce the idea to 1-4 **contribution statements**: each a concrete claim with its scope, written so
   a paper could contradict it. "Faster CCL" is not a statement. "Restricting the search seeds to
   neighbours of non-simple deletions keeps partitions exact" is.
2. `autolab novelty new <H-id> --statement "..." [--statement "..."] --type <novelty type>`. The
   command mixes in control statements taken from the lab's reading notes. Hand the student the audit
   id and nothing else: do not paste your statements, and do not say which are real.
3. Dispatch: `autolab task new "Novelty audit N001" --student literature --task novelty --difficulty hard`.
4. When `autolab novelty check <N>` passes: `autolab review run novelty --audit <N>`.
5. A review that lists problems goes back to the student; review the fixed version again. Check any paper
   the reviewer names before you rely on it: read it.
6. `autolab novelty close <N> --verdict PROCEED|CAUTION|ABANDON --closest <L-id> --delta "..."`
   (`--why` for CAUTION). The command enforces the limits. A major issue a revision cannot fix, such as a
   statement too vague to verify, can only close as CAUTION with its id in `--why`. Closing writes
   `state/CONTRIBUTIONS.md` and links the audit on the candidate card. A follow-up candidate with the same
   contribution cites the same audit (`novelty_audit` on its card); a changed statement needs a new audit.

## If you are the student: search, read, judge

Treat every statement in `audit.json` the same. Some are published results used as controls, and you
are not told which.

**Search.** Pass `--audit <N>` on every command. Keyword search alone is not enough: in a test on a
real project it missed the closest comparator that one hop of citation links ranked 10th of 210.

1. `autolab lit snowball --seeds <closest papers> --keywords k1,k2 --audit <N>`. Start from the closest
   work you know; repeat from the best new papers. This is the main search.
2. `autolab lit search "<3-6 keywords>" --audit <N>`, several wordings; long sentences return noise. Use the
   vocabulary of neighbouring fields too: the same mechanism often has another name there.
3. `autolab lit snippets "<a sentence such a paper would contain>" --audit <N>`: passages from paper bodies.
4. Look at the most recent 12 months on purpose (`--since`). A source marked as not answering makes the
   result incomplete: retry it later and say so in the report.
5. Before you judge, read the whole list of every saved search (`literature/searches/<S-id>.json`), low ranks
   too. Papers marked with an L-id count: being in the index does not mean anyone read them. In the first real
   audit the two closest comparators sat at ranks 19 and 46, one of them indexed and unread for weeks.

**Read.** Register what matters (`autolab lit add`), get open copies (`autolab lit fetch --id <L-id>`), and
read the closest papers in full (`literature/text/<L-id>.txt`, reading receipt `autolab lit note`). A paper
you could not read is "not assessed"; it never counts as "does not contain it". Skimming a close comparator
limits what you can conclude: say so.

**Judge.** Append one JSON line per (statement, paper) to `state/novelty/<N>/judgments.jsonl`:

```json
{"sid": "S2", "paper": "L0018", "read_level": "full", "page": 5,
 "quote": "exact words from the paper",
 "types": {"constraint_limitation": {"degree": "derivable_unstated", "known": "...", "introduced": "...", "novel": "..."},
           "phenomenon_effect": null}}
```

- `read_level`: `full`, `skim` or `abstract`. `types` uses the seven keys below; null where the paper says
  nothing on that dimension. `degree`: `explicit_established`, `explicit_peripheral`, `derivable_unstated`,
  `genuinely_new`. `known`, `introduced`, `novel`: one to three dense sentences each.
- Judge the statement as written, clause by clause, not the project around it. Add no restriction it does
  not state; if a clause needs one to be true, say that under risks.
- A degree rates the whole statement. A paper that establishes one clause gets `derivable_unstated` at most,
  and only if the other clauses really follow from it; `known` names the clause. Write the derivation, or
  leave the type null.
- A paper about something else gets null, not `genuinely_new`. That degree means the paper bears on the
  statement and its content does not reach it.
- Check what the quote says before you rate it. An implied guarantee is not an explicit one, and a randomized
  failure is not an exact certificate.
- `explicit_established` or `explicit_peripheral` needs `quote`, copied from `literature/text/<paper>.txt`;
  the checker looks for it there.
- Keys: `phenomenon_effect`, `explanatory_mechanistic`, `unification`, `generalization_scope_expansion`,
  `constraint_limitation`, `conceptual_reframing_abstraction`, `empirical_synthesis_meta_regularity`.

**Report.** Fill `report.md`: what was searched and what could not be read, the closest work for each
statement, the positioning in one sentence a referee could verify, and the risks. Then run
`autolab novelty check <N>`, fix what it lists, and hand off.

## If you are the reviewer

Try to refute the audit. Name published work that already contains a statement, even if it is not in the
list. Check that the search could have found what matters, that degrees fit the quotes, and that the delta
can be verified. Judge under `references/verdict-limits.md`. Say so plainly when an idea clears the check.

## Sources

The scale is copied from Asta's Theorizer (Apache-2.0), which applies it only to the papers a theory was
built from; here it runs on an open search. The verdict limits are copied from ARIS `novelty-check` (MIT).
The procedure, the blind controls and the checks are autolab's own.
