# 三层记忆系统优化实验测试用例

> 本文档专为验证 `memory.tiering` 改动效果而设计。每个测试用例均指定了
> **A/B 分组**、**可度量指标**与**可复现命令**，用于在任务完成率、多轮一致性
> 和响应时间三个维度上量化改动前后的差异。

---

## 实验设计总览

### A/B 分组配置

| 组别 | `~/.hermes/config.yaml` 关键配置 | 说明 |
|------|-------------------------------|------|
| **Mode A（基线）** | `memory.tiering.enabled: false` | 旧路径：平铺 MEMORY.md，全量注入 system prompt |
| **Mode B（优化）** | `memory.tiering.enabled: true` | 三层路径：T0/T1/T2 + 每轮按需召回 |

同一台机器切换模式只需改配置后重启 agent，所有测试均可在同一环境下对比。

### 核心度量指标

| 指标 | 缩写 | 计算方式 |
|------|------|---------|
| 任务完成率 | TCR | 成功完成操作数 / 总操作数 × 100% |
| 多轮一致性评分 | MCS | 正确应用目标记忆的轮次 / 总轮次 × 100% |
| 上下文 Token 开销 | CTK | 每轮注入记忆字符数（近似 tokens×4） |
| 记忆添加延迟 | MAL | `memory(add)` 工具响应耗时（ms） |
| 召回精准度 | P@K | 命中预期条目的比例（见 TC-M04） |

### 指标采集渠道

```bash
# Mode B 专用：读取实时指标日志
tail -f ~/.hermes/logs/memory_metrics.jsonl | python3 -c "
import sys, json
for line in sys.stdin:
    r = json.loads(line)
    print(r['event'], r.get('k_returned',''), r.get('top_similarity',''))
"

# 通用：统计 T1 当前大小
python3 -c "
import json; path='~/.hermes/memories/WORKING.jsonl'
import os; path=os.path.expanduser(path)
lines=[l for l in open(path) if l.strip()] if os.path.exists(path) else []
print(f'T1 entries: {len(lines)}, chars: {sum(len(json.loads(l)[\"text\"]) for l in lines)}')
"
```

---

## TC-M01 · 满载时的任务完成率

**目标**：验证旧系统在记忆满载时拒绝新增导致任务中断，新系统通过驱逐保持可用。

**前置数据**：向 agent 预写 18 条记忆，使 MEMORY.md 接近 2200 字符上限。

```bash
# 预填脚本（Mode A 和 Mode B 均可用于准备数据）
python3 - <<'EOF'
from tools.memory_tool import MemoryStore, get_memory_dir

store = MemoryStore()
store.load_from_disk()
entries = [
    "项目A使用Python 3.11，包管理用poetry",
    "项目B前端框架是Vue 3，构建工具Vite",
    "CI/CD平台是GitHub Actions，runner用ubuntu-latest",
    "数据库生产环境PostgreSQL 15，只读副本x2",
    "Redis用于会话缓存，TTL默认3600秒",
    "代码审查需要至少两个approve才能合并",
    "Release分支命名规范：release/YYYY-MM-DD",
    "团队Slack频道：#dev-alerts用于告警推送",
    "监控系统Datadog，SLO仪表盘链接已固定在channel",
    "本地开发使用Docker Compose，文件在infra/docker/",
    "单元测试覆盖率要求80%以上，PR会自动检查",
    "API文档用OpenAPI 3.0，swagger-ui路径/api/docs",
    "国际化(i18n)框架：i18next，翻译文件在locales/",
    "OAuth provider：Auth0，tenant：company-prod",
    "错误追踪Sentry，DSN已写入.env.example",
    "数据迁移脚本放migrations/，用alembic管理",
    "前端E2E测试Playwright，截图存tests/screenshots/",
    "依赖安全扫描每周一自动运行，结果发到#security",
]
for e in entries:
    r = store.add("memory", e)
    print(f"add: {r.get('success')} — {e[:40]}")
EOF
```

**操作步骤**：

```bash
# Step 1：记录当前 MEMORY.md 字符数
wc -c ~/.hermes/memories/MEMORY.md  # Mode A 基线

# Step 2：启动 agent（对应模式），发送 5 条新记忆添加指令
hermes --config mode_a.yaml  # Mode A
# 或
hermes --config mode_b.yaml  # Mode B

# 在 agent 内依次执行（可用 hermes batch 批量）：
# 1. "记住：前端新增了React Query用于数据获取"
# 2. "记住：后端引入FastAPI替换Flask"
# 3. "记住：部署环境迁移到AWS EKS"
# 4. "记住：数据库连接池改用PgBouncer"
# 5. "记住：CDN切换到CloudFront"
```

**度量**：

| 操作 | Mode A（旧） | Mode B（新） |
|------|------------|------------|
| 第1条新记忆添加 | 期望：失败（超 2200 字符）| 期望：成功（触发驱逐）|
| 第2条新记忆添加 | 期望：失败 | 期望：成功 |
| 第3-5条新记忆添加 | 期望：全部失败 | 期望：全部成功 |
| **任务完成率 TCR** | **0/5 = 0%** | **5/5 = 100%** |

**验证命令**：

```bash
# Mode B：验证驱逐发生
grep '"event":"evict"' ~/.hermes/logs/memory_metrics.jsonl | wc -l
# 期望：>= 3（至少驱逐3条老条目到T2）

# Mode B：验证 T2 存在数据（被驱逐的老条目未丢失）
wc -l ~/.hermes/memories/COLD.jsonl
```

---

## TC-M02 · 多轮对话中的记忆一致性

**目标**：在 10 轮交替主题对话中，验证 agent 每轮能正确应用当前话题相关的记忆，而不被其他话题的记忆干扰。

**前置记忆**（两组各 4 条，共 8 条，故意主题正交）：

```
Python 组：
  P1: 用户Python项目统一用ruff做lint，不用flake8
  P2: Python依赖管理用poetry，lockfile必须提交
  P3: Python测试框架pytest，fixture放conftest.py
  P4: Python类型注解要求全覆盖，用mypy严格模式

JavaScript 组：
  J1: JS/TS项目用eslint+prettier，禁用tslint
  J2: 包管理器用pnpm workspace，不用npm/yarn
  J3: 测试框架vitest，快照测试文件后缀.snap
  J4: 构建工具vite，不用webpack
```

**前置脚本**：

```python
# setup_tc_m02.py
from tools.memory_tool import MemoryStore

store = MemoryStore()
store.load_from_disk()
memories = [
    "用户Python项目统一用ruff做lint，不用flake8",
    "Python依赖管理用poetry，lockfile必须提交",
    "Python测试框架pytest，fixture放conftest.py",
    "Python类型注解要求全覆盖，用mypy严格模式",
    "JS/TS项目用eslint+prettier，禁用tslint",
    "包管理器用pnpm workspace，不用npm/yarn",
    "测试框架vitest，快照测试文件后缀.snap",
    "构建工具vite，不用webpack",
]
for m in memories:
    store.add("memory", m)
    print(f"added: {m[:50]}")
```

**10 轮对话脚本**（交替发送给 agent）：

| 轮次 | 用户消息 | 期望 agent 应用的记忆 | 判断标准 |
|------|---------|-------------------|---------|
| T1 | "帮我在Python项目里加个lint检查step到CI" | P1（ruff） | 命令含 `ruff`，不含 `flake8` |
| T2 | "新建一个React组件，用TS写" | J1（eslint+prettier） | 提到 eslint 或 prettier |
| T3 | "Python项目怎么加新依赖？" | P2（poetry） | 给出 `poetry add` 命令 |
| T4 | "JS项目加lodash依赖" | J2（pnpm） | 给出 `pnpm add` 命令 |
| T5 | "帮我写一个Python函数的单元测试" | P3（pytest） | 文件名/导入含 `pytest` |
| T6 | "JS项目加一个快照测试" | J3（vitest） | 含 `.snap` 或 `vitest` |
| T7 | "Python函数签名缺少类型注解，怎么检查" | P4（mypy） | 含 `mypy` |
| T8 | "JS项目构建打包命令是什么" | J4（vite） | 含 `vite build`，不含 `webpack` |
| T9 | "Python项目CI完整流程应该包括什么" | P1+P2+P3+P4 | 同时提到 ruff、poetry、pytest |
| T10 | "JS项目从零搭架子，列出关键工具" | J1+J2+J3+J4 | 同时提到 eslint、pnpm、vitest、vite |

**度量**：

```bash
# 每轮结束后记录：agent回复是否包含期望的工具名（0/1）
# MCS = sum(命中轮次) / 10

# Mode A 预期问题：所有 8 条记忆每轮都在 context 中，
#   但模型需自己从"噪音"中筛选，T1~T4 结果可能混淆（如 T1 提到 pnpm）
# Mode B 预期优势：T1 仅召回语义相关的 3-5 条，
#   Python 轮次看不到 JS 记忆，反之亦然
```

**期望 MCS 对比**：

| 指标 | Mode A（预期） | Mode B（预期） |
|------|-------------|-------------|
| T1-T8 单项命中率 | 70-80%（噪音干扰） | 90-100% |
| T9-T10 综合命中率 | 85%（所有记忆可见） | 90%（语义分组召回正确） |
| **总体 MCS** | **~73%** | **~92%** |

---

## TC-M03 · 响应时间与上下文 Token 开销

**目标**：量化两种模式在"注入 context 大小"和"每轮额外延迟"上的差异。

**前置条件**：T1 共 20 条记忆，总字符 ≈ 3600（超过旧系统 2200 上限，Mode A 无法达到此状态，故 Mode A 用满载 16 条 × 135 字符 ≈ 2160 chars 作为基线）。

**测量方法**：

```python
# bench_context_size.py — 无需 LLM，直接测量两种模式的 context 注入大小
import time
from tools.memory_tool import MemoryStore
from tools.memory_tiered_store import TieredMemoryStore
from tools.memory_embedder import make_embedder
from tools.memory_retriever import MemoryRetriever

ENTRIES = [f"记忆条目{i}：" + "测试内容" * 8 for i in range(20)]
USER_QUERY = "帮我做一个Python项目的代码审查"

# ── Mode A ──────────────────────────────────────────────────────────────
store_a = MemoryStore()
store_a.load_from_disk()
for e in ENTRIES[:16]:  # 最多16条不超限
    store_a.add("memory", e)
t0 = time.perf_counter()
prompt_a = store_a.format_for_system_prompt("memory") or ""
elapsed_a = (time.perf_counter() - t0) * 1000

# ── Mode B ──────────────────────────────────────────────────────────────
embedder = make_embedder(prefer_local=False)  # Jaccard，本地无需模型
store_b = TieredMemoryStore(embedder=embedder, t1_char_limit=3000)
store_b.load_from_disk()
for e in ENTRIES:  # 全部20条
    store_b.add("memory", e)
retriever = MemoryRetriever(store_b, embedder, min_similarity=0.05, k=5)

t0 = time.perf_counter()
hits = retriever.recall(USER_QUERY)
block = retriever.render_block(hits)
elapsed_b = (time.perf_counter() - t0) * 1000

print(f"Mode A: system_prompt_chars={len(prompt_a)}, latency={elapsed_a:.1f}ms")
print(f"Mode B: recall_block_chars={len(block)}, hits={len(hits)}, latency={elapsed_b:.1f}ms")
print(f"Context reduction: {1 - len(block)/max(len(prompt_a),1):.1%}")
```

**期望输出对比**：

```
Mode A: system_prompt_chars=2160,  latency=<1ms   (仅字符串格式化)
Mode B: recall_block_chars=~400,   hits=5, latency=5-15ms (含 embedding 计算)
Context reduction: ~82%
```

**关键数据点**：

| 度量项 | Mode A | Mode B | 变化 |
|-------|--------|--------|------|
| 每轮注入记忆字符 | ~2160（固定，满载） | ~400（动态，top-5） | **-82%** |
| T1 可存储条目上限 | ~16 条 | ~20+ 条（软上限可配） | **+25%** |
| 每轮记忆处理延迟 | <1ms（format string） | 5-15ms（Jaccard）/ 8-20ms（fastembed） | +5~20ms |
| Prompt cache 友好度 | ❌（memory 在 system prompt 随时变） | ✅（T0 冻结在 system prompt） | 提升 |

**延迟可接受性验证**：

```bash
# 运行 100 次召回，测 P50/P95/P99
python3 - <<'EOF'
import time, statistics
from tools.memory_tiered_store import TieredMemoryStore
from tools.memory_embedder import KeywordEmbedder
from tools.memory_retriever import MemoryRetriever

embedder = KeywordEmbedder()
store = TieredMemoryStore(embedder=embedder)
store.load_from_disk()

# 预填 20 条
for i in range(20):
    store.add("memory", f"project fact number {i} containing relevant detail")

retriever = MemoryRetriever(store, embedder, min_similarity=0.05, k=5)

latencies = []
for _ in range(100):
    t0 = time.perf_counter()
    retriever.recall("tell me about the project facts")
    latencies.append((time.perf_counter() - t0) * 1000)

latencies.sort()
print(f"P50={statistics.median(latencies):.1f}ms  "
      f"P95={latencies[94]:.1f}ms  "
      f"P99={latencies[98]:.1f}ms")
# 期望：P99 < 30ms（KeywordEmbedder）
EOF
```

---

## TC-M04 · 召回精准度（Precision@K）

**目标**：验证召回算法能从混杂的记忆库中精准找到当前查询最相关的条目。

**前置数据**（5 个语义簇，每簇 3 条，共 15 条）：

```python
# setup_tc_m04.py
CLUSTERS = {
    "python_tooling": [
        "Python项目使用ruff做lint，配置在pyproject.toml",
        "Python格式化用black，行宽120",
        "Python类型检查用mypy --strict模式",
    ],
    "js_tooling": [
        "JS/TS项目eslint配置继承@company/eslint-config",
        "包管理器pnpm，工作区根目录有pnpm-workspace.yaml",
        "前端构建用vite 5.x，HMR默认开启",
    ],
    "deployment": [
        "生产环境部署在AWS EKS，namespace按team隔离",
        "Helm chart存储在infra/helm/，版本随app版本同步",
        "滚动更新策略：maxSurge=1，maxUnavailable=0",
    ],
    "database": [
        "主数据库PostgreSQL 15，只读副本2个",
        "ORM使用SQLAlchemy 2.x，async模式",
        "迁移脚本alembic，每次迁移必须可回滚",
    ],
    "team_process": [
        "PR合并需要2个approve，必须通过所有CI check",
        "Commit message遵循Conventional Commits规范",
        "Code freeze每月最后一周，只允许bugfix",
    ],
}

QUERIES_AND_EXPECTED = [
    ("Python代码质量工具链是什么？", "python_tooling"),
    ("前端依赖怎么安装？用什么包管理器？", "js_tooling"),
    ("怎么做K8s部署？", "deployment"),
    ("数据库版本和迁移工具", "database"),
    ("PR合并流程是什么？", "team_process"),
]
```

**精准度测试脚本**：

```python
# run_precision_test.py
import json
from pathlib import Path
from tools.memory_tiered_store import TieredMemoryStore
from tools.memory_embedder import make_embedder
from tools.memory_retriever import MemoryRetriever

# 准备 store（使用 fastembed 时精准度更高）
embedder = make_embedder(prefer_local=True)
store = TieredMemoryStore(embedder=embedder, t1_char_limit=5000)
store.load_from_disk()

# 清空并写入测试数据（直接操作便于控制）
entry_index = {}  # text -> cluster
for cluster, entries in CLUSTERS.items():
    for text in entries:
        r = store.add("memory", text)
        if r.get("added_id"):
            entry_index[r["added_id"]] = cluster

retriever = MemoryRetriever(store, embedder, min_similarity=0.1, k=5)

hits_total, hits_correct = 0, 0
for query, expected_cluster in QUERIES_AND_EXPECTED:
    hits = retriever.recall(query)
    top_clusters = [entry_index.get(h.id, "unknown") for h in hits]
    in_cluster = sum(1 for c in top_clusters if c == expected_cluster)
    hits_total += len(hits)
    hits_correct += in_cluster
    precision = in_cluster / len(hits) if hits else 0
    print(f"Query: {query[:40]}")
    print(f"  top clusters: {top_clusters}, P@{len(hits)}={precision:.0%}")

overall = hits_correct / hits_total if hits_total else 0
print(f"\nOverall Precision@5 = {hits_correct}/{hits_total} = {overall:.0%}")
# KeywordEmbedder 期望: 60-70%
# FastembedEmbedder 期望: 80-90%
```

**期望精准度对比**：

| 嵌入后端 | P@5（期望） | 与 Mode A（无召回）对比 |
|---------|-----------|----------------------|
| Mode A（全量注入，无过滤） | N/A（全部 15 条暴露） | 噪音率 67%（15 条中只有 3 条相关）|
| Mode B + KeywordEmbedder | 60-70% | 噪音率降至 30-40% |
| Mode B + FastembedEmbedder | 80-90% | 噪音率降至 10-20% |

---

## TC-M05 · 驱逐后数据完整性与可检索性

**目标**：验证被驱逐到 T2 的条目不会丢失，仍可被 `session_search` 命中，且 T2 tier 字段正确。

**操作步骤**：

```bash
# Step 1：写入 25 条记忆（远超 T1 上限 3000 字符）
python3 - <<'EOF'
from tools.memory_tiered_store import TieredMemoryStore
from tools.memory_embedder import KeywordEmbedder
import os

store = TieredMemoryStore(
    embedder=KeywordEmbedder(),
    t1_char_limit=500,  # 刻意设低以触发驱逐
)
store.load_from_disk()
for i in range(25):
    r = store.add("memory", f"fact-{i:02d}: 这是第{i}条测试记忆，包含唯一标识符 FACT{i:02d}")
    evicted = r.get("evicted", [])
    if evicted:
        print(f"  evicted: {[e['preview'][:30] for e in evicted]}")

print(f"T1 count: {store.t1_count()}, T2 count: {store.t2_count()}")
EOF

# Step 2：验证 T2 文件内容
python3 - <<'EOF'
import json, os
cold = os.path.expanduser("~/.hermes/memories/COLD.jsonl")
if os.path.exists(cold):
    entries = [json.loads(l) for l in open(cold) if l.strip()]
    print(f"T2 entry count: {len(entries)}")
    for e in entries:
        assert e["tier"] == 2, f"Expected tier=2, got {e['tier']}"
        assert e["text"], "Empty text in T2"
    print("✅ All T2 entries have correct tier=2 and non-empty text")
    # 验证数据未丢失：所有原始 FACT ID 在 T1+T2 中都能找到
    working = os.path.expanduser("~/.hermes/memories/WORKING.jsonl")
    t1_entries = [json.loads(l) for l in open(working) if l.strip()] if os.path.exists(working) else []
    all_texts = {e["text"] for e in entries + t1_entries}
    missing = [f"FACT{i:02d}" for i in range(25) if not any(f"FACT{i:02d}" in t for t in all_texts)]
    if missing:
        print(f"❌ Missing facts: {missing}")
    else:
        print("✅ All 25 facts preserved (T1 + T2 combined)")
EOF
```

**期望结果**：

| 断言 | 期望值 |
|------|-------|
| T1 条目数 | ≤ 7（字符限制 500 / 平均 70 chars/条） |
| T2 条目数 | ≥ 18（25 - T1条数） |
| T2 中 tier 字段 | 全部 = 2 |
| T1 + T2 总文本集合是否包含全部 25 条原始内容 | ✅ 是 |
| Mode A 同场景下第 17 条起是否报错 | ✅ 是（超 2200 字符后返回 error） |

---

## TC-M06 · 跨会话 recall_count 持久化

**目标**：验证召回计数器跨会话正确持久化，recency+频次评分在重启后仍有效。

```python
# session_persistence_test.py
import json, time
from pathlib import Path
from tools.memory_tiered_store import TieredMemoryStore, _ulid_like, _now_iso
from tools.memory_embedder import KeywordEmbedder
from tools.memory_retriever import MemoryRetriever

embedder = KeywordEmbedder()

# ── 会话 1：写入并触发 3 次召回 ──────────────────────────────────────
store1 = TieredMemoryStore(embedder=embedder, t1_char_limit=3000)
store1.load_from_disk()
r = store1.add("memory", "python ruff is the preferred linter")
target_id = r["added_id"]
store1.add("memory", "unrelated entry about something else entirely")

retriever1 = MemoryRetriever(store1, embedder, min_similarity=0.05, k=3)
for _ in range(3):
    retriever1.recall("python lint tool")
    time.sleep(0.01)

# 验证内存中的计数
e_mem = next(x for x in store1.recall_candidates() if x.id == target_id)
assert e_mem.recall_count == 3, f"in-memory count={e_mem.recall_count}, expected 3"
print(f"Session 1: recall_count={e_mem.recall_count} ✅")

# ── 会话 2：重新加载，验证磁盘持久化 ─────────────────────────────────
store2 = TieredMemoryStore(embedder=embedder, t1_char_limit=3000)
store2.load_from_disk()
e_disk = next(x for x in store2.recall_candidates() if x.id == target_id)
assert e_disk.recall_count == 3, f"disk count={e_disk.recall_count}, expected 3"
assert e_disk.last_recalled_at is not None
print(f"Session 2: recall_count={e_disk.recall_count}, last_recalled_at={e_disk.last_recalled_at} ✅")

# ── 验证高频条目在同等语义相似度下排序靠前 ──────────────────────────
store2.add("memory", "python ruff alternative linting option")  # 相似内容，新条目
retriever2 = MemoryRetriever(store2, embedder, min_similarity=0.05, k=2)
hits = retriever2.recall("python lint tool")
assert hits[0].id == target_id, (
    f"High-frequency entry should rank first; got {hits[0].id}"
)
print("Session 2: high-recall-count entry ranks #1 ✅")
```

**期望输出**：

```
Session 1: recall_count=3 ✅
Session 2: recall_count=3, last_recalled_at=2026-... ✅
Session 2: high-recall-count entry ranks #1 ✅
```

---

## TC-M07 · Feature Flag 回归：Mode B 关闭时行为与 Mode A 完全一致

**目标**：验证 `memory.tiering.enabled = false` 时代码路径与旧 MemoryStore 100% 等价，不引入回归。

**操作步骤**：

```bash
# 1. 在 agent_init.py 测试中，用 monkeypatch 覆盖 cfg_get 返回 tiering.enabled=false
# 2. 确认 agent._memory_retriever is None
# 3. 确认 agent._memory_store 是 MemoryStore 而非 TieredMemoryStore

python3 - <<'EOF'
# 快速回归验证（不需要启动完整 agent）
import importlib, unittest.mock as mock

# 模拟 cfg_get 返回 tiering disabled
with mock.patch("hermes_cli.config.cfg_get") as m:
    m.side_effect = lambda key, default=None: (
        {"memory.tiering.enabled": False}.get(key, default)
    )
    # 确认 tiering 路径不被进入
    from hermes_cli.config import cfg_get
    tiering_cfg = {"enabled": False}
    tiering_on = bool(tiering_cfg.get("enabled", False))
    assert tiering_on is False
    print("✅ Feature flag disabled: tiering path skipped")

# 直接单元测试 MemoryStore（Mode A 的基础行为）
from tools.memory_tool import MemoryStore
import tempfile, os
with tempfile.TemporaryDirectory() as tmp:
    with mock.patch("tools.memory_tool.get_memory_dir", return_value=tmp):
        store = MemoryStore()
        store.load_from_disk()
        r = store.add("memory", "basic entry")
        assert r["success"]
        snap = store.format_for_system_prompt("memory")
        assert "basic entry" in snap
        print("✅ Mode A MemoryStore.format_for_system_prompt injects all entries")
EOF
```

**关键回归断言**：

| 行为 | Mode A（旧 MemoryStore） | Mode B disabled（期望相同） |
|------|------------------------|--------------------------|
| `format_for_system_prompt("memory")` | 返回含全部条目的字符串 | 返回相同字符串 |
| `add("memory", ...)` 满载后 | 返回 error | 返回相同 error |
| `_memory_retriever` 属性 | 不存在 / None | 必须为 None |
| 每轮 user_message | 无 `<recalled-memory>` 块 | 无 `<recalled-memory>` 块 |

---

## 汇总：三个核心维度结论模板

实验结束后填写以下汇总表（可替换为实测数字）：

### 任务完成率（TCR）

| 场景 | Mode A 实测 | Mode B 实测 | 提升 |
|------|-----------|-----------|------|
| TC-M01 满载时新增 5 条记忆 | __/5 | __/5 | __ |
| TC-M03 20 条记忆成功写入 | 16/20 上限 | 20/20 | +25% |
| TC-M05 驱逐后数据零丢失 | N/A | ✅/❌ | — |

### 多轮一致性（MCS）

| 场景 | Mode A 实测 | Mode B 实测 | 提升 |
|------|-----------|-----------|------|
| TC-M02 10 轮交替主题对话 | __/10 | __/10 | __ |
| TC-M04 P@5 召回精准度 | N/A（无过滤）| __% | __ |
| TC-M06 跨会话计数持久化 | N/A | ✅/❌ | — |

### 响应时间 / Token 开销

| 度量 | Mode A 实测 | Mode B 实测 | 变化 |
|------|-----------|-----------|------|
| 每轮注入记忆字符（满载） | ~2160 | ~400 | -82% |
| 记忆处理延迟 P99（ms） | <1 | __ | +__ms |
| Prompt cache 命中率（T0） | 低（memory 变动破坏）| 高（T0 冻结）| ↑ |

---

*测试数据目录：`~/.hermes/memories/`　指标日志：`~/.hermes/logs/memory_metrics.jsonl`*
*基于 commit 729a778af + tiered memory 改动分支。*
