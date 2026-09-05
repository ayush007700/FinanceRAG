"""Lightweight finance entity extraction for graph enrichment."""

from __future__ import annotations

import re

ENTITY_PATTERNS = [
    r"§\s?\d+[A-Za-z]*",
    r"\bIRC\s§?\s?\d+[A-Za-z]*\b",
    r"\bForm\s\d{3,5}(?:-[A-Z])?\b",
    r"\bR&D Tax Credit\b",
    r"\bCost Segregation\b",
    r"\bSales\s*(?:&|and)\s*Use Tax\b",
    r"\bInvestment Tax Credit\b",
    r"\bProduction Tax Credit\b",
    r"\bLIFO\b",
    r"\bHMRC\b",
    r"\bIRS\b",
    r"\bCPA firms?\b",
    r"\bFortune 1000\b",
    r"\b§179D\b",
    r"\b§45L\b",
]


def extract_entities(text: str) -> list[str]:
    found: set[str] = set()
    for pattern in ENTITY_PATTERNS:
        for match in re.findall(pattern, text, flags=re.IGNORECASE):
            found.add(re.sub(r"\s+", " ", match).strip())
    return sorted(found)
