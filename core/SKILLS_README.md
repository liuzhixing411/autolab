# core/skills — the shared skill library

The PI, the students and the reviewer all load skills from this one folder. It contains only mature open-source skills, copied verbatim at pinned commits; the source, commit and licence of each skill are in `MANIFEST.tsv` (see also `THIRD_PARTY_NOTICES.md` at the repository root). Which role uses which skill at which stage is defined in `core/skills.toml`; a domain pack can add routes in `domains/<domain>/domain.toml`.

## Contents

| Source | Count | What for |
|---|---|---|
| ARIS — wanshuiyin/Auto-claude-code-research-in-sleep (MIT) | 45 + `shared-references/` | Choosing directions, experiments, claim review, writing, review-and-revise loops, self-improvement. Dependencies completed from ARIS's own `skill-groups.tsv` |
| K-Dense-AI/scientific-agent-skills (MIT) | 12 | Literature databases, hypotheses, experimental design, statistics, units and uncertainty, compute discovery, citation management, sympy, peer review, scientific visualisation |
| vercel-labs/skills (MIT) | 1 | find-skills: search a registry when no skill fits a task |
| anthropics/skills (Apache-2.0) | 1 | skill-creator: for writing local skills later |
| blader/humanizer (MIT) | 1 | humanizer: removes machine-sounding prose |
| research-paper-writing (MIT, see its LICENSE) | 1 | Paper-writing guidance |

The skills are kept as upstream wrote them (a few ARIS files contain Chinese examples or a Chinese patent-format reference). Field-neutral additions go into `core/overlays/` and domain additions into `domains/<domain>/overlays/`; overlays are appended when a project mounts its skills, so the upstream files stay unchanged.

## How a project mounts them

`autolab init` and `autolab skills mount` copy (or link) the skills a project needs into `<project>/.claude/skills/`, append the overlays, and render the student agents and `CLAUDE.md` with each role's skill list. ARIS skills find their helper scripts through `ARIS_REPO` (set by `scripts/env.sh` to `third_party/aris`, fetched by `scripts/fetch_vendor.sh`). `shared-references/` must sit next to the ARIS skills because they reference it with relative paths.

## Upgrading

1. `scripts/fetch_vendor.sh --all` clones the upstream repositories into `third_party/` at the pinned commits; move a clone to a newer commit to upgrade.
2. Review the diff between the old and new commit, especially `SKILL.md` files and `shared-references/`.
3. Copy the changed skills into `core/skills/`, update the commit in `MANIFEST.tsv`, and run the tests.
4. Tag a release; every machine should use a tagged version so that everyone runs the same skills.
