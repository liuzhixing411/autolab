"""Project layout. Every module gets paths from here so the layout lives in one place."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Layout:
    root: Path

    # human zone (agents read-only, except ACTION_NEEDED.md and ACK.md)
    @property
    def human(self) -> Path:
        return self.root / "00_human"

    @property
    def human_docs(self) -> list[Path]:
        return [self.human / n for n in ("GOAL.md", "RULES.md", "TASTE.md")]

    @property
    def inbox(self) -> Path:
        return self.human / "inbox"

    @property
    def uploads(self) -> Path:
        return self.human / "uploads"

    @property
    def pdf_drop(self) -> Path:
        return self.human / "pdf_drop"

    @property
    def taste_good(self) -> Path:
        return self.human / "taste" / "good"

    @property
    def taste_bad(self) -> Path:
        return self.human / "taste" / "bad"

    @property
    def action_needed(self) -> Path:
        return self.human / "ACTION_NEEDED.md"

    @property
    def ack(self) -> Path:
        return self.human / "ACK.md"

    @property
    def stop(self) -> Path:
        return self.human / "STOP"

    @property
    def ready(self) -> Path:
        return self.human / "READY"

    # shared knowledge
    @property
    def literature(self) -> Path:
        return self.root / "literature"

    @property
    def lit_index(self) -> Path:
        return self.literature / "index.jsonl"

    @property
    def lit_notes(self) -> Path:
        return self.literature / "notes"

    @property
    def wiki(self) -> Path:
        return self.root / "research-wiki"

    @property
    def experiments(self) -> Path:
        return self.root / "experiments"

    @property
    def claims(self) -> Path:
        return self.root / "claims"

    @property
    def rejected(self) -> Path:
        return self.claims / "_rejected"

    @property
    def findings(self) -> Path:
        return self.root / "findings"

    @property
    def experience(self) -> Path:
        return self.root / "experience"

    # machine state
    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def digests(self) -> Path:
        return self.state / "digests"

    @property
    def tasks(self) -> Path:
        return self.state / "tasks"

    @property
    def escalations(self) -> Path:
        return self.state / "escalations"

    @property
    def context_packs(self) -> Path:
        return self.state / "context_packs"

    @property
    def ledger(self) -> Path:
        return self.state / "ledger"

    @property
    def reviews(self) -> Path:
        return self.state / "reviews"

    @property
    def human_snapshot(self) -> Path:
        return self.state / "human_snapshot.json"

    @property
    def uploads_registry(self) -> Path:
        return self.state / "uploads.jsonl"

    @property
    def ids(self) -> Path:
        return self.state / "ids.json"

    # agent runtime
    @property
    def claude(self) -> Path:
        return self.root / ".claude"

    @property
    def skills_mount(self) -> Path:
        return self.claude / "skills"

    @property
    def agents(self) -> Path:
        return self.claude / "agents"

    @property
    def config(self) -> Path:
        return self.root / "autolab.toml"

    def dirs(self) -> list[Path]:
        """Directories `autolab init` creates."""
        return [
            self.inbox, self.uploads, self.pdf_drop, self.taste_good, self.taste_bad,
            self.lit_notes, self.literature / "pdf", self.wiki, self.experiments,
            self.claims, self.rejected, self.findings, self.experience,
            self.digests, self.tasks, self.escalations, self.context_packs,
            self.ledger, self.reviews, self.skills_mount, self.agents,
        ]


def find_root(start: Path | None = None) -> Path:
    """Walk up from `start` to the folder holding autolab.toml."""
    here = (start or Path.cwd()).resolve()
    for p in [here, *here.parents]:
        if (p / "autolab.toml").is_file():
            return p
    raise FileNotFoundError("no autolab.toml found here or above; run `autolab init` first")
