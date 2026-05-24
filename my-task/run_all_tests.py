#!/usr/bin/env python3
"""
Tiered Memory System — Automated Test Runner
=============================================
Reproduces all quantitative results in my-task/experiment-report.md.

Test coverage
─────────────
  TC-01  满载任务完成率          TieredMemoryStore.add() 驱逐
  TC-02  多轮对话一致性           → 见 tc02_live_test.py（需要 LLM API key）
  TC-03A 每轮注入字符数           format_for_system_prompt → None
  TC-03B Prompt Cache 命中率     → 定性分析（打印说明）
  TC-03C 召回延迟基准             MemoryRetriever.recall() 吞吐
  TC-04  召回精准度 P@K           MemoryRetriever 评分公式
  TC-05  长期知识容量扩展         T1→T2 驱逐 + 冷存档

用法
────
  cd hermes-agent
  venv/bin/python my-task/run_all_tests.py

输出：每个用例 Mode A / Mode B 数字 + 末尾汇总表。
无需任何 LLM API key；全部使用 KeywordEmbedder（Jaccard）本地运行。
"""
import re
import sys
import time
import tempfile
import statistics
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

SEP = "─" * 64


# ════════════════════════════════════════════════════════════════
# 辅助
# ════════════════════════════════════════════════════════════════

def make_patches(tmp: str):
    p = Path(tmp)
    return [
        patch("tools.memory_tool.get_memory_dir", return_value=p),
        patch("tools.memory_tiered_store.get_memory_dir", return_value=p),
    ]


def apply(patches):
    for p in patches:
        p.start()


def release(patches):
    for p in patches:
        p.stop()


# ════════════════════════════════════════════════════════════════
# TC-01  满载任务完成率
# ════════════════════════════════════════════════════════════════

def tc01():
    print(f"\n{'TC-01':═<64}")
    print("满载任务完成率（TCR）— TieredMemoryStore.add() 驱逐机制")
    print(SEP)

    # 36 条 ~60 字符预填（合计 ~2160 字符，逼近 Mode A 上限 2200）
    PREFILL = [
        f"项目配置事实{i:02d}：团队规范要求所有服务必须通过健康检查，"
        f"超时配置为30秒，重试策略为指数退避，最大重试3次，日志级别生产环境用INFO"
        for i in range(36)
    ]
    NEW_ENTRIES = [
        "新增规范：所有API响应必须包含requestId字段，便于链路追踪和日志关联",
        "数据库连接池从HikariCP切换为pgBouncer，最大连接数从50调整为20",
        "前端资源CDN迁移至CloudFront，旧的阿里云OSS地址将于下季度下线",
        "安全要求：所有内部服务间调用改用mTLS，证书由cert-manager自动轮转",
        "监控告警阈值调整：P99延迟从500ms收紧到300ms，触发PagerDuty通知",
    ]
    prefill_chars = sum(len(e) for e in PREFILL)
    print(f"预填 {len(PREFILL)} 条 / 合计 {prefill_chars} 字符（Mode A 上限 2200，剩余 {2200-prefill_chars} 字符）")

    results = {}
    for mode in ("A", "B"):
        with tempfile.TemporaryDirectory() as tmp:
            patches = make_patches(tmp)
            apply(patches)
            if mode == "A":
                from tools.memory_tool import MemoryStore
                store = MemoryStore()
                store.load_from_disk()
            else:
                from tools.memory_tiered_store import TieredMemoryStore
                from tools.memory_embedder import KeywordEmbedder
                store = TieredMemoryStore(embedder=KeywordEmbedder(), t1_char_limit=3000)
                store.load_from_disk()

            for e in PREFILL:
                store.add("memory", e)

            successes = []
            for e in NEW_ENTRIES:
                r = store.add("memory", e)
                successes.append(r["success"])

            release(patches)

        ok = sum(successes)
        total = len(NEW_ENTRIES)
        tcr = ok / total
        results[mode] = dict(ok=ok, total=total, tcr=tcr, successes=successes)
        status = [("✅" if s else "❌") for s in successes]
        print(f"  Mode {mode}: {' '.join(status)}  →  TCR = {ok}/{total} = {tcr:.0%}")

    print()
    print(f"  结论: Mode A TCR={results['A']['tcr']:.0%}  Mode B TCR={results['B']['tcr']:.0%}")
    return results


# ════════════════════════════════════════════════════════════════
# TC-03A  每轮注入字符数
# ════════════════════════════════════════════════════════════════

def tc03a():
    print(f"\n{'TC-03A':═<64}")
    print("每轮注入字符数 — format_for_system_prompt(\"memory\") → None")
    print(SEP)

    # 11 条英文 Python 工具链条目 ~88 字符/条 ≈ 968 字符
    ENTRIES = [
        "Use ruff for Python linting; run ruff check . before every commit, not flake8 or pylint",
        "Manage Python dependencies with poetry; always commit poetry.lock alongside every change",
        "Type-check Python with mypy --strict; CI pipeline fails on any unresolved type error",
        "Pin Python to version 3.11 in .python-version; enforce locally with pyenv or mise",
        "Run all tests with pytest from the project root; shared fixtures live in tests/conftest.py",
        "Format Python code with black --line-length 88; enforced automatically via pre-commit hook",
        "Pin direct dependencies in pyproject.toml; never create requirements.txt for new projects",
        "Use pydantic v2 for all data validation; avoid plain dataclasses for external-facing schemas",
        "Never write bare except: clauses; always catch specific exception types in production code",
        "Keep functions under 40 lines and modules under 300 lines; flag exceptions for review",
        "Define __all__ in every Python module; keeps the public API surface explicit and auditable",
    ]
    QUERY = "run ruff on the codebase before committing"

    # ── Mode A ──
    with tempfile.TemporaryDirectory() as tmp:
        patches = make_patches(tmp)
        apply(patches)
        from tools.memory_tool import MemoryStore
        writer = MemoryStore()
        writer.load_from_disk()
        for e in ENTRIES:
            writer.add("memory", e)
        # 新建实例模拟新会话（format_for_system_prompt 读启动时快照）
        reader = MemoryStore()
        reader.load_from_disk()
        mem_block_a = reader.format_for_system_prompt("memory") or ""
        release(patches)

    chars_a = len(mem_block_a)

    # ── Mode B ──
    with tempfile.TemporaryDirectory() as tmp:
        patches = make_patches(tmp)
        apply(patches)
        from tools.memory_tiered_store import TieredMemoryStore
        from tools.memory_embedder import KeywordEmbedder
        from tools.memory_retriever import MemoryRetriever

        embedder = KeywordEmbedder()
        store_b = TieredMemoryStore(embedder=embedder, t1_char_limit=3000)
        store_b.load_from_disk()
        for e in ENTRIES:
            store_b.add("memory", e)

        assert store_b.format_for_system_prompt("memory") is None, \
            "TieredMemoryStore 应返回 None（T1 不进 system prompt）"

        retriever = MemoryRetriever(store_b, embedder, min_similarity=0.05, k=5)
        hits = retriever.recall(QUERY)
        block_b = retriever.render_block(hits)
        chars_b = len(block_b)
        release(patches)

    saving = (1 - chars_b / chars_a) if chars_a else 0
    print(f"  条目数: {len(ENTRIES)}  查询: \"{QUERY}\"")
    print(f"  Mode A system_prompt 记忆块: {chars_a} chars（全量固定）")
    print(f"  Mode B per-turn 召回块:      {chars_b} chars（hits={len(hits)}）")
    print(f"  每轮 context 节省:           {saving:.1%}")

    return dict(chars_a=chars_a, chars_b=chars_b, hits=len(hits), saving=saving)


# ════════════════════════════════════════════════════════════════
# TC-03B  Prompt Cache 命中率（定性）
# ════════════════════════════════════════════════════════════════

def tc03b():
    print(f"\n{'TC-03B':═<64}")
    print("Prompt Cache 命中率 — 定性分析（无需 LLM API）")
    print(SEP)
    print("""  原理：Anthropic / OpenRouter 以 system prompt 内容作为 cache key。
  Mode A：system prompt = T0 + 全量 T1（随每次 memory(add) 增长）
           → 每次新增记忆后下个会话 cache MISS。
  Mode B：system prompt = T0 only（USER.md，极少变动）
           → T1 写入不影响 system prompt → 跨会话持续 cache HIT。

  验证方式：调用 API 后观察 usage.cache_read_input_tokens：
    Mode A：仅首轮命中，后续因 T1 变化而 miss。
    Mode B：每轮均应出现大量缓存命中。

  10 轮会话（每轮新增 1 条记忆）预期命中次数：
    Mode A  ~0-3 次      Mode B  ~9-10 次
  """)


# ════════════════════════════════════════════════════════════════
# TC-03C  召回延迟基准
# ════════════════════════════════════════════════════════════════

def tc03c(n_iters: int = 1000):
    print(f"\n{'TC-03C':═<64}")
    print(f"召回延迟基准 — KeywordEmbedder，{n_iters} 次迭代")
    print(SEP)

    with tempfile.TemporaryDirectory() as tmp:
        patches = make_patches(tmp)
        apply(patches)
        from tools.memory_tiered_store import TieredMemoryStore
        from tools.memory_embedder import KeywordEmbedder
        from tools.memory_retriever import MemoryRetriever

        embedder = KeywordEmbedder()
        store = TieredMemoryStore(embedder=embedder, t1_char_limit=3000)
        store.load_from_disk()
        for i in range(20):
            store.add("memory", f"entry {i}: project tooling rules and team configuration standards")
        retriever = MemoryRetriever(store, embedder, min_similarity=0.05, k=5)

        lats_ms = []
        for _ in range(n_iters):
            t0 = time.perf_counter()
            retriever.recall("project tooling rules")
            lats_ms.append((time.perf_counter() - t0) * 1000)

        release(patches)

    lats_ms.sort()
    p50 = statistics.median(lats_ms)
    p95 = lats_ms[int(n_iters * 0.95)]
    p99 = lats_ms[int(n_iters * 0.99)]
    p_max = lats_ms[-1]

    print(f"  T1 条目数: 20  查询: \"project tooling rules\"")
    print(f"  P50 = {p50:.3f} ms")
    print(f"  P95 = {p95:.3f} ms")
    print(f"  P99 = {p99:.3f} ms")
    print(f"  Max = {p_max:.3f} ms")
    print(f"  目标: P99 < 10 ms  →  {'✅ 达标' if p99 < 10 else '❌ 超标'}")

    return dict(p50=p50, p95=p95, p99=p99, p_max=p_max)


# ════════════════════════════════════════════════════════════════
# TC-04  召回精准度 P@K
# ════════════════════════════════════════════════════════════════

def tc04():
    print(f"\n{'TC-04':═<64}")
    print("召回精准度 P@K — MemoryRetriever 评分公式（KeywordEmbedder）")
    print(SEP)

    # 英文条目，确保 Jaccard 有词素重叠
    CLUSTERS = {
        "python":  ["Python lint: use ruff",
                    "Python deps: use poetry lockfile",
                    "Python types: use mypy strict mode",
                    "Python tests: use pytest with conftest"],
        "js":      ["JS style: use eslint and prettier",
                    "JS packages: use pnpm workspace",
                    "JS build: use vite bundler",
                    "JS tests: use vitest with snap files"],
        "deploy":  ["deploy on AWS EKS per team namespace",
                    "Helm charts stored in infra helm dir",
                    "rolling update maxSurge one pod",
                    "deploy window Tuesday Thursday only"],
        "db":      ["database PostgreSQL fifteen with replicas",
                    "ORM SQLAlchemy two not Django",
                    "migrations use alembic before deploy",
                    "connection pool pgBouncer max twenty"],
        "process": ["PR requires two approvals to merge",
                    "commits follow Conventional Commits spec",
                    "code freeze last day of each month"],
    }
    QUERIES = [
        ("run linter on python code before commit",   "python"),
        ("install frontend package with package manager", "js"),
        ("kubernetes deployment strategy rolling",    "deploy"),
        ("database orm and migration tooling",        "db"),
        ("code review pull request merge process",    "process"),
    ]

    with tempfile.TemporaryDirectory() as tmp:
        patches = make_patches(tmp)
        apply(patches)
        from tools.memory_tiered_store import TieredMemoryStore
        from tools.memory_embedder import KeywordEmbedder
        from tools.memory_retriever import MemoryRetriever

        embedder = KeywordEmbedder()
        store = TieredMemoryStore(embedder=embedder, t1_char_limit=10000)
        store.load_from_disk()

        id_to_cluster = {}
        for cluster, texts in CLUSTERS.items():
            for text in texts:
                r = store.add("memory", text)
                if r.get("added_id"):
                    id_to_cluster[r["added_id"]] = cluster

        retriever = MemoryRetriever(store, embedder, min_similarity=0.05, k=5)

        total_recalled = 0
        total_correct = 0
        rows = []
        for query, expected in QUERIES:
            hits = retriever.recall(query)
            hit_clusters = [id_to_cluster.get(h.id, "?") for h in hits]
            correct = sum(1 for c in hit_clusters if c == expected)
            k = len(hits)
            precision = correct / k if k else 0
            total_recalled += k
            total_correct += correct
            rows.append((expected, query, k, correct, precision))

        release(patches)

    all_entry_count = sum(len(v) for v in CLUSTERS.values())
    # Mode A 全量注入：每轮查询的噪音 = (总条数 - 目标域条数) / 总条数
    per_cluster = {c: len(v) for c, v in CLUSTERS.items()}
    noise_a_turns = sum(all_entry_count - per_cluster[q[1]] for q in QUERIES)
    total_a_injected = all_entry_count * len(QUERIES)
    noise_rate_a = noise_a_turns / total_a_injected if total_a_injected else 0

    print(f"  {'集群':<8} {'P@K':>5}   {'查询'}")
    print(f"  {'─'*8} {'─'*5}   {'─'*40}")
    for cluster, query, k, correct, prec in rows:
        print(f"  [{cluster:<8}] P@{k}={prec:.0%}  {query[:50]}")

    overall = total_correct / total_recalled if total_recalled else 0
    noise_b = total_recalled - total_correct
    noise_rate_b = noise_b / total_recalled if total_recalled else 0
    print()
    print(f"  整体精准度: {total_correct}/{total_recalled} = {overall:.0%}")
    print(f"  Mode A 噪音率（全量可见，无过滤）: ~{noise_rate_a:.0%}")
    print(f"  Mode B 噪音率（top-K 过滤后）:     {noise_rate_b:.0%}")

    return dict(precision=overall, noise_b=noise_rate_b,
                correct=total_correct, recalled=total_recalled)


# ════════════════════════════════════════════════════════════════
# TC-05  长期知识容量扩展
# ════════════════════════════════════════════════════════════════

def tc05():
    print(f"\n{'TC-05':═<64}")
    print("长期知识容量扩展 — T1→T2 驱逐 + 冷存档（50 条写入）")
    print(SEP)

    # 50 条英文规范条目（每条 ~91 字符），50×91=4550 >> Mode A 上限 2200
    TOPICS = [
        "Python 3.11 pinned",    "poetry lockfile git",
        "mypy strict typing",    "pytest conftest.py",
        "black fmt len=88",      "dep pin pyproject",
        "pydantic v2 valid",     "no bare exceptions",
        "fn max 40 lines",       "__all__ in modules",
        "GitHub Actions CI",     "reviewpad auto PR",
        "API URL versioning",    "JSON log + traceId",
        "config via Consul",     "DB migrate Fridays",
        "i18next in locales",    "iOS15 Android10 SDK",
        "WS heartbeat 30sec",    "Redis SETNX 5s TTL",
        "Kafka dot topic fmt",   "GraphQL back-compat",
        "asset hash fingerpr",   "GrowthBook AB flags",
        "err 4xx/5xx codes",     "mock ext in tests",
        "distroless Docker",     "PR type(scope) fmt",
        "deploy Tue Thu 16h",    "idx_table_col names",
        "Istio mTLS mesh",       "Radix UI components",
        "FastAPI pydantic v2",   "SLO 99.9 P99 300ms",
        "AWS secrets manager",   "openapi codegen",
        "k6 P95 under 200ms",    "data mask at DAO",
        "flags off by default",  "Celery Redis broker",
        "rate 10rps per user",   "trunk-based gitflow",
        "Docusaurus in repo",    "Snyk daily dep scan",
        "CORS internal only",    "least-conn loadbal",
        "5min alert dedup",      "no SELECT star sql",
        "Zustand no Redux",      "container res limits",
    ]
    FACTS = [
        f"rule_{i:02d}: {topic}; this is a hard rule enforced by CI; all teams must align by Q3"
        for i, topic in enumerate(TOPICS)
    ]
    avg_len = sum(len(f) for f in FACTS) / len(FACTS)
    print(f"  条目数: {len(FACTS)}  平均长度: {avg_len:.0f} chars/条  总计: {sum(len(f) for f in FACTS)} chars")

    # ── Mode A ──
    with tempfile.TemporaryDirectory() as tmp:
        patches = make_patches(tmp)
        apply(patches)
        from tools.memory_tool import MemoryStore
        store_a = MemoryStore()
        store_a.load_from_disk()
        success_a = 0
        for f in FACTS:
            if store_a.add("memory", f)["success"]:
                success_a += 1
        release(patches)

    # ── Mode B ──
    with tempfile.TemporaryDirectory() as tmp:
        patches = make_patches(tmp)
        apply(patches)
        from tools.memory_tiered_store import TieredMemoryStore
        from tools.memory_embedder import KeywordEmbedder
        from tools.memory_retriever import MemoryRetriever

        embedder = KeywordEmbedder()
        store_b = TieredMemoryStore(embedder=embedder, t1_char_limit=3000)
        store_b.load_from_disk()
        success_b = 0
        for f in FACTS:
            if store_b.add("memory", f)["success"]:
                success_b += 1
        t1 = store_b.t1_count()
        t2 = store_b.t2_count()
        release(patches)

    lost_a = len(FACTS) - success_a
    print(f"  Mode A: 成功写入 {success_a}/{len(FACTS)}，永久丢失 {lost_a} 条")
    print(f"  Mode B: 成功写入 {success_b}/{len(FACTS)}，T1 热区 {t1} 条，T2 冷存 {t2} 条，合计 {t1+t2} 条")
    print(f"  容量提升: {(t1+t2)/success_a - 1:+.0%}" if success_a else "")

    return dict(success_a=success_a, success_b=success_b, t1=t1, t2=t2,
                lost_a=lost_a, total=len(FACTS))


# ════════════════════════════════════════════════════════════════
# 汇总
# ════════════════════════════════════════════════════════════════

def print_summary(r01, r03a, r03c, r04, r05):
    print(f"\n{'SUMMARY':═<64}")
    print("汇总对比表")
    print(SEP)
    print(f"  {'用例':<10} {'指标':<30} {'Mode A':>12} {'Mode B':>12}")
    print(f"  {'─'*10} {'─'*30} {'─'*12} {'─'*12}")

    tcr_a = f"{r01['A']['tcr']:.0%}"
    tcr_b = f"{r01['B']['tcr']:.0%}"
    print(f"  {'TC-01':<10} {'满载写入成功率 (TCR)':<30} {tcr_a:>12} {tcr_b:>12}")

    print(f"  {'TC-02':<10} {'MCS (LLM 实测)':<30} {'见 tc02':>12} {'_live_test':>12}")

    chars_a = f"{r03a['chars_a']} chars"
    chars_b = f"{r03a['chars_b']} chars"
    print(f"  {'TC-03A':<10} {'每轮注入记忆字符数':<30} {chars_a:>12} {chars_b:>12}")

    print(f"  {'TC-03B':<10} {'cache 命中率':<30} {'低（定性）':>12} {'高（定性）':>12}")

    p99_a = "N/A"
    p99_b = f"{r03c['p99']:.3f} ms"
    print(f"  {'TC-03C':<10} {'召回延迟 P99':<30} {p99_a:>12} {p99_b:>12}")

    prec_a = "N/A（全量）"
    prec_b = f"{r04['precision']:.0%}"
    print(f"  {'TC-04':<10} {'召回精准度 P@K':<30} {prec_a:>12} {prec_b:>12}")

    cap_a = f"{r05['success_a']}/{r05['total']}"
    cap_b = f"{r05['success_b']}/{r05['total']}"
    print(f"  {'TC-05':<10} {'50 条写入后可检索数':<30} {cap_a:>12} {cap_b:>12}")
    print(SEP)
    print()


# ════════════════════════════════════════════════════════════════
# main
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 64)
    print("三层记忆系统优化  ·  自动化测试套件")
    print("=" * 64)
    print("TC-02 (LLM 实测) → 运行 my-task/tc02_live_test.py")
    print("TC-03B (定性) → 见下方打印说明")

    r01  = tc01()
    r03a = tc03a()
    tc03b()
    r03c = tc03c()
    r04  = tc04()
    r05  = tc05()

    print_summary(r01, r03a, r03c, r04, r05)
    print("全部自动化用例执行完毕。")
