"""Lightweight metric recorder for the tiered memory subsystem.

Writes one JSON-line per event to `~/.hermes/logs/memory_metrics.jsonl`. The
file is append-only; consumers tail or batch-read it to evaluate whether
tiering is actually helping (recall hit rate, eviction frequency, write
churn).

Events:
  recall        — emitted on every MemoryRetriever.recall() call
  add           — emitted on every TieredMemoryStore._add_t1 success
  add_dedup_t1  — emitted when an add() matches an existing T1 entry
                  (no new write; observability of agent write churn)
  add_dup_in_t2 — emitted when an add() doesn't match T1 but the same
                  text already exists in T2 (cold archive). The add
                  still proceeds as a fresh T1 entry — this event lets
                  callers see how often agents re-learn things the
                  system already archived.
  replace       — emitted on every TieredMemoryStore._replace_t1 success
  remove        — emitted on every TieredMemoryStore._remove_t1 success
  evict         — emitted on every T1 → T2 demotion

Design:
  - Append-only, single line per event, no rotation (call site is low-rate).
  - All failures swallowed at debug level: a broken metrics file must
    never break a memory operation.
  - Optional — callers pass an instance into TieredMemoryStore /
    MemoryRetriever; passing None disables recording.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryMetrics:
    """Append-only jsonl recorder. One per agent instance."""

    def __init__(self, session_id: str = "", log_path: Optional[Path] = None):
        self.session_id = session_id or "unknown"
        if log_path is None:
            try:
                from hermes_constants import get_hermes_home
                log_path = get_hermes_home() / "logs" / "memory_metrics.jsonl"
            except Exception:
                # If hermes_home isn't available (e.g., bare unit test path),
                # fall back to a no-op location and disable writing.
                log_path = None
        self.log_path = log_path

    def record(self, event: str, **fields: Any) -> None:
        """Append one event line. Never raises."""
        if self.log_path is None:
            return
        record = {
            "ts": _now_iso(),
            "event": event,
            "session_id": self.session_id,
            **fields,
        }
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
        except OSError as e:
            logger.debug(
                "MemoryMetrics: failed to write event %s: %s", event, e,
            )
