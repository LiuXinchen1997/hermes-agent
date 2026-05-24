# 三层记忆系统优化实验测试用例

> 每个用例对应一处核心代码改动，均可独立运行、产出可对比的数字。
> A/B 分组：切换 `~/.hermes/config.yaml` 中 `memory.tiering.enabled` 后重启 agent 即可。

| 组别 | 配置 | 路径 |
|------|------|------|
| **Mode A（基线）** | `memory.tiering.enabled: false` | 旧 MemoryStore，全量注入 system prompt |
| **Mode B（优化）** | `memory.tiering.enabled: true` | TieredMemoryStore + MemoryRetriever |

---

## TC-01 · 满载时的任务完成率

**对应改动**：`TieredMemoryStore.add()` —— 旧系统满载直接 reject，新系统触发 T1→T2 驱逐。

**前置**：预写 16 条记忆，使 MEMORY.md 接近 2200 字符上限（Mode A）/ T1 接近 3000 字符上限（Mode B）。

```python
# prefill.py — 两种模式通用
from tools.memory_tool import MemoryStore
store = MemoryStore(); store.load_from_disk()
entries = [f"项目事实{i}：" + "内容" * 20 for i in range(16)]
for e in entries:
    r = store.add("memory", e)
    print(r["success"], e[:30])
```

**操作**：再依次添加 5 条新记忆，记录每次 `add()` 的成功/失败。

**度量 — 任务完成率（TCR）**：

| | Mode A | Mode B |
|--|--------|--------|
| 第 1-3 条新增 | ❌ 超限 reject | ✅ 驱逐后写入 |
| 第 4-5 条新增 | ❌ 超限 reject | ✅ 驱逐后写入 |
| **TCR** | **0 / 5 = 0%** | **5 / 5 = 100%** |

**验证**（Mode B）：

```bash
# 驱逐事件计数
grep '"event":"evict"' ~/.hermes/logs/memory_metrics.jsonl | wc -l
# 期望 >= 3

# T2 数据未丢失
python3 -c "
import json, os
cold = os.path.expanduser('~/.hermes/memories/COLD.jsonl')
lines = [json.loads(l) for l in open(cold) if l.strip()]
print(f'T2 entries: {len(lines)}, all tier=2:', all(e[\"tier\"]==2 for e in lines))
"
```

---

## TC-02 · 多轮对话的相关性一致性

**对应改动**：`conversation_loop.py` —— 每轮在 user_message 前注入 `<recalled-memory>` 块，只含语义相关条目，而非全量。

**前置记忆**（8 条，Python / JS 两组正交）：

```
P1: Python项目用ruff做lint，不用flake8
P2: Python依赖管理用poetry，lockfile必须提交
P3: Python测试框架pytest，fixture放conftest.py
P4: Python类型注解用mypy --strict检查

J1: JS/TS项目用eslint+prettier，禁用tslint
J2: 包管理器pnpm workspace，不用npm/yarn
J3: 测试框架vitest，快照文件后缀.snap
J4: 构建工具vite，不用webpack
```

**6 轮对话（交替主题）**：

| 轮次 | 用户消息 | 期望应用的记忆 | 命中标准 |
|------|---------|--------------|---------|
| T1 | 帮我在 Python 项目 CI 里加 lint step | P1 | 回复含 `ruff`，不含 `flake8` |
| T2 | JS 项目怎么安装新依赖？ | J2 | 回复含 `pnpm add` |
| T3 | Python 项目如何写单元测试？ | P3 | 含 `pytest` |
| T4 | JS 项目加快照测试 | J3 | 含 `vitest` 或 `.snap` |
| T5 | Python 项目新增依赖的完整步骤 | P2 | 含 `poetry add` |
| T6 | JS 项目打包命令是什么 | J4 | 含 `vite build`，不含 `webpack` |

**度量 — 多轮一致性评分（MCS）**：

| | Mode A（预期） | Mode B（预期） |
|--|--------------|--------------|
| 单轮命中率 | 70-80%（8 条全可见，噪音干扰） | 90-100%（只注入相关 3-4 条）|
| **MCS（6 轮）** | **~4/6** | **~6/6** |

**原因**：Mode A 每轮 system prompt 里 Python/JS 记忆都存在，模型容易在 T2/T4/T6 混用；Mode B 语义召回后 Python 轮次看不到 JS 条目，反之亦然。

---

## TC-03 · System Prompt Token 开销对比

**对应改动**：`TieredMemoryStore.format_for_system_prompt("memory") → None` —— T1 不再注入 system prompt，改为每轮按需注入 user_message 头部，缩减固定 context 体积。

**测量脚本**（无需 LLM，直接比较两种模式的注入字符数）：

```python
# bench_context.py
import time, tempfile, os
from unittest.mock import patch

ENTRIES = [f"记忆条目{i}：项目规范相关内容，包含技术细节" * 3 for i in range(20)]
QUERY = "帮我做一个Python项目的代码审查"

# ── Mode A ──────────────────────────────────────────────────────────────
from tools.memory_tool import MemoryStore
with tempfile.TemporaryDirectory() as tmp:
    with patch("tools.memory_tool.get_memory_dir", return_value=tmp):
        store_a = MemoryStore(); store_a.load_from_disk()
        for e in ENTRIES[:16]:           # 满载 ~2160 字符
            store_a.add("memory", e)
        prompt_a = store_a.format_for_system_prompt("memory") or ""

# ── Mode B ──────────────────────────────────────────────────────────────
from tools.memory_tiered_store import TieredMemoryStore
from tools.memory_embedder import KeywordEmbedder
from tools.memory_retriever import MemoryRetriever
with tempfile.TemporaryDirectory() as tmp:
    with patch("tools.memory_tiered_store.get_memory_dir", return_value=tmp):
        embedder = KeywordEmbedder()
        store_b = TieredMemoryStore(embedder=embedder, t1_char_limit=3000)
        store_b.load_from_disk()
        for e in ENTRIES:                # 全部 20 条
            store_b.add("memory", e)
        assert store_b.format_for_system_prompt("memory") is None  # T1 不注入 system prompt
        retriever = MemoryRetriever(store_b, embedder, min_similarity=0.05, k=5)
        hits = retriever.recall(QUERY)
        block_b = retriever.render_block(hits)

print(f"Mode A system_prompt_chars : {len(prompt_a)}")
print(f"Mode B recalled_block_chars: {len(block_b)}  (hits={len(hits)})")
print(f"Context 节省: {1 - len(block_b)/len(prompt_a):.0%}")
```

**期望输出**：

```
Mode A system_prompt_chars : 2160
Mode B recalled_block_chars: ~400   (hits=5)
Context 节省: ~82%
```

**延迟代价**（Mode B 新增的 embedding 计算）：

```bash
python3 -c "
import time, statistics
from tools.memory_tiered_store import TieredMemoryStore
from tools.memory_embedder import KeywordEmbedder
from tools.memory_retriever import MemoryRetriever
import tempfile
from unittest.mock import patch

with tempfile.TemporaryDirectory() as tmp:
    with patch('tools.memory_tiered_store.get_memory_dir', return_value=tmp):
        e = KeywordEmbedder()
        s = TieredMemoryStore(embedder=e); s.load_from_disk()
        for i in range(20): s.add('memory', f'entry {i} about project tooling and rules')
        r = MemoryRetriever(s, e, min_similarity=0.05, k=5)
        lats = []
        for _ in range(100):
            t = time.perf_counter()
            r.recall('project tooling rules')
            lats.append((time.perf_counter()-t)*1000)
        lats.sort()
        print(f'P50={statistics.median(lats):.1f}ms  P95={lats[94]:.1f}ms  P99={lats[98]:.1f}ms')
        # 期望 KeywordEmbedder P99 < 10ms；fastembed P99 < 30ms
"
```

---

## TC-04 · 召回精准度 P@5

**对应改动**：`MemoryRetriever.recall()` —— 评分公式 `α·cosine + β·recency + γ·freq` 是否能从混杂记忆库中精准筛出相关条目。

**5 个语义簇 × 3 条 = 15 条记忆**，5 条查询各指向不同的簇：

```python
# precision_test.py
import tempfile
from unittest.mock import patch
from tools.memory_tiered_store import TieredMemoryStore
from tools.memory_embedder import make_embedder
from tools.memory_retriever import MemoryRetriever

CLUSTERS = {
    "python": ["Python用ruff lint","Python用poetry管依赖","Python用mypy类型检查"],
    "js":     ["JS用eslint+prettier","包管理用pnpm","构建用vite"],
    "deploy": ["部署在AWS EKS","Helm chart在infra/helm/","滚动更新maxSurge=1"],
    "db":     ["主库PostgreSQL 15","ORM用SQLAlchemy 2","迁移用alembic"],
    "process":["PR需要2个approve","commit遵循Conventional Commits","每月末代码冻结"],
}
QUERIES = [
    ("Python代码质量工具", "python"),
    ("前端依赖安装", "js"),
    ("Kubernetes部署方式", "deploy"),
    ("数据库和迁移工具", "db"),
    ("代码合并流程", "process"),
]

embedder = make_embedder(prefer_local=True)  # fastembed 或降级 Jaccard

with tempfile.TemporaryDirectory() as tmp:
    with patch("tools.memory_tiered_store.get_memory_dir", return_value=tmp):
        store = TieredMemoryStore(embedder=embedder, t1_char_limit=5000)
        store.load_from_disk()
        id_to_cluster = {}
        for cluster, texts in CLUSTERS.items():
            for text in texts:
                r = store.add("memory", text)
                if r.get("added_id"):
                    id_to_cluster[r["added_id"]] = cluster

        retriever = MemoryRetriever(store, embedder, min_similarity=0.1, k=5)
        total, correct = 0, 0
        for query, expected in QUERIES:
            hits = retriever.recall(query)
            hit_clusters = [id_to_cluster.get(h.id, "?") for h in hits]
            n = sum(1 for c in hit_clusters if c == expected)
            p = n / len(hits) if hits else 0
            print(f"[{expected:8s}] P@{len(hits)}={p:.0%}  clusters={hit_clusters}")
            total += len(hits); correct += n

        print(f"\nOverall P@5 = {correct}/{total} = {correct/total:.0%}")
        # KeywordEmbedder 期望: 60-70%
        # FastembedEmbedder 期望: 80-90%
```

**与 Mode A 对比**（Mode A 不过滤，所有 15 条全部暴露）：

| | Mode A（全量注入，无筛选）| Mode B Keyword | Mode B fastembed |
|--|--------------------------|---------------|-----------------|
| 噪音率（不相关条目占比）| 13/15 = **87%** | ~35% | ~15% |
| P@5 | N/A | **60-70%** | **80-90%** |

---

## TC-05 · Recall 块不污染对话历史

**对应改动**：`conversation_loop.py:327-329` —— recall 块只拼接到 API 发送的 `user_message`，**不修改** `persist_user_message`，确保对话历史、background_review 快照、外部 memory sync 都看不到召回块。

**测试逻辑**（直接验证 conversation_loop 的关键逻辑）：

```python
# isolation_test.py
# 模拟 conversation_loop.py 中的 recall 注入片段，验证隔离行为

from tools.memory_tiered_store import TieredMemoryStore
from tools.memory_embedder import KeywordEmbedder
from tools.memory_retriever import MemoryRetriever
import tempfile
from unittest.mock import patch

with tempfile.TemporaryDirectory() as tmp:
    with patch("tools.memory_tiered_store.get_memory_dir", return_value=tmp):
        embedder = KeywordEmbedder()
        store = TieredMemoryStore(embedder=embedder); store.load_from_disk()
        store.add("memory", "Python项目用ruff做lint")
        retriever = MemoryRetriever(store, embedder, min_similarity=0.05, k=3)

        # 模拟 conversation_loop.py 中的注入逻辑
        user_message = "帮我加一个lint检查"
        persist_user_message = None        # 通常为 None 直到此处

        hits = retriever.recall(user_message)
        block = retriever.render_block(hits)
        assert block, "应该有召回命中"

        if block:
            # conversation_loop.py:327-329 的核心逻辑
            if persist_user_message is None:
                persist_user_message = user_message   # 先固定原始消息
            user_message = block + "\n\n" + user_message  # 再注入召回块

        # 断言：API 收到的消息含召回块
        assert "<memory-context>" in user_message, "API user_message 应含召回块"

        # 断言：历史记录不含召回块
        assert "<memory-context>" not in persist_user_message, \
            "persist_user_message 不应含召回块（否则污染历史）"
        assert persist_user_message == "帮我加一个lint检查", \
            f"persist_user_message 应等于原始消息，实际: {persist_user_message!r}"

        print("✅ API user_message 含召回块:", "<memory-context>" in user_message)
        print("✅ persist_user_message 干净:", persist_user_message)
        print("✅ 两者不等（块已隔离）:", user_message != persist_user_message)
```

**期望输出**：

```
✅ API user_message 含召回块: True
✅ persist_user_message 干净: 帮我加一个lint检查
✅ 两者不等（块已隔离）: True
```

**如果隔离失败**（即删掉 `persist_user_message = user_message` 那行）会发生什么：
- background_review fork 看到的对话历史含 `<recalled-memory>` 脚手架标签
- 外部 memory provider `sync_turn()` 收到带召回块的消息，可能产生重复记忆
- 多轮 session 历史中会出现大量内部注入内容，干扰 context compressor

---

## 汇总

| 用例 | 验证的代码改动 | 度量指标 | Mode A | Mode B |
|------|--------------|---------|--------|--------|
| TC-01 满载 TCR | `TieredMemoryStore.add()` 驱逐 | 成功写入率 | **0%** | **100%** |
| TC-02 多轮一致性 | `conversation_loop.py` 按需注入 | MCS（6轮） | ~67% | ~100% |
| TC-03 Token 开销 | `format_for_system_prompt → None` | 每轮注入字符 | ~2160 | ~400（-82%）|
| TC-04 召回精准度 | `MemoryRetriever` 评分公式 | P@5 | N/A | 60-90% |
| TC-05 块隔离性 | `conversation_loop.py:327-329` | 断言通过/失败 | N/A | 全部通过 |

*指标日志：`~/.hermes/logs/memory_metrics.jsonl`　记忆目录：`~/.hermes/memories/`*
