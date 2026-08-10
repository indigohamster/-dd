"""Build a knowledge catalog from configured metadata without inventing it."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping


_METADATA_FIELDS = ("description", "tags", "coverage", "example_questions")


def _text(value: Any, limit: int) -> str:
    value = " ".join(str(value or "").split())
    return value[:limit]


def _string_list(value: Any, *, limit: int, item_limit: int) -> tuple[str, ...]:
    if isinstance(value, str):
        values = [item.strip() for item in value.replace("，", ",").split(",")]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        values = []
    result = []
    for item in values:
        normalized = _text(item, item_limit)
        if normalized and normalized not in result:
            result.append(normalized)
        if len(result) >= limit:
            break
    return tuple(result)


@dataclass(frozen=True)
class KnowledgeCatalogEntry:
    kb_id: str
    name: str
    description: str = ""
    tags: tuple[str, ...] = ()
    coverage: str = ""
    example_questions: tuple[str, ...] = ()
    metadata_complete: bool = False
    missing_metadata: tuple[str, ...] = ()

    def prompt_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("metadata_complete", None)
        payload.pop("missing_metadata", None)
        return payload


class KnowledgeCatalog:
    """Normalized, immutable view of currently enabled knowledge bases."""

    def __init__(self, entries: list[KnowledgeCatalogEntry]):
        self.entries = tuple(entries)
        self.allowed_kb_ids = frozenset(item.kb_id for item in self.entries)

    @classmethod
    def from_ima_config(cls, ima_config: Mapping[str, Any] | None):
        configured = (ima_config or {}).get("default_knowledge_bases", [])
        entries = []
        seen_ids = set()
        for raw in configured if isinstance(configured, list) else []:
            if not isinstance(raw, Mapping) or raw.get("enabled", True) is False:
                continue
            kb_id = _text(raw.get("id"), 512)
            name = _text(raw.get("name"), 160)
            if not kb_id or not name or kb_id in seen_ids:
                continue
            seen_ids.add(kb_id)
            description = _text(raw.get("description"), 600)
            tags = _string_list(raw.get("tags"), limit=12, item_limit=60)
            coverage = _text(raw.get("coverage"), 600)
            examples = _string_list(
                raw.get("example_questions"), limit=8, item_limit=240,
            )
            values = {
                "description": description,
                "tags": tags,
                "coverage": coverage,
                "example_questions": examples,
            }
            missing = tuple(field for field in _METADATA_FIELDS if not values[field])
            entries.append(KnowledgeCatalogEntry(
                kb_id=kb_id,
                name=name,
                description=description,
                tags=tags,
                coverage=coverage,
                example_questions=examples,
                metadata_complete=not missing,
                missing_metadata=missing,
            ))
        return cls(entries)

    def prompt_payload(self) -> list[dict[str, Any]]:
        return [item.prompt_payload() for item in self.entries]

    def completeness_report(self) -> dict[str, Any]:
        incomplete = [
            {"name": item.name, "missing": list(item.missing_metadata)}
            for item in self.entries if not item.metadata_complete
        ]
        return {
            "total": len(self.entries),
            "complete": len(self.entries) - len(incomplete),
            "incomplete": incomplete,
        }

    def fingerprint(self) -> str:
        serialized = json.dumps(
            self.prompt_payload(), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
