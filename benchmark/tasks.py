"""Load, validate and hash a versioned task suite.

Layout: <suite>/suite.json, <suite>/public/*.json, <suite>/private/*.json.
Each task file holds a JSON array of tasks.
"""

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from .common import read_json

SCORING_BY_CATEGORY = {
    "coding": "tests",
    "extraction": "fields",
    "reasoning": "answer_key",
    "document": "document",
    "tool_use": "tool_state",
    "writing": "human",
}
CATEGORY_LABELS = {
    "coding": "Coding",
    "extraction": "Data extraction",
    "reasoning": "Reasoning and math",
    "document": "Long-document analysis",
    "tool_use": "Tool use",
    "writing": "Writing",
}
CATEGORIES = list(SCORING_BY_CATEGORY)
DIFFICULTIES = {"easy", "medium", "hard"}
TARGET_TASKS = {"coding": 40, "extraction": 40, "reasoning": 40, "document": 40, "tool_use": 40, "writing": 20}
TARGET_PRIVATE_SHARE = 0.25
TARGET_SENTINELS = 20

REQUIRED_FIELDS = {
    "tests": ["test_code", "reference_solution"],
    "fields": ["expected"],
    "answer_key": ["answers"],
    "document": ["document", "answers"],
    "tool_state": ["tools", "initial_state", "expected_state", "max_steps"],
    "human": ["rubric"],
}


def repeats_for(category):
    return 1 if category == "writing" else 3


@dataclass
class Task:
    id: str
    category: str
    difficulty: str
    prompt: str
    visibility: str
    system: str = ""
    sentinel: bool = False
    trap: bool = False
    reviewed_by: str = ""
    spec: dict = field(default_factory=dict)

    @property
    def scoring(self):
        return SCORING_BY_CATEGORY[self.category]

    @property
    def repeats(self):
        return repeats_for(self.category)

    def full_prompt(self):
        if self.scoring == "document":
            return f"{self.prompt}\n\n<document>\n{self.spec['document']}\n</document>"
        return self.prompt


@dataclass
class Suite:
    path: Path
    version: str
    tasks: list
    public_hash: str
    private_hash: str

    def by_id(self):
        return {t.id: t for t in self.tasks}


def hash_directory(path):
    """Hash file names and contents, ignoring line-ending differences."""
    path = Path(path)
    digest = hashlib.sha256()
    if path.exists():
        for file in sorted(p for p in path.rglob("*") if p.is_file()):
            digest.update(file.relative_to(path).as_posix().encode("utf-8") + b"\0")
            digest.update(file.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()


def _load_task(raw, visibility, base_dir):
    spec = dict(raw)
    if "document_file" in spec:
        spec["document"] = (base_dir / spec["document_file"]).read_text(encoding="utf-8")
    return Task(
        id=spec.pop("id"),
        category=spec.pop("category"),
        difficulty=spec.pop("difficulty"),
        prompt=spec.pop("prompt"),
        visibility=visibility,
        system=spec.pop("system", ""),
        sentinel=spec.pop("sentinel", False),
        trap=spec.pop("trap", False),
        reviewed_by=spec.pop("reviewed_by", ""),
        spec=spec,
    )


def load_suite(path):
    path = Path(path)
    version = read_json(path / "suite.json")["version"]
    tasks = []
    for visibility in ("public", "private"):
        for file in sorted((path / visibility).glob("*.json")):
            tasks.extend(_load_task(raw, visibility, file.parent) for raw in read_json(file))
    return Suite(path, version, tasks, hash_directory(path / "public"), hash_directory(path / "private"))


def validate(suite):
    """Return a list of problems that make the suite unusable."""
    problems = []
    seen = set()
    for task in suite.tasks:
        if task.id in seen:
            problems.append(f"{task.id}: duplicate task id")
        seen.add(task.id)
        if task.category not in SCORING_BY_CATEGORY:
            problems.append(f"{task.id}: unknown category {task.category!r}")
            continue
        if task.difficulty not in DIFFICULTIES:
            problems.append(f"{task.id}: unknown difficulty {task.difficulty!r}")
        for name in REQUIRED_FIELDS[task.scoring]:
            if name not in task.spec:
                problems.append(f"{task.id}: missing {name!r}")
        if task.scoring == "answer_key" and "ANSWER:" not in task.prompt:
            problems.append(f"{task.id}: prompt must ask for a final 'ANSWER:' line")
        if task.scoring == "tool_state":
            for tool in task.spec.get("tools", []):
                if tool.get("op") not in {"read", "write", "list"}:
                    problems.append(f"{task.id}: tool {tool.get('name')!r} has an unknown op")
    return problems


def readiness(suite):
    """Compare a suite with the published protocol's targets; returns (summary, gaps)."""
    counts = {c: sum(t.category == c for t in suite.tasks) for c in CATEGORIES}
    total = len(suite.tasks)
    private = sum(t.visibility == "private" for t in suite.tasks)
    sentinels = sum(t.sentinel for t in suite.tasks)
    unreviewed = [t.id for t in suite.tasks if not t.reviewed_by]
    gaps = [f"{c}: {counts[c]} of {TARGET_TASKS[c]} tasks" for c in CATEGORIES if counts[c] < TARGET_TASKS[c]]
    if total and abs(private / total - TARGET_PRIVATE_SHARE) > 0.02:
        gaps.append(f"private share is {private / total:.0%}, target is {TARGET_PRIVATE_SHARE:.0%}")
    if sentinels != TARGET_SENTINELS:
        gaps.append(f"{sentinels} sentinel tasks, target is {TARGET_SENTINELS}")
    if unreviewed:
        gaps.append(f"{len(unreviewed)} tasks have no second reviewer")
    summary = {"total": total, "private": private, "sentinels": sentinels, "by_category": counts}
    return summary, gaps
