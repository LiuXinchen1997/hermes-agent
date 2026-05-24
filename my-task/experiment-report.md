# 三层记忆系统优化实验报告

**日期**：2026-05-24  
**分支**：main（LiuXinchen1997/hermes-agent）  
**对比**：Mode A（旧版平铺 MemoryStore）vs Mode B（TieredMemoryStore + MemoryRetriever）

---

## 一、总结

本次实验通过 5 个测试用例，从任务完成率、Token 开销、召回精准度、知识容量四个维度量化验证了三层记忆系统的优化效果。TC-02 已接入真实 LLM（DeepSeek deepseek-chat）完成实测。

| 指标 | Mode A（基线）| Mode B（优化）| 变化 |
|------|-------------|-------------|------|
| 满载任务完成率（TC-01） | 0% | **100%** | +100 pp |
| 多轮一致性 MCS（TC-02，实测） | **6/6 = 100%** | **6/6 = 100%** | 持平；系统提示节省 −86% |
| 每轮注入字符数（TC-03A） | 990 字符 | **482 字符** | **−51.3%** |
| 召回延迟 P99（TC-03C） | — | **0.052 ms** | 远低于 10 ms 目标 |
| 召回精准度 P@5（TC-04） | 无（全量注入）| **77%** | 噪音率 80%→23% |
| 50 次写入后可检索条数（TC-05） | 24 / 50 | **50 / 50** | 可检索量 +108% |

---

## 二、TC-01 · 满载时任务完成率

**对应改动**：`TieredMemoryStore.add()` — 新系统满载时触发 T1→T2 驱逐，旧系统直接 reject。

**实验方案**：向两种存储各预写 14 条 ~154 字符的条目（合计 ~2156 字符，Mode A 剩余约 44 字符），再连续写入 5 条新记忆。

| 写入轮次 | Mode A 结果 | Mode B 结果 |
|---------|------------|------------|
| 第 15 条 | **REJECTED**（"Memory at 2,195/2,200 chars"） | ✅ 驱逐最低分 T1 条目后写入 |
| 第 16 条 | REJECTED | ✅ |
| 第 17 条 | REJECTED | ✅ |
| 第 18 条 | REJECTED | ✅ |
| 第 19 条 | REJECTED | ✅ |

**任务完成率（TCR）**：Mode A = **0/5 = 0%** | Mode B = **5/5 = 100%**

Mode A 一旦达到字符上限便永久拒绝写入，相关知识点彻底丢失。Mode B 每次写入前自动将 `recency + frequency` 分值最低的条目降级到 `COLD.jsonl`（T2），始终保持 T1 在预算之内。

---

## 三、TC-02 · 多轮对话一致性（LLM 实测）

**对应改动**：`conversation_loop.py` — 每轮 `user_message` 头部注入语义相关 top-K 条目，而非全量。

**实测环境**：DeepSeek `deepseek-chat`，脚本 `my-task/tc02_live_test.py`，原始结果 `my-task/tc02_results.json`。

**实验方案**：预写 20 条英文记忆（16 条覆盖 Python 工具链、JS 工具链、部署、数据库 4 个知识域，4 条**跨域干扰项**），进行 6 轮问答，每轮问题只关联其中一个域，评分标准：回答包含期望工具且未提及干扰工具为 PASS。

### 实测结果

| 轮次 | 查询 | Mode A | Mode B | Mode B 召回数 | 干扰泄漏 |
|------|------|--------|--------|-------------|---------|
| T1 | Python 项目如何配置 lint？ | ✅ ruff | ✅ ruff | 4 | 0 |
| T2 | JS 项目用什么包管理器？ | ✅ pnpm | ✅ pnpm | 5 | 1 (mocha 条目) |
| T3 | Python 如何写单元测试？ | ✅ pytest | ✅ pytest | 5 | 1 (npm 条目) |
| T4 | 如何构建前端生产包？ | ✅ vite | ✅ vite | 5 | 0 |
| T5 | Python 如何添加新依赖？ | ✅ poetry | ✅ poetry | 2 | 0 |
| T6 | 如何写组件快照测试？ | ✅ vitest | ✅ vitest | 3 | 1 (mocha 条目) |

| 指标 | Mode A | Mode B |
|------|--------|--------|
| **MCS（PASS / 总轮次）** | **6 / 6 = 100%** | **6 / 6 = 100%** |
| 系统提示字符数 | **1718 chars**（含 1510 chars 记忆块）| **206 chars**（仅基础 prompt）|
| 每轮注入记忆块大小 | 1510 chars（固定，全量）| ~200–400 chars（动态召回）|
| 6 轮中干扰项泄漏次数 | N/A（全量可见）| **3 / 6 次**（KeywordEmbedder 精度有限）|

### 关键发现

1. **答题质量持平**：DeepSeek 模型足够健壮，即使在 Mode A 全量注入 4 条干扰项的情况下也未被误导，两种模式均 MCS = 100%。
2. **系统提示大幅缩小**：Mode B 系统提示从 1718 chars 压缩到 206 chars（**−88%**），随着记忆库增长该优势线性放大——Mode A 每增加一条记忆系统提示就增长，Mode B 始终不变。
3. **KeywordEmbedder 精度局限**：Jaccard 关键词重叠法在 3/6 轮次中将不相关干扰项（mocha、npm）召回进了上下文。所幸 DeepSeek 能自行过滤，但若换用 FastembedEmbedder（真语义向量），干扰泄漏率预计可降至 0–1 次。
4. **召回块不污染历史**：Mode B 召回块只注入 API 发送的 `user_message`，不修改 `persist_user_message`（[conversation_loop.py:327-329](../agent/conversation_loop.py#L327-L329)），对话历史快照保持纯净。

---

## 四、TC-03 · Token 开销与 Prompt Cache 稳定性

### 4.1 每轮注入字符数（TC-03A）

**实验方案**：预写 11 条 Python 工具链相关的英文条目（KeywordEmbedder），查询语句为 "run ruff on the codebase before committing"。

| 模式 | System Prompt 中的记忆字符 | 每轮按需召回字符 | **每轮合计** |
|------|--------------------------|---------------|------------|
| Mode A | **990**（全量 T1 固定写入）| 0 | **990** |
| Mode B | ~148（仅 T0/USER.md）| 334（召回 5 条相关）| **482** |

**每轮 context 节省：990 → 482 = −51.3%**

随着记忆库增长，Mode A 的每轮注入量与记忆总量线性增长；Mode B 每轮最多注入 `k × avg_entry_len`（默认 k=5），与库容量无关，优势随时间持续扩大。

### 4.2 Prompt Cache 命中率（TC-03B，定性）

System prompt 是 prompt cache 的主键，其内容变化即导致 cache miss。

| 场景 | Mode A | Mode B |
|------|--------|--------|
| System prompt 内容 | T0 + **全量 T1**（随记忆写入不断变化） | **仅 T0**（USER.md，极少变动） |
| 每次 `memory(add)` 后下个会话是否 cache miss | **是**（T1 内容改变了 system prompt） | **否**（T0 不变，cache 命中）|
| 10 轮会话（每轮新增 1 条记忆）的缓存命中次数 | ~0–3 次 | ~9–10 次 |

验证方法：观察 Anthropic API 响应中的 `usage.cache_read_input_tokens`。Mode B 每轮应出现大量缓存命中；Mode A 仅首轮命中，后续因 memory 写入导致 system prompt 变化而持续 miss。

### 4.3 召回延迟基准（TC-03C）

**实验方案**：20 条 T1 条目，KeywordEmbedder，循环 1000 次 `recall()` 调用计时。

| 百分位 | 延迟 |
|--------|------|
| P50 | **0.041 ms** |
| P95 | **0.047 ms** |
| P99 | **0.052 ms** |
| Max | 0.135 ms |

全部百分位远低于 10 ms 目标。`batch_similarity()` 接口保证 query 只编码一次（O(N) 点积），即便 FastembedEmbedder（模型推理 ~5–15 ms）在典型 LLM API 500 ms+ 的往返延迟下也可忽略不计。

---

## 五、TC-04 · 召回精准度 P@5

**对应改动**：`MemoryRetriever.recall()` — 评分公式 `α·cosine + β·recency + γ·freq` 从混杂库中精准筛出相关条目。

**实验方案**：4 个知识域 × 4 条英文条目 = 16 条 T1，针对每个域分别发起查询（KeywordEmbedder）。

| 查询 | 目标域 | 相关条数 | 命中@K | P@K |
|------|--------|---------|--------|-----|
| "run linter before commit" | Python lint | 4 | 3/5 | **60%** |
| "bundle size optimization" | JS build | 3 | 2/3 | **67%** |
| "blue-green deployment" | 部署 | 4 | 1/1 | **100%** |
| "connection pool size" | 数据库 | 4 | 3/3 | **100%** |
| "CI pipeline timeout" | 部署 | 4 | 1/1 | **100%** |

**整体精准度**：10/13 = **P@5 = 77%**

| 模式 | LLM 每轮看到的噪音条目 | 噪音率 |
|------|----------------------|--------|
| Mode A（全量可见） | ~12/15 非相关条目 | **80%** |
| Mode B（top-K 召回） | 3/13 边界条目 | **23%** |

Mode B 将 LLM 侧噪音降低约 3.5 倍。23% 的残余噪音（3 条边界条目）反映了 KeywordEmbedder 基于词素重叠的局限性；启用 FastembedEmbedder 后精准度预期可提升至 80–90%。

---

## 六、TC-05 · 长期知识容量扩展

**对应改动**：T1→T2 驱逐机制 + `COLD.jsonl` 冷存档 — Mode A 满载后有效记忆冻结，Mode B 可检索知识库持续扩展。

**实验方案**：50 条各不相同的事实（每条 ~87 字符，总计 ~4350 字符，远超 Mode A 上限 2200），两种模式各从空白状态写入。

| 指标 | Mode A | Mode B |
|------|--------|--------|
| 写入成功条数 | **24 / 50** | **50 / 50** |
| 永久丢失条数 | **26** | 0 |
| T1（热区）条目 | 24 | 33 |
| T2（冷存）条目 | 0 | 17 |
| **可检索总量** | **24** | **50** |

Mode A 在第 25 条时硬性拒绝（"Memory at 2,173/2,200 chars"），之后 26 条事实**永久丢失**，无备份、无归档、无法恢复。

Mode B 成功写入全部 50 条。T1 达到 3000 字符预算时，自动将最低分条目驱逐到 `COLD.jsonl`（T2）。最终 T1 保留近期/高频的 33 条，T2 冷存 17 条，所有 50 条事实均在磁盘上保留，可按需取回。

---

## 七、结论

### 分维度评估

**任务完成率**：Mode B 通过 T1→T2 驱逐彻底消除了硬性写入失败，Mode A 在满载后静默丢弃关键信息。

**多轮一致性**：Mode B 每轮精准注入高相关条目，噪音率 80%→23%，预计可显著减少跨域干扰导致的错误回答（待 LLM 会话实测验证）。

**响应延迟**：召回开销 P99=0.052 ms，可忽略不计。Prompt cache 稳定性结构性改善：Mode A 每次记忆写入破坏缓存，Mode B 的 T0-only system prompt 跨会话持续命中缓存，可大幅降低 TTFT。

**知识容量**：Mode B 通过优雅驱逐存储 2 倍以上的事实，无任何数据永久丢失。

### 局限与后续工作

1. **TC-02 需要人工验证** — 需要接入真实 LLM 会话进行多轮评分
2. **KeywordEmbedder 对中文退化** — 中文无空格导致 Jaccard 重叠为 0，生产环境应启用 FastembedEmbedder（`uv pip install -e ".[memory-embeddings]"`）
3. **T2 无自动读回路径** — 冷存条目目前需手动 `cat COLD.jsonl` + 重新 `memory(add)` 取回（后续计划增加 `restore` API）
4. **驱逐策略待优化** — 当前仅用 recency + frequency 选驱逐目标，未考虑语义稀缺性，极端情况下可能驱逐掉"罕见但重要"的条目
