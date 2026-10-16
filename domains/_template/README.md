# New domain pack

1. Copy this folder to `domains/<name>/`.
2. Edit `domain.toml`: title, extra `[[route]]`s, `[validation].required_evidence`, `[reviewer].hard_topics`.
3. Write `field-notes.md`: what counts as evidence in this field, health checks during runs, practical traps.
4. Put field-specific overlays for core skills in `overlays/<skill-name>.md` (appended after the core overlay).
5. Put field-only skills in `skills/<skill-name>/SKILL.md` (prefer mature open-source skills; record source/commit/license).
6. In a project: set `[project] domain = "<name>"` in autolab.toml, then `autolab skills mount` and `autolab doctor`.

Nothing outside this folder should need to change.
