"""Backward-compatible facade for the interest profiling package.

WP8 split the former 680-line module into:
  * interests/scoring.py    - pure literal rules, stance, metrics (no I/O)
  * interests/repository.py - every SQL statement
  * interests/service.py    - orchestration / HTTP-facing use cases

Importing from here keeps every existing caller and test working, while the
implementation lives in the layered package.
"""
from .interests.scoring import (
    ALGORITHM_VERSION, BEHAVIOR_WEIGHTS, HALF_LIFE_DAYS, STANCES, UNKNOWN, WINDOWS,
    analyze_text, define_topic, fingerprint, metrics, normalize_topic, published,
    raw_evidence, representative_from, utcnow,
)
from .interests.service import InterestService

__all__ = [
    "ALGORITHM_VERSION", "BEHAVIOR_WEIGHTS", "HALF_LIFE_DAYS", "STANCES", "UNKNOWN", "WINDOWS",
    "InterestService", "analyze_text", "define_topic", "fingerprint", "metrics",
    "normalize_topic", "published", "raw_evidence", "representative_from", "utcnow",
]
