"""The data dictionary resource: every dataset with its columns and comments (from the catalogue),
preceded by the hand-written domain rules."""

from __future__ import annotations

from pathlib import Path

from .catalog import Catalog

RULES_PATH = Path(__file__).with_name("domain_rules.md")


def render(catalog: Catalog) -> str:
    parts = ["# Data dictionary — La Carte des Colocs analytics schema", "", RULES_PATH.read_text(encoding="utf-8"), ""]
    for database in ("primary", "metrics"):
        datasets = catalog.list(database)
        parts.append(f"## Database `{database}` — {len(datasets)} datasets")
        parts.append("")
        for dataset in datasets:
            parts.append(f"### {dataset.qualified} ({dataset.kind})")
            if dataset.comment:
                parts.append(dataset.comment)
            parts.append("")
            for column in dataset.columns:
                comment = f" — {column.comment}" if column.comment else ""
                parts.append(f"- `{column.name}` {column.type}{comment}")
            parts.append("")
    return "\n".join(parts)
