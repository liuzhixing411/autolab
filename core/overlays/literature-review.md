## autolab: literature reviews in a project
- The review lives in `literature/REVIEW.md`; keep the search log (databases, exact search strings, dates, result counts) in `literature/search_log.md`, and raw search output under `literature/sources/`.
- Register every paper with `autolab lit add` / `autolab lit update` (IDs L####, tier, role, access). Papers you cannot get legally go to the human through the download request that `autolab lit add` creates; never use shadow libraries.
- Full texts: `autolab lit text <L>`; every must-read paper needs a reading receipt `literature/notes/<L>.md` with page-exact key points.
- `parallel-cli` and the AI schematic generator need keys this lab does not have: skip them. Use the bundled `verify_citations.py` (Crossref) and the openalex / semantic-scholar / arxiv skills for searching.
- For each related work state precisely what it does and how this project differs; do not claim novelty from absence of evidence (log the queries that found nothing).
