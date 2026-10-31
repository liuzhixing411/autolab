## autolab: how to search here
Use these commands first; the source table above is secondary. They hold the API keys, queue the calls and leave the record the gates read. The handoff gate requires a `search` and a `snowball` since the task card was written.

1. `autolab lit snowball --seeds L0001,L0002 --keywords k1,k2`: references, citing papers and related works of the closest papers, ranked by how many seeds link to a paper. This is the main search: in a real project it ranked the closest comparator 10th of 210, where keyword search missed it.
2. `autolab lit search "<3-6 keywords>" [--since 2024]`: Semantic Scholar and OpenAlex merged. Long sentences return noise here. Try the vocabulary of neighbouring fields.
3. `autolab lit snippets "<a sentence such a paper would contain>"`: passages from paper bodies; a noisy source, read it for leads.
4. `autolab lit fetch --id <L-id>` (or `--id all`): legal open copies, extracted to `literature/text/`. A PDF that a web tool shows as binary is not unreadable: save it and run `autolab lit update --id <L-id> --pdf <file>`. first-test left a close comparator unread for weeks over this.

`[L0023]` in a result means the paper is already in the index. A `WARNING: no answer from ...` means the result is incomplete: retry later and say so in the handoff. Full results are in `literature/searches/<id>.json`. Register what matters with `autolab lit add`.
