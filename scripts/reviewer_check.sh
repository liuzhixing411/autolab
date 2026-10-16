#!/usr/bin/env bash
# Real-reviewer interface check: builds a throwaway CFD project with one claim and runs
# one review per tier (light / regular / deep) through Codex, then the claim gate.
# The numbers are SYNTHETIC and labelled as such; this checks the interface, not science.
#   usage: reviewer_check.sh setup <slurm-account>        → prints the project path
#          reviewer_check.sh run <project> light|regular|deep|gate|routes
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AL="$HERE/scripts/autolab"

setup() {
  local P; P="$(mktemp -d "${CLUSTER_SCRATCH:-${TMPDIR:-/tmp}}/autolab-review-XXXX")/proj"
  "$AL" init "$P" --domain cfd --site "${AUTOLAB_SITE:-negishi}" >/dev/null
  cd "$P"
  for f in GOAL RULES TASTE; do printf '# %s\nInterface check project (synthetic data).\n' "$f" > "00_human/$f.md"; done
  sed -i "s/^account = \"\"/account = \"$1\"/; s/^require_clean_git = true/require_clean_git = false/" autolab.toml
  "$AL" lit add --title "Schiller-Naumann drag correlation for spheres" --doi 10.0/sn-check --tier B \
     --role benchmark --access oa --keywords sphere,drag >/dev/null
  local CP; CP=$("$AL" preflight "sphere drag low Reynolds" --keywords sphere,drag | cut -d: -f1)
  mkdir -p experiments/E001/results
  cat > experiments/E001/plan.md <<PLAN
+++
id = "E001"
context_pack = "$CP"
hypothesis = "Steady drag on a sphere at Re=100 is mesh-converged to <0.5% on the fine mesh"
prediction = "Cd within 2% of the Schiller-Naumann value 1.093"
decision_rule = "if GCI_fine < 0.5% use fine mesh for the study, else refine"
budget_hours = 1.0
run_command = "echo synthetic"

[resources]
partition = "cpu"
cpus = 1
mem_gb = 1
hours = 0.1
+++

## Prior knowledge consulted
- L0001: correlation used as the benchmark value; it is an empirical fit (about 5% scatter), so agreement within 2% is the target, not 0.2%.
PLAN
  cat > experiments/E001/results/metrics.json <<'J'
{"note": "SYNTHETIC numbers for an interface check", "Re": 100,
 "meshes": [{"name": "coarse", "cells": 250000, "Cd": 1.121},
            {"name": "medium", "cells": 2000000, "Cd": 1.101},
            {"name": "fine", "cells": 16000000, "Cd": 1.096}],
 "Cd_extrapolated": 1.0943, "schiller_naumann_Cd": 1.093}
J
  cat > experiments/E001/results/mesh_convergence.json <<'J'
{"note": "SYNTHETIC", "refinement_ratio": 2.0, "values": [1.121, 1.101, 1.096],
 "observed_order": 2.0, "extrapolated": 1.0943, "gci_fine": 0.0019, "asymptotic_ratio": 1.005}
J
  cat > experiments/E001/report.md <<'R'
# E001 report: sphere drag at Re = 100 (SYNTHETIC numbers, interface check)

Steady incompressible flow past a sphere at Re = 100 on three systematically refined meshes
(refinement ratio 2 in each direction). Drag coefficients: 1.121 (coarse), 1.101 (medium),
1.096 (fine). Observed order 2.0, Richardson-extrapolated Cd = 1.0943, GCI on the fine mesh 0.19 %.
The Schiller-Naumann correlation gives 1.093 at Re = 100.

Domain: spherical outer boundary at 20 diameters. Second-order schemes. Residuals fell 6 orders.
R
  "$AL" gate plan E001 >/dev/null
  mkdir -p claims/C001
  cat > claims/C001/claim.md <<'C'
+++
statement = "For steady flow past a sphere at Re = 100, the solver gives a mesh-converged drag coefficient Cd = 1.094 (Richardson-extrapolated, GCI_fine 0.19%), within 0.2% of the Schiller-Naumann correlation (1.093)."
experiments = ["E001"]
evidence = ["experiments/E001/results/metrics.json", "experiments/E001/report.md"]
pi_grade = "C"
central = false

[validation]
mesh_convergence = "experiments/E001/results/mesh_convergence.json"
+++

## Scope tested vs scope claimed
One Reynolds number, one domain size, steady solver.

## Boring explanations considered
Outer-boundary blockage not varied; the correlation itself has a few percent scatter.

## Why it matters
Verification step before the main study; supports using the fine mesh.
C
  echo "$P"
}

run() {
  cd "$1"
  case "$2" in
    routes)
      for t in format_check section_review claim_support; do "$AL" review route "$t" | tr -d '\n '; echo; done
      "$AL" review route section_review --text "cavitation bubble collapse" | tr -d '\n '; echo ;;
    light)   "$AL" review run format_check claims/C001/claim.md \
               --instructions "Check only formatting and wording of the claim statement (units, notation, clarity). Do not judge correctness." ;;
    regular) "$AL" review run section_review experiments/E001/report.md \
               --instructions "Review this short report as a section of a CFD paper: clarity, missing setup details, reproducibility." ;;
    deep)    "$AL" review run claim_support --claim C001 experiments/E001/results/metrics.json \
               experiments/E001/results/mesh_convergence.json experiments/E001/report.md \
               --instructions "Judge whether the evidence supports the claim as stated, and grade its value A-D. Note the data are labelled synthetic; judge internal consistency and support." ;;
    gate)    "$AL" gate claim C001 || true ;;
  esac
}

"$@"
