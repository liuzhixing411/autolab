+++
statement = "<<FILL: Under [conditions], [factor] changes [outcome] by [magnitude ± uncertainty] vs [reference].>>"
experiments = ["<<FILL: E001>>"]
evidence = ["<<FILL: experiments/E001/results/metrics.json>>"]
pi_grade = "<<FILL: A|B|C|D>>"
central = false

[validation]
# keys required by the domain pack (domain.toml → [validation].required_evidence), value = path
+++

## Closest prior work
<!-- Required by the claim gate. One row per closest work: | L-id | what it does | mechanism difference | where our evidence shows the difference |. If none was found, write "none found" and name the search log (queries, databases, dates) that shows it. -->

## Scope tested vs scope claimed
<!-- What was tested, what is claimed, and explicitly what is NOT claimed. The reviewer marks unverified items
     outside the claimed scope as `claimed: false`; those are kept as disclosed limits and do not block the gate. -->

## Boring explanations considered (artifact, bug, confound, unequal tuning, selection)

## Why it matters (so-what chain)
