#!/usr/bin/env python3
"""TC-02 live agent test: Mode A vs Mode B multi-turn consistency.

Mode A: all 20 memories injected into system prompt (flat MemoryStore).
Mode B: T0-only system prompt, top-5 recalled per turn (TieredMemoryStore + KeywordEmbedder).

Key fixes vs first run:
  1. Mode A uses a *fresh* MemoryStore after writes so format_for_system_prompt
     returns the live snapshot (not the empty boot-time snapshot).
  2. min_similarity lowered to 0.05 so low-overlap but relevant entries are recalled.
  3. Memory entries are positive-only ("use ruff") — no "not flake8" phrasing —
     so the scorer can unambiguously flag any interference keyword in responses.

Scoring per turn:
  PASS  — response contains the expected tool AND avoids the interference keyword
  NOISY — correct tool present BUT interference keyword also appears
  FAIL  — correct tool absent from response
"""
import sys
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from openai import OpenAI

DEEPSEEK_API_KEY = "sk-5173fcd2ea114fd895f756b8e404587e"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
MODEL = "deepseek-chat"

# ── 20 memories: 16 project facts (positive-only) + 4 interference entries ──

CLUSTER_ENTRIES = {
    "python": [
        "Python projects use ruff for linting",
        "Python dependencies are managed with poetry; always commit poetry.lock to git",
        "Python tests run with pytest; all shared fixtures live in tests/conftest.py",
        "Python type-checking uses mypy --strict mode; CI fails if there are type errors",
    ],
    "js": [
        "JS/TS projects use eslint and prettier for code style",
        "The package manager for all JS projects is pnpm workspace",
        "Frontend tests use vitest; snapshot files have the .snap extension",
        "The frontend build tool is vite for all projects",
    ],
    "deploy": [
        "Production services run on AWS EKS; namespaces are isolated per team",
        "Helm charts are stored in the infra/helm/ directory",
        "Rolling updates are configured with maxSurge=1 maxUnavailable=0",
        "Deployment windows are Tuesdays and Thursdays between 16:00 and 18:00 only",
    ],
    "db": [
        "The database is PostgreSQL 15 with two read replicas",
        "The ORM is SQLAlchemy 2",
        "Database migrations use alembic and run before each deployment",
        "Connection pooling is managed by pgBouncer with a maximum of 20 connections",
    ],
}

# Cross-domain interference: each entry mentions a keyword from a *different* cluster
INTERFERENCE_ENTRIES = [
    "Some frontend helper scripts used flake8 for style checks but those scripts were removed",
    "Legacy branches still contain webpack configuration files from before the 2023 migration",
    "A handful of backend test helpers are written in JS and use the mocha runner",
    "Several Python backend services need npm available in the container to invoke frontend assets",
]

ALL_ENTRIES = [e for cl in CLUSTER_ENTRIES.values() for e in cl] + INTERFERENCE_ENTRIES

# ── 6-turn question set: 3 Python, 3 JS ──

QUESTIONS = [
    dict(turn="T1", query="How do I set up linting for a new Python project?",
         must_contain=["ruff"], must_not_contain=["flake8"]),
    dict(turn="T2", query="What package manager should I use for a JavaScript project?",
         must_contain=["pnpm"], must_not_contain=["npm"]),
    dict(turn="T3", query="How do I write unit tests in Python?",
         must_contain=["pytest"], must_not_contain=["mocha"]),
    dict(turn="T4", query="How do I build the frontend bundle for production?",
         must_contain=["vite"], must_not_contain=["webpack"]),
    dict(turn="T5", query="How do I add a new Python library as a dependency?",
         must_contain=["poetry"], must_not_contain=["pip install"]),
    dict(turn="T6", query="How do I write a component snapshot test?",
         must_contain=["vitest"], must_not_contain=["jest"]),
]

SYSTEM_BASE = (
    "You are a software development assistant for a team. "
    "Answer each question using the team's established conventions. "
    "Be concise and specific. Do not recommend tools that are not part of the team's standards."
)


def call_llm(system_prompt: str, user_message: str) -> str:
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        max_tokens=300,
        temperature=0.1,
    )
    return resp.choices[0].message.content.strip()


def score(response: str, q: dict) -> dict:
    import re
    r = response.lower()
    hits = [kw for kw in q["must_contain"] if re.search(rf"\b{re.escape(kw.lower())}\b", r)]
    noises = [kw for kw in q["must_not_contain"] if re.search(rf"\b{re.escape(kw.lower())}\b", r)]
    correct = len(hits) == len(q["must_contain"])
    clean = len(noises) == 0
    return dict(correct=correct, clean=clean, passed=correct and clean, hits=hits, noises=noises)


# ──────────────────────────────────────────────
# Mode A: flat MemoryStore, all 20 in system prompt
# ──────────────────────────────────────────────
def run_mode_a():
    print("\n▶ Mode A (flat MemoryStore — all 20 memories in system prompt):")
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp)
        with patch("tools.memory_tool.get_memory_dir", return_value=p):
            from tools.memory_tool import MemoryStore
            # Write phase
            writer = MemoryStore()
            writer.load_from_disk()
            for e in ALL_ENTRIES:
                writer.add("memory", e)
            # Fresh instance = simulates new session; snapshot now includes all entries
            reader = MemoryStore()
            reader.load_from_disk()
            mem_block = reader.format_for_system_prompt("memory") or ""

    system_prompt = SYSTEM_BASE + ("\n\n" + mem_block if mem_block else "")
    print(f"  system prompt: {len(system_prompt)} chars  (memory block: {len(mem_block)} chars, {len(ALL_ENTRIES)} entries)")

    results = []
    for q in QUESTIONS:
        print(f"  [{q['turn']}] {q['query'][:55]}...")
        response = call_llm(system_prompt, q["query"])
        sc = score(response, q)
        results.append({**q, "response": response, "score": sc})
        tag = "PASS" if sc["passed"] else ("NOISY" if sc["correct"] else "FAIL")
        print(f"       {tag:<5}  must={sc['hits']}  noise={sc['noises']}")
        print(f"         → {response[:110].replace(chr(10), ' ')}")
    return results, len(system_prompt), len(mem_block)


# ──────────────────────────────────────────────
# Mode B: TieredMemoryStore, per-turn semantic recall
# ──────────────────────────────────────────────
def run_mode_b():
    print("\n▶ Mode B (TieredMemoryStore — T0-only system prompt + per-turn recall):")
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp)
        with patch("tools.memory_tool.get_memory_dir", return_value=p), \
             patch("tools.memory_tiered_store.get_memory_dir", return_value=p):
            from tools.memory_tiered_store import TieredMemoryStore
            from tools.memory_embedder import KeywordEmbedder
            from tools.memory_retriever import MemoryRetriever

            embedder = KeywordEmbedder()
            store = TieredMemoryStore(embedder=embedder, t1_char_limit=5000)
            store.load_from_disk()
            for e in ALL_ENTRIES:
                store.add("memory", e)

            # min_similarity=0.05: catches entries with even 1-2 keyword overlaps
            retriever = MemoryRetriever(store, embedder, min_similarity=0.05, k=5)
            system_prompt = SYSTEM_BASE  # T0 only
            print(f"  system prompt: {len(system_prompt)} chars  (T0 only, no memory block)")

            results = []
            for q in QUESTIONS:
                print(f"  [{q['turn']}] {q['query'][:55]}...")
                hits = retriever.recall(q["query"])
                recall_block = retriever.render_block(hits) if hits else ""
                user_msg = (recall_block + "\n\n" + q["query"]) if recall_block else q["query"]

                recalled_texts = [h.text for h in hits]
                interference_leaked = [t for t in recalled_texts if t in INTERFERENCE_ENTRIES]

                response = call_llm(system_prompt, user_msg)
                sc = score(response, q)
                results.append({
                    **q, "response": response, "score": sc,
                    "recalled": recalled_texts,
                    "interference_leaked": interference_leaked,
                    "recall_block_chars": len(recall_block),
                })
                tag = "PASS" if sc["passed"] else ("NOISY" if sc["correct"] else "FAIL")
                leaked_flag = f"  ⚠ interference leaked: {interference_leaked}" if interference_leaked else ""
                print(f"       {tag:<5}  recalled={len(hits)}  interference={len(interference_leaked)}{leaked_flag}")
                print(f"         recalled entries: {[t[:45] for t in recalled_texts]}")
                print(f"         → {response[:110].replace(chr(10), ' ')}")
    return results, len(system_prompt)


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 68)
    print("TC-02 Live Agent Test: Mode A vs Mode B  (DeepSeek deepseek-chat)")
    print(f"Memories: {len(ALL_ENTRIES)} total  ({len(INTERFERENCE_ENTRIES)} cross-domain interference)")
    print("=" * 68)

    results_a, sys_chars_a, mem_block_a = run_mode_a()
    results_b, sys_chars_b = run_mode_b()

    # ── Comparison table ──
    print("\n" + "=" * 68)
    print("COMPARISON SUMMARY")
    print("=" * 68)
    print(f"{'Turn':<4} {'Query':<46} {'Mode A':^8} {'Mode B':^8}")
    print("-" * 68)
    for ra, rb in zip(results_a, results_b):
        def fmt(r):
            s = r["score"]
            if s["passed"]:   return "✅"
            if s["correct"]:  return f"⚠({','.join(s['noises'])})"
            return f"❌({','.join(s['must_contain'])} missing)"
        print(f"{ra['turn']:<4} {ra['query'][:46]:<46} {fmt(ra):^8} {fmt(rb):^8}")

    pa = sum(1 for r in results_a if r["score"]["passed"])
    pb = sum(1 for r in results_b if r["score"]["passed"])
    na = sum(1 for r in results_a if r["score"]["noises"])
    nb = sum(1 for r in results_b if r["score"]["noises"])
    total = len(QUESTIONS)
    print("-" * 68)
    print(f"{'MCS (pass/total)':<50} {pa}/{total}    {pb}/{total}")
    print(f"{'Noise turns':<50} {na}       {nb}")
    print(f"{'System prompt chars':<50} {sys_chars_a}    {sys_chars_b}")
    print(f"{'Memory chars in system prompt':<50} {mem_block_a}    0 (recall-only)")

    # ── Save results ──
    out_path = Path(__file__).parent / "tc02_results.json"
    out_path.write_text(json.dumps(
        {
            "model": MODEL,
            "total_memories": len(ALL_ENTRIES),
            "interference_entries": INTERFERENCE_ENTRIES,
            "mode_a": {
                "system_chars": sys_chars_a,
                "memory_block_chars": mem_block_a,
                "results": [
                    {"turn": r["turn"], "query": r["query"],
                     "response": r["response"], "score": r["score"]}
                    for r in results_a
                ],
            },
            "mode_b": {
                "system_chars": sys_chars_b,
                "results": [
                    {"turn": r["turn"], "query": r["query"],
                     "response": r["response"], "score": r["score"],
                     "recalled": r["recalled"],
                     "interference_leaked": r["interference_leaked"],
                     "recall_block_chars": r["recall_block_chars"]}
                    for r in results_b
                ],
            },
        },
        ensure_ascii=False, indent=2,
    ), encoding="utf-8")
    print(f"\nRaw results → {out_path}")
