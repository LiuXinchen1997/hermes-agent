"""Three-tier memory store — drop-in subclass of `MemoryStore`.

Inherits the `target="user"` (USER.md) code path from `MemoryStore`. Overrides
`target="memory"` to use a tiered jsonl storage with per-entry embeddings and
graceful eviction.

Layout:
    <memory_dir>/
    ├── USER.md           T0 — long-term user profile (inherited from MemoryStore)
    ├── MEMORY.md         legacy, kept as fallback after one-shot migration
    ├── WORKING.jsonl     T1 — working memory (per-turn recall, eviction on overflow)
    └── COLD.jsonl        T2 — cold archive (append-only, never auto-injected)

Tier transitions:
    T1 → T2:  triggered by add() when total chars exceed t1_char_limit.
              Picks bottom-1 by recency+frequency, appends to COLD.jsonl,
              removes from WORKING.jsonl.
    T2 → T1:  not implemented.
    T0 ↔ Tn:  no automatic promotion / demotion.

Compatibility with existing `MemoryStore` consumer surface:
    add(target, content)               — "memory" → T1 path; "user" → super()
    replace(target, old, new)          — "memory" → T1 path; "user" → super()
    remove(target, old)                — "memory" → T1 path; "user" → super()
    format_for_system_prompt(target)   — "memory" → None (retriever handles per-turn);
                                          "user" → super()
    load_from_disk()                   — super() loads USER+MEMORY; we then load T1
                                          and migrate from MEMORY.md if WORKING.jsonl
                                          is missing.
"""
from __future__ import annotations

import json
import logging
import os
import random
import string
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from math import exp, log1p
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.memory_tool import MemoryStore, _scan_memory_content, get_memory_dir

logger = logging.getLogger(__name__)


T1_CHAR_LIMIT_DEFAULT = 3000


# ─── id generation ────────────────────────────────────────────────────────


def _ulid_like() -> str:
    """26-char sortable id: 10-hex-char timestamp + 16-char random base36."""
    ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    ts_part = format(ts_ms, "010x")
    rand_alphabet = string.ascii_lowercase + string.digits
    rand_part = "".join(random.choices(rand_alphabet, k=16))
    return f"{ts_part}_{rand_part}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── entry ────────────────────────────────────────────────────────────────


@dataclass
class Entry:
    id: str
    text: str
    tier: int  # 1 = WORKING, 2 = COLD
    created_at: str
    last_recalled_at: Optional[str] = None
    recall_count: int = 0
    embedding: Optional[List[float]] = None

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_jsonl(cls, line: str) -> "Entry":
        d = json.loads(line)
        return cls(
            id=d["id"],
            text=d["text"],
            tier=int(d.get("tier", 1)),
            created_at=d.get("created_at") or _now_iso(),
            last_recalled_at=d.get("last_recalled_at"),
            recall_count=int(d.get("recall_count", 0)),
            embedding=d.get("embedding"),
        )

    @property
    def char_len(self) -> int:
        return len(self.text)


# ─── store ────────────────────────────────────────────────────────────────


class TieredMemoryStore(MemoryStore):
    """MemoryStore extension: T0=USER.md (super), T1=WORKING.jsonl, T2=COLD.jsonl."""

    def __init__(
        self,
        embedder=None,
        memory_char_limit: int = 2200,
        user_char_limit: int = 1375,
        t1_char_limit: int = T1_CHAR_LIMIT_DEFAULT,
        tau_days: float = 14.0,
        beta: float = 0.2,
        gamma: float = 0.1,
        metrics=None,
    ):
        # Note: memory_char_limit is unused by the T1 path (we use t1_char_limit
        # instead). Kept in the signature for super() init compatibility so
        # existing config keys (memory.memory_char_limit) don't break.
        super().__init__(
            memory_char_limit=memory_char_limit,
            user_char_limit=user_char_limit,
        )
        self.embedder = embedder
        self.t1_char_limit = t1_char_limit
        self.tau_days = tau_days
        self.beta = beta
        self.gamma = gamma
        self.metrics = metrics  # optional MemoryMetrics; None disables recording

        self._t1: List[Entry] = []

    # ─── path helpers ────────────────────────────────────────────────────

    def _t1_path(self) -> Path:
        return get_memory_dir() / "WORKING.jsonl"

    def _t2_path(self) -> Path:
        return get_memory_dir() / "COLD.jsonl"

    def _legacy_memory_md_path(self) -> Path:
        return get_memory_dir() / "MEMORY.md"

    # ─── load + migration ────────────────────────────────────────────────

    def load_from_disk(self) -> None:
        """Load T0 (via super) + T1 jsonl. Migrate from MEMORY.md if needed."""
        super().load_from_disk()  # handles USER.md + (legacy) MEMORY.md

        get_memory_dir().mkdir(parents=True, exist_ok=True)
        self._t1 = []

        t1_path = self._t1_path()
        legacy_path = self._legacy_memory_md_path()

        if t1_path.exists():
            self._load_t1_from_jsonl(t1_path)
        elif legacy_path.exists() and self.memory_entries:
            # One-shot migration: every line in legacy MEMORY.md becomes a T1
            # entry. We keep MEMORY.md on disk untouched as a fallback; new
            # writes go to WORKING.jsonl from here on.
            logger.info(
                "Migrating %d entries from legacy MEMORY.md to %s",
                len(self.memory_entries), t1_path,
            )
            for text in self.memory_entries:
                emb = self.embedder.encode(text) if self.embedder else None
                self._t1.append(Entry(
                    id=_ulid_like(),
                    text=text,
                    tier=1,
                    created_at=_now_iso(),
                    embedding=emb,
                ))
            self._persist_t1()

        # Clear inherited `memory_entries` and the "memory" snapshot so that
        # consumers calling format_for_system_prompt("memory") get None — the
        # retriever takes over per-turn injection from here.
        self.memory_entries = []
        self._system_prompt_snapshot["memory"] = ""

    def _load_t1_from_jsonl(self, path: Path) -> None:
        for lineno, raw in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            raw = raw.strip()
            if not raw:
                continue
            try:
                self._t1.append(Entry.from_jsonl(raw))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                logger.warning(
                    "TieredMemoryStore: skipping malformed line %d in %s: %s",
                    lineno, path, e,
                )

    # ─── MemoryStore-compatible API (target="memory" branch) ─────────────

    def add(self, target: str, content: str) -> Dict[str, Any]:
        if target != "memory":
            return super().add(target, content)
        return self._add_t1(content)

    def replace(
        self, target: str, old_text: str, new_content: str
    ) -> Dict[str, Any]:
        if target != "memory":
            return super().replace(target, old_text, new_content)
        return self._replace_t1(old_text, new_content)

    def remove(self, target: str, old_text: str) -> Dict[str, Any]:
        if target != "memory":
            return super().remove(target, old_text)
        return self._remove_t1(old_text)

    def format_for_system_prompt(self, target: str) -> Optional[str]:
        # T1 is recalled per-turn, never injected into system prompt.
        # T0 (USER.md) keeps the inherited behavior.
        if target == "memory":
            return None
        return super().format_for_system_prompt(target)

    # ─── T1 mutations ────────────────────────────────────────────────────

    def _add_t1(self, content: str) -> Dict[str, Any]:
        content = content.strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}

        # Reuse the existing safety scan from MemoryStore.
        scan_error = _scan_memory_content(content)
        if scan_error:
            return {"success": False, "error": scan_error}

        # Exact-text dedup against T1
        for e in self._t1:
            if e.text == content:
                if self.metrics is not None:
                    try:
                        self.metrics.record(
                            "add_dedup_t1",
                            matched_id=e.id,
                            t1_count=len(self._t1),
                        )
                    except Exception:
                        pass
                return self._t1_response(
                    message="Entry already exists (no duplicate added).",
                )

        # Cross-tier observability: same text might exist in T2 (cold
        # archive) from a previous eviction. We do NOT auto-promote it
        # back (T2 → T1 path is deferred — see docs/memory_tiering.md),
        # but we surface the fact in the tool response and metrics so
        # callers know they're re-adding something the system already
        # had at some point.
        t2_match_info: Optional[Dict[str, Any]] = None
        t2_match = self._find_in_t2(content)
        if t2_match is not None:
            t2_match_info = {
                "id": t2_match.id,
                "archived_at": t2_match.last_recalled_at or t2_match.created_at,
                "prior_recall_count": t2_match.recall_count,
            }
            if self.metrics is not None:
                try:
                    self.metrics.record(
                        "add_dup_in_t2",
                        matched_t2_id=t2_match.id,
                        prior_recall_count=t2_match.recall_count,
                    )
                except Exception:
                    pass

        emb = self.embedder.encode(content) if self.embedder else None
        new_entry = Entry(
            id=_ulid_like(),
            text=content,
            tier=1,
            created_at=_now_iso(),
            embedding=emb,
        )
        self._t1.append(new_entry)

        # Graceful eviction: demote bottom-1 (excluding the just-added entry)
        # until under limit or only the just-added entry remains.
        evicted_summaries: List[Dict[str, str]] = []
        while self._t1_char_count() > self.t1_char_limit and len(self._t1) > 1:
            victim = self._select_eviction_victim(exclude_id=new_entry.id)
            if victim is None:
                break
            evicted_summaries.append({
                "id": victim.id,
                "preview": victim.text[:80] + ("…" if len(victim.text) > 80 else ""),
            })
            self._demote_to_t2(victim)

        if self._t1_char_count() > self.t1_char_limit:
            logger.warning(
                "TieredMemoryStore: T1 still over limit (%d/%d) after eviction; "
                "accepting single oversize entry %s",
                self._t1_char_count(), self.t1_char_limit, new_entry.id,
            )

        self._persist_t1()
        if self.metrics is not None:
            try:
                self.metrics.record(
                    "add",
                    t1_count_after=len(self._t1),
                    t1_chars_after=self._t1_char_count(),
                    evicted=len(evicted_summaries),
                )
            except Exception:
                pass
        message = "Entry added."
        if t2_match_info is not None:
            message += (
                " Note: an entry with identical text was previously in "
                "the cold archive (T2). See `previously_archived` for the "
                "old id; this add created a new T1 entry rather than "
                "promoting from T2."
            )
        response = self._t1_response(
            message=message,
            evicted=evicted_summaries,
            added_id=new_entry.id,
        )
        if t2_match_info is not None:
            response["previously_archived"] = t2_match_info
        return response

    def _replace_t1(self, old_text: str, new_content: str) -> Dict[str, Any]:
        old_text = old_text.strip()
        new_content = new_content.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}
        if not new_content:
            return {
                "success": False,
                "error": "new_content cannot be empty. Use 'remove' to delete entries.",
            }

        scan_error = _scan_memory_content(new_content)
        if scan_error:
            return {"success": False, "error": scan_error}

        matches = [e for e in self._t1 if old_text in e.text]
        if not matches:
            return {"success": False, "error": f"No entry matched '{old_text}'."}

        unique_texts = {e.text for e in matches}
        if len(matches) > 1 and len(unique_texts) > 1:
            return {
                "success": False,
                "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                "matches": [
                    e.text[:80] + ("…" if len(e.text) > 80 else "") for e in matches
                ],
            }

        target_entry = matches[0]
        target_entry.text = new_content
        target_entry.embedding = (
            self.embedder.encode(new_content) if self.embedder else None
        )
        # Keep created_at + recall_count: same logical entry, just edited.

        evicted_summaries: List[Dict[str, str]] = []
        while self._t1_char_count() > self.t1_char_limit and len(self._t1) > 1:
            victim = self._select_eviction_victim(exclude_id=target_entry.id)
            if victim is None:
                break
            evicted_summaries.append({
                "id": victim.id,
                "preview": victim.text[:80] + ("…" if len(victim.text) > 80 else ""),
            })
            self._demote_to_t2(victim)

        self._persist_t1()
        if self.metrics is not None:
            try:
                self.metrics.record("replace", evicted=len(evicted_summaries))
            except Exception:
                pass
        return self._t1_response(
            message="Entry replaced.", evicted=evicted_summaries,
        )

    def _remove_t1(self, old_text: str) -> Dict[str, Any]:
        old_text = old_text.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}

        matches = [e for e in self._t1 if old_text in e.text]
        if not matches:
            return {"success": False, "error": f"No entry matched '{old_text}'."}

        unique_texts = {e.text for e in matches}
        if len(matches) > 1 and len(unique_texts) > 1:
            return {
                "success": False,
                "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                "matches": [
                    e.text[:80] + ("…" if len(e.text) > 80 else "") for e in matches
                ],
            }

        target_entry = matches[0]
        self._t1 = [e for e in self._t1 if e.id != target_entry.id]
        self._persist_t1()
        if self.metrics is not None:
            try:
                self.metrics.record("remove", t1_count_after=len(self._t1))
            except Exception:
                pass
        return self._t1_response(message="Entry removed.")

    # ─── Retriever-facing API ────────────────────────────────────────────

    def recall_candidates(self) -> List[Entry]:
        """Snapshot of T1 entries for the retriever to score."""
        return list(self._t1)

    def update_recall(
        self, entry_ids: List[str], timestamp: Optional[str] = None,
    ) -> None:
        """Bump recall_count + last_recalled_at on named entries. Batch persist."""
        if not entry_ids:
            return
        ts = timestamp or _now_iso()
        ids_set = set(entry_ids)
        touched = False
        for e in self._t1:
            if e.id in ids_set:
                e.last_recalled_at = ts
                e.recall_count += 1
                touched = True
        if touched:
            self._persist_t1()

    def t1_count(self) -> int:
        return len(self._t1)

    def t1_chars(self) -> int:
        return self._t1_char_count()

    def t2_count(self) -> int:
        """Count non-blank lines in COLD.jsonl. Stream-read so a multi-MB
        archive doesn't load wholesale into memory just for a diagnostic."""
        path = self._t2_path()
        if not path.exists():
            return 0
        count = 0
        with path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    count += 1
        return count

    def _find_in_t2(self, text: str) -> Optional[Entry]:
        """Stream-scan COLD.jsonl for an entry whose text matches exactly.

        Returns the most-recently-archived match (by created_at), or None.
        Called from `_add_t1` for cross-tier dedup observability —
        intentionally does NOT promote the match back to T1 (T2 → T1
        path is deferred). Cost is O(N) over T2; acceptable for demo
        scale where T2 is in the kilobytes-to-low-megabytes range.
        """
        path = self._t2_path()
        if not path.exists():
            return None
        most_recent: Optional[Entry] = None
        most_recent_ts = ""
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = Entry.from_jsonl(line)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
                if entry.text != text:
                    continue
                ts = entry.created_at or ""
                if ts > most_recent_ts:
                    most_recent = entry
                    most_recent_ts = ts
        return most_recent

    # ─── eviction internals ──────────────────────────────────────────────

    def _t1_char_count(self) -> int:
        return sum(e.char_len for e in self._t1)

    def _select_eviction_victim(
        self, exclude_id: Optional[str] = None,
    ) -> Optional[Entry]:
        candidates = [e for e in self._t1 if e.id != exclude_id]
        if not candidates:
            return None
        return min(candidates, key=self._eviction_score)

    def _eviction_score(self, e: Entry) -> float:
        now = datetime.now(timezone.utc)
        ref = e.last_recalled_at or e.created_at
        ref_dt = datetime.fromisoformat(ref)
        if ref_dt.tzinfo is None:
            ref_dt = ref_dt.replace(tzinfo=timezone.utc)
        dt_days = (now - ref_dt).total_seconds() / 86400.0
        recency = exp(-dt_days / self.tau_days)
        freq = log1p(e.recall_count)
        return self.beta * recency + self.gamma * freq

    def _demote_to_t2(self, entry: Entry) -> None:
        entry.tier = 2
        with self._t2_path().open("a", encoding="utf-8") as f:
            f.write(entry.to_jsonl() + "\n")
        self._t1 = [e for e in self._t1 if e.id != entry.id]
        if self.metrics is not None:
            try:
                # Age in days at demotion time — a useful indicator of how
                # long entries survive in T1 before churning.
                from datetime import datetime as _dt
                created = _dt.fromisoformat(entry.created_at)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age_days = (
                    datetime.now(timezone.utc) - created
                ).total_seconds() / 86400.0
                self.metrics.record(
                    "evict",
                    victim_id=entry.id,
                    victim_age_days=round(age_days, 3),
                    victim_recall_count=entry.recall_count,
                )
            except Exception:
                pass

    # ─── persistence ─────────────────────────────────────────────────────

    def _persist_t1(self) -> None:
        """Atomic rewrite of WORKING.jsonl via tempfile + os.replace."""
        path = self._t1_path()
        get_memory_dir().mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=".working_",
            suffix=".jsonl.tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for e in self._t1:
                    f.write(e.to_jsonl() + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ─── response shaping (MemoryStore-compatible) ───────────────────────

    def _t1_response(
        self,
        message: str,
        evicted: Optional[List[Dict[str, str]]] = None,
        added_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Shape return value to match MemoryStore.add()'s consumer expectations."""
        entries = [e.text for e in self._t1]
        current = self._t1_char_count()
        limit = self.t1_char_limit
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0
        resp: Dict[str, Any] = {
            "success": True,
            "target": "memory",
            "entries": entries,
            "usage": f"{pct}% — {current:,}/{limit:,} chars",
            "entry_count": len(entries),
            "message": message,
        }
        if evicted:
            resp["evicted"] = evicted
        if added_id:
            resp["added_id"] = added_id
        return resp
