#!/usr/bin/env bash
# Real-cluster interface check: init a throwaway project, pass the knowledge gate,
# and dry-run the job through the real scheduler (sbatch --test-only). Submits nothing.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACCOUNT="${1:?usage: cluster_check.sh <slurm-account> [site] [domain]}"
SITE="${2:-negishi}"
DOMAIN="${3:-cfd}"
AL="$HERE/scripts/autolab"
P="$(mktemp -d "${CLUSTER_SCRATCH:-${TMPDIR:-/tmp}}/autolab-check-XXXX")/proj"
"$AL" init "$P" --domain "$DOMAIN" --site "$SITE" >/dev/null
cd "$P"
for f in GOAL RULES TASTE; do printf '# %s\nfilled for cluster check\n' "$f" > "00_human/$f.md"; done
sed -i "s/^account = \"\"/account = \"$ACCOUNT\"/; s/^require_clean_git = true/require_clean_git = false/" autolab.toml
"$AL" lit add --title "Bubble breakup in jets" --doi 10.0/check --tier B --role theory --access oa --keywords bubble,jet >/dev/null
CP=$("$AL" preflight "bubble jet check" --keywords bubble,jet | cut -d: -f1)
mkdir -p experiments/E001
cat > experiments/E001/plan.md <<PLAN
+++
id = "E001"
context_pack = "$CP"
hypothesis = "cluster check"
prediction = "job script accepted by the scheduler"
decision_rule = "if accepted the interface works"
budget_hours = 0.1
run_command = "echo hello"

[resources]
partition = "cpu"
cpus = 1
mem_gb = 1
hours = 0.1
+++

## Prior knowledge consulted
- L0001: background only for the interface check, no influence on this dry run
PLAN
echo "== gate plan";      "$AL" gate plan E001
echo "== slurm validate"; "$AL" slurm validate E001
echo "== tick";           "$AL" tick
echo "== doctor";         "$AL" doctor || true
echo "project left at $P"
