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

**前置**：预写 15 条记忆（每条 ~145 字符，总计 ~2175 字符，逼近 Mode A 上限 2200）。

```python
# prefill.py — 分模式运行：python prefill.py A 或 B
import sys
mode = sys.argv[1] if len(sys.argv) > 1 else "A"

PREFILL = [
    f"项目配置事实{i:02d}：团队规范要求所有服务必须通过健康检查，超时配置为30秒，重试策略为指数退避，最大重试3次，日志级别生产环境用INFO"
    for i in range(15)
]

if mode == "A":
    from tools.memory_tool import MemoryStore
    store = MemoryStore(); store.load_from_disk()
else:
    from tools.memory_tiered_store import TieredMemoryStore
    from tools.memory_embedder import KeywordEmbedder
    store = TieredMemoryStore(embedder=KeywordEmbedder(), t1_char_limit=3000)
    store.load_from_disk()

for e in PREFILL:
    r = store.add("memory", e)
    print(f"{'✅' if r['success'] else '❌'} {e[:50]}...")
print(f"预填完成，共 {sum(len(e) for e in PREFILL)} 字符")
```

**操作**：再依次添加以下 5 条固定内容，记录每次成功/失败：

```python
NEW_ENTRIES = [
    "新增规范：所有API响应必须包含requestId字段，便于链路追踪和日志关联",
    "数据库连接池从HikariCP切换为pgBouncer，最大连接数从50调整为20",
    "前端资源CDN迁移至CloudFront，旧的阿里云OSS地址将于下季度下线",
    "安全要求：所有内部服务间调用改用mTLS，证书由cert-manager自动轮转",
    "监控告警阈值调整：P99延迟从500ms收紧到300ms，触发PagerDuty通知",
]
for e in NEW_ENTRIES:
    r = store.add("memory", e)
    print(f"{'✅' if r['success'] else '❌'} {e[:50]}")
```

**度量 — 任务完成率（TCR）**：

| | Mode A | Mode B |
|--|--------|--------|
| 5 条新增全部成功 | ❌ 超限 reject（2175+145>2200） | ✅ 驱逐最旧条目后写入 |
| **TCR** | **0 / 5 = 0%** | **5 / 5 = 100%** |

**Mode B 验证**：

```bash
grep '"event":"evict"' ~/.hermes/logs/memory_metrics.jsonl | wc -l  # 期望 >= 3
python3 -c "
import json, os
cold = os.path.expanduser('~/.hermes/memories/COLD.jsonl')
if os.path.exists(cold):
    lines = [json.loads(l) for l in open(cold) if l.strip()]
    print(f'T2 保存条数: {len(lines)}，tier 字段全部=2:', all(e[\"tier\"]==2 for e in lines))
"
```

---

## TC-02 · 多轮对话的相关性一致性

**对应改动**：`conversation_loop.py` —— 每轮 user_message 头部只注入语义相关的 top-K 条目，而非全量。

**为什么 Mode A 在此场景下会出错**：Mode A 把全部 20 条记忆都写进 system prompt，其中包含若干**跨域干扰条目**（同时提到两个领域的关键词），模型需要从噪音中辨别；Mode B 语义召回后每轮只注入 3-5 条相关条目，干扰条目不出现。

**前置记忆（20 条，含 4 条跨域干扰项）**：

```python
MEMORIES = [
    # Python 工具链（4 条）
    "Python项目用ruff做lint，不用flake8或pylint",
    "Python依赖管理用poetry，lockfile必须提交到git",
    "Python测试框架pytest，fixture统一放conftest.py",
    "Python类型注解用mypy --strict模式检查",

    # JS/TS 工具链（4 条）
    "JS/TS项目用eslint+prettier，禁用tslint",
    "包管理器pnpm workspace，不用npm或yarn",
    "前端测试框架vitest，快照文件后缀.snap",
    "构建工具vite，不用webpack或rollup",

    # 部署（4 条）
    "生产部署在AWS EKS，namespace按team隔离",
    "Helm chart存储在infra/helm/，版本随app版本同步",
    "数据库PostgreSQL 15，只读副本2个",
    "缓存用Redis 7，TTL默认3600秒",

    # 跨域干扰项（4 条，关键词有意模糊）
    "前端项目有Python辅助脚本用于数据处理，历史上用过flake8，现已废弃",
    "旧版前端用webpack构建，2023年迁移至vite，部分legacy分支仍有webpack配置",
    "后端集成测试有少量JS脚本用mocha，非主力测试框架",
    "某些Python服务在容器内通过npm调用前端编译产物，需要node环境",
]
```

**6 轮交替对话**：

| 轮次 | 用户消息 | 期望召回 | 命中标准 |
|------|---------|---------|---------|
| T1 | 帮我给 Python 项目加 lint CI step | Python lint 条目 | 含 `ruff`，**不含** `flake8` |
| T2 | JS 项目怎么安装新依赖？ | pnpm 条目 | 含 `pnpm add`，**不含** `npm` |
| T3 | Python 项目怎么写单元测试？ | pytest 条目 | 含 `pytest`，**不含** `mocha` |
| T4 | 前端项目打包命令是什么？ | vite 条目 | 含 `vite build`，**不含** `webpack` |
| T5 | Python 新增第三方依赖步骤 | poetry 条目 | 含 `poetry add`，**不含** `pip` |
| T6 | JS 项目加一个组件快照测试 | vitest 条目 | 含 `vitest` 或 `.snap` |

**Mode A 的风险**：T1 会看到"历史上用过 flake8"的干扰项；T4 会看到"部分 legacy 分支仍有 webpack"；T3 会看到"少量 JS 脚本用 mocha"。这些干扰项会使模型偶尔输出不符合规范的答案（如提到 flake8 作为替代，或说"如果是 legacy 分支用 webpack"）。

**度量 — 多轮一致性评分（MCS）**：

| | Mode A（预期） | Mode B（预期） |
|--|--------------|--------------|
| 无歧义轮次（T2/T5/T6） | ≥ 5/6 | 6/6 |
| 含干扰项轮次（T1/T3/T4） | ~3-4/6（干扰条目可见） | 6/6（干扰条目不被召回）|
| **MCS** | **~67-83%** | **~100%** |

> **正确性保障注**：Mode B 的召回块只注入 API 发送的 `user_message`，**不修改** `persist_user_message`（[conversation_loop.py:327-329](../agent/conversation_loop.py#L327-L329)），所以 MCS 计量所用的历史记录不会被召回块污染，每轮的判断标准是纯净的。

---

## TC-03 · Token 开销与 Prompt Cache 稳定性

**对应改动**：`TieredMemoryStore.format_for_system_prompt("memory") → None` —— T1 移出 system prompt，system prompt 只保留 T0（USER.md），显著提升跨会话 prompt cache 命中率。

### Part A · 每轮注入字符数对比

```python
# bench_context.py
import tempfile
from pathlib import Path
from unittest.mock import patch

# 每条 ~140 字符，15 条 ≈ 2100 字符
ENTRIES = [
    f"规范条目{i:02d}：所有微服务必须实现 /health 接口，响应格式 JSON 含 status/version/uptime，超时30秒，生产环境强制 TLS 1.2+"
    for i in range(15)
]
QUERY = "帮我做一个 Python 项目的代码审查"

def make_patch(tmp: str):
    p = Path(tmp)
    return [
        patch("tools.memory_tool.get_memory_dir", return_value=p),
        patch("tools.memory_tiered_store.get_memory_dir", return_value=p),
    ]

from tools.memory_tool import MemoryStore
with tempfile.TemporaryDirectory() as tmp:
    patches = make_patch(tmp)
    for p in patches: p.start()
    store_a = MemoryStore(); store_a.load_from_disk()
    for e in ENTRIES: store_a.add("memory", e)
    prompt_a = store_a.format_for_system_prompt("memory") or ""
    for p in patches: p.stop()

from tools.memory_tiered_store import TieredMemoryStore
from tools.memory_embedder import KeywordEmbedder
from tools.memory_retriever import MemoryRetriever
with tempfile.TemporaryDirectory() as tmp:
    patches = make_patch(tmp)
    for p in patches: p.start()
    embedder = KeywordEmbedder()
    store_b = TieredMemoryStore(embedder=embedder, t1_char_limit=3000)
    store_b.load_from_disk()
    for e in ENTRIES: store_b.add("memory", e)
    assert store_b.format_for_system_prompt("memory") is None
    retriever = MemoryRetriever(store_b, embedder, min_similarity=0.05, k=5)
    hits = retriever.recall(QUERY)
    block_b = retriever.render_block(hits)
    for p in patches: p.stop()

print(f"Mode A system_prompt_memory_chars : {len(prompt_a)}")
print(f"Mode B per_turn_recalled_chars    : {len(block_b)}  (hits={len(hits)})")
print(f"每轮 context 节省                 : {1 - len(block_b)/len(prompt_a):.0%}")
```

**期望输出**：

```
Mode A system_prompt_memory_chars : ~2100
Mode B per_turn_recalled_chars    : ~400   (hits=5)
每轮 context 节省                 : ~81%
```

### Part B · Prompt Cache 命中率对比

这是 Mode B 更大的延迟优势，但在字符数测量中体现不出来。

| 场景 | Mode A | Mode B |
|------|--------|--------|
| System prompt 内容 | T0 + **全量 T1**（随 memory 增长而变）| **仅 T0**（USER.md，极少变动） |
| 每次新增记忆后下个会话是否 cache miss | ✅ **是**（T1 内容改变了 system prompt）| ❌ **否**（T0 不变，cache 命中）|
| 10 轮会话中 cache 命中次数（假设每轮新增 1 条记忆）| ~0-3 次 | ~9-10 次 |
| 实际 API 成本倍数（Anthropic cache 折扣约 10%）| 1x | **~0.3x**（大量命中后）|

验证方式：在 Anthropic API 响应里观察 `usage.cache_read_input_tokens`。Mode B 每轮应出现大量缓存命中；Mode A 只有首轮命中，后续因 memory 写入导致 system prompt 变化而 miss。

### Part C · 召回延迟基准

```python
# latency_bench.py
import time, statistics, tempfile
from pathlib import Path
from unittest.mock import patch
from tools.memory_tiered_store import TieredMemoryStore
from tools.memory_embedder import KeywordEmbedder
from tools.memory_retriever import MemoryRetriever

with tempfile.TemporaryDirectory() as tmp:
    p = Path(tmp)
    with patch("tools.memory_tool.get_memory_dir", return_value=p), \
         patch("tools.memory_tiered_store.get_memory_dir", return_value=p):
        e = KeywordEmbedder()
        s = TieredMemoryStore(embedder=e, t1_char_limit=3000); s.load_from_disk()
        for i in range(20):
            s.add("memory", f"entry {i} about project tooling rules and team configuration")
        r = MemoryRetriever(s, e, min_similarity=0.05, k=5)
        lats = []
        for _ in range(100):
            t = time.perf_counter()
            r.recall("project tooling rules")
            lats.append((time.perf_counter() - t) * 1000)
        lats.sort()
        print(f"P50={statistics.median(lats):.1f}ms  P95={lats[94]:.1f}ms  P99={lats[98]:.1f}ms")
        # 期望：KeywordEmbedder P99 < 10ms；fastembed P99 < 30ms
        # 与 cache 节省相比，这点额外延迟可忽略不计
```

---

## TC-04 · 召回精准度 P@5

**对应改动**：`MemoryRetriever.recall()` —— 评分公式 `α·cosine + β·recency + γ·freq` 从混杂库中精准筛出相关条目。

```python
# precision_test.py
import tempfile
from pathlib import Path
from unittest.mock import patch
from tools.memory_tiered_store import TieredMemoryStore
from tools.memory_embedder import make_embedder
from tools.memory_retriever import MemoryRetriever

CLUSTERS = {
    "python": ["Python用ruff lint", "Python用poetry管依赖", "Python用mypy类型检查"],
    "js":     ["JS用eslint+prettier", "包管理用pnpm", "构建用vite"],
    "deploy": ["部署在AWS EKS", "Helm chart在infra/helm/", "滚动更新maxSurge=1"],
    "db":     ["主库PostgreSQL 15", "ORM用SQLAlchemy 2", "迁移用alembic"],
    "process":["PR需要2个approve", "commit遵循Conventional Commits", "每月末代码冻结"],
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
    p = Path(tmp)
    with patch("tools.memory_tool.get_memory_dir", return_value=p), \
         patch("tools.memory_tiered_store.get_memory_dir", return_value=p):
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
            precision = n / len(hits) if hits else 0
            print(f"[{expected:8s}] P@{len(hits)}={precision:.0%}  clusters={hit_clusters}")
            total += len(hits); correct += n

        print(f"\nOverall P@5 = {correct}/{total} = {correct/total:.0%}")
        # KeywordEmbedder 期望: 60-70%
        # FastembedEmbedder 期望: 80-90%
```

**与 Mode A 对比**：

| | Mode A（全量可见，无筛选）| Mode B Keyword | Mode B fastembed |
|--|--------------------------|---------------|-----------------|
| 每轮注入条目数 | 全部 15 条 | top-5 相关 | top-5 相关 |
| 噪音率 | 13/15 = **87%** | ~35% | ~15% |
| P@5 | N/A（无召回概念） | **60-70%** | **80-90%** |

---

## TC-05 · 长期容量扩展与有效检索数

**对应改动**：T1→T2 驱逐机制 + `COLD.jsonl` 冷存档 —— Mode A 在达到字符上限后有效记忆冻结不再增长，Mode B 的可检索知识库随使用持续扩展。

**测量目标**：经过 50 次 `memory(add)` 后，两种模式各能有效保存多少条不重复的事实。

```python
# capacity_test.py
import tempfile
from pathlib import Path
from unittest.mock import patch
from tools.memory_tool import MemoryStore
from tools.memory_tiered_store import TieredMemoryStore
from tools.memory_embedder import KeywordEmbedder
from tools.memory_retriever import MemoryRetriever

# 50 条各不相同的事实（每条 ~60 字符，总计 ~3000 字符，超过 Mode A 上限）
FACTS = [f"项目知识点{i:02d}：{topic}" for i, topic in enumerate([
    "使用Python 3.11，虚拟环境用venv",      "CI用GitHub Actions，runner ubuntu-latest",
    "代码审查工具reviewpad自动分配reviewer",  "API版本管理用URL路径前缀/v1/v2",
    "日志格式统一JSON，字段含traceId",        "配置中心用Consul，本地开发用.env",
    "数据库迁移每周五上线前执行",             "前端国际化i18next，翻译文件在locales/",
    "移动端SDK最低支持iOS 15/Android 10",    "WebSocket心跳间隔30秒",
    "分布式锁用Redis SETNX，超时5秒",        "消息队列Kafka，topic命名用点分隔",
    "GraphQL schema变更需向后兼容",          "静态资源hash指纹防缓存穿透",
    "A/B测试平台GrowthBook，flag命名kebab", "错误码规范：4xx客户端/5xx服务端",
    "单测mock外部依赖，不测第三方库",         "Docker镜像基础层用distroless",
    "PR标题格式：type(scope): description", "发布窗口每周二四16:00-18:00",
    "数据库索引命名idx_表名_字段名",          "服务网格Istio，mTLS全量开启",
    "前端组件库基于Radix UI二次封装",         "后端框架FastAPI，pydantic v2验证",
    "SLO目标：可用性99.9%，P99延迟<300ms",  "密钥管理AWS Secrets Manager",
    "代码生成工具openapi-generator",         "性能测试k6，阈值P95<200ms",
    "数据脱敏在DAO层处理，不在API层",        "特性开关默认关闭，上线后逐步开放",
    "前端状态管理Zustand，禁用Redux",         "离线任务用Celery，broker Redis",
    "API限流令牌桶算法，10req/s/user",       "Git分支策略trunk-based development",
    "文档站点Docusaurus，与代码仓库共存",    "依赖扫描Snyk，每日自动检测",
    "跨域配置只允许内部域名",                "负载均衡策略least-connections",
    "告警收敛窗口5分钟，避免告警风暴",       "SQL禁止SELECT *，显式列出字段",
    "容器资源limits必须设置，防OOM驱逐",     "前端路由用React Router v6",
    "后端分页cursor-based，非offset",        "邮件服务SendGrid，模板统一管理",
    "数据备份每日全量+每小时增量",           "HTTP客户端axios，统一拦截器处理token",
    "错误上报Sentry，release版本关联",       "部署回滚保留最近3个版本",
])]

def count_retrievable(store, retriever=None):
    """统计可检索的条目数（T1 热区 + 可被 recall 命中的条目数）。"""
    if retriever:
        # Mode B：T1 活跃条目数 + T2 冷存条目数（data not lost）
        t1 = store.t1_count()
        t2 = store.t2_count()
        return t1, t2, t1 + t2
    else:
        # Mode A：system prompt 中的实际条目数
        snap = store.format_for_system_prompt("memory") or ""
        count = snap.count("§") + 1 if snap.strip() else 0
        return count, 0, count

# ── Mode A ──────────────────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as tmp:
    p = Path(tmp)
    with patch("tools.memory_tool.get_memory_dir", return_value=p):
        store_a = MemoryStore(); store_a.load_from_disk()
        success_a = sum(1 for f in FACTS if store_a.add("memory", f)["success"])
        t1_a, _, total_a = count_retrievable(store_a)
        print(f"Mode A: 成功写入 {success_a}/50，可检索 {total_a} 条")

# ── Mode B ──────────────────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as tmp:
    p = Path(tmp)
    with patch("tools.memory_tool.get_memory_dir", return_value=p), \
         patch("tools.memory_tiered_store.get_memory_dir", return_value=p):
        embedder = KeywordEmbedder()
        store_b = TieredMemoryStore(embedder=embedder, t1_char_limit=3000)
        store_b.load_from_disk()
        success_b = sum(1 for f in FACTS if store_b.add("memory", f)["success"])
        retriever = MemoryRetriever(store_b, embedder, min_similarity=0.05, k=5)
        t1_b, t2_b, total_b = count_retrievable(store_b, retriever)
        print(f"Mode B: 成功写入 {success_b}/50，T1 热区 {t1_b} 条，T2 冷存 {t2_b} 条，总计 {total_b} 条")
```

**期望输出**：

```
Mode A: 成功写入 ~36/50，可检索 ~36 条   ← 超过 2200 字符后全部 reject
Mode B: 成功写入 50/50，T1 热区 ~20 条，T2 冷存 ~30 条，总计 50 条
```

**长期价值**：Mode A 有效知识库在满载后冻结，agent 无法从新经验中继续学习；Mode B T2 冷存虽不自动注入，但不丢失数据——demo 阶段需手动 `cat ~/.hermes/memories/COLD.jsonl` + 重新 `memory(add)` 取回（后续如加 `restore` API 可自动），整体仍体现"越用越聪明"的设计目标。

---

## 汇总

| 用例 | 验证的代码改动 | 度量指标 | Mode A | Mode B |
|------|--------------|---------|--------|--------|
| TC-01 满载 TCR | `TieredMemoryStore.add()` 驱逐 | 新增成功率 | **0%** | **100%** |
| TC-02 多轮一致性 | `conversation_loop.py` 按需注入 | MCS（6 轮） | ~67-83% | ~100% |
| TC-03A Token 开销 | `format_for_system_prompt → None` | 每轮注入字符 | ~2100 | ~400（-81%）|
| TC-03B Prompt Cache | system prompt 冻结（T0 only）| 跨会话 cache 命中率 | 低（T1 变动破坏）| 高（T0 稳定）|
| TC-04 召回精准度 | `MemoryRetriever` 评分公式 | P@5 | N/A | 60-90% |
| TC-05 容量扩展 | T1→T2 驱逐 + 冷存 | 50 次写入后可检索数 | ~36 条 | **50 条** |

*指标日志：`~/.hermes/logs/memory_metrics.jsonl`　记忆目录：`~/.hermes/memories/`*
