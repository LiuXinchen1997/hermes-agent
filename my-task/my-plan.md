# Hermes-Agent 记忆系统改造计划（demo）

> Demo 设计稿。聚焦 memory 子系统的分层与按需召回。
> 实现背景见 [code-analysis.md](code-analysis.md)。

---

## 0. 现状判断

Memory 子系统当前实现的三个结构性缺陷：

- `MemoryStore` 是平铺 `List[str]`，仅有字符上限校验，无 entry-level metadata（[memory_tool.py:107-142](../tools/memory_tool.py#L107-L142)）
- system prompt 全量注入冻结快照，所有条目权重相同，无相关度筛选
- 满载时 `add()` 直接 return error，无 graceful eviction（[memory_tool.py:250-261](../tools/memory_tool.py#L250-L261)）

净表现：撞 2200 字符即 reject；过期备忘和当前偏好同等占 prompt 预算；agent 学到新事实时必须先手动 remove 旧的腾位置。

---

## 1. 改造方案

### 1.1 三层结构

| Tier | 落地文件 | 上限 | 注入策略 |
|---|---|---|---|
| **T0** 长期用户画像 | `USER.md` (沿用) | 500 字符 (硬) | 会话起始一次性 freeze，整轮注入 system prompt |
| **T1** 工作记忆 | `WORKING.jsonl` (新) | 3000 字符 (软) | 每轮按相关度召回 top-5，注入 user message 头部 |
| **T2** 冷存档 | `COLD.jsonl` (新) | 无 | 不主动注入，`session_search` 可命中 |

T1 / T2 entry schema：

```jsonl
{"id": "ulid", "text": "...", "tier": 1,
 "created_at": "...", "last_recalled_at": "...",
 "recall_count": 0, "embedding": [f32; 512]}
```

Tier 转换只支持单向 T1 → T2（见 §1.3 eviction）。T2 → T1 和 T0 ↔ T1 都不做：T2 通过 `session_search` 单次取用即可，不必回流；T0 是用户身份，由用户/agent 显式写。

`memory(add)` 写入路径：注入 in-memory T1 列表 + 同步算 embedding 写盘（fastembed 单次推理 8-15ms，对工具响应可接受）。Tier 概念对 agent 完全透明——agent 看到的还是 `target="memory"` / `target="user"` 两个选项，和现状一致。

### 1.2 召回算法

每轮 user message 进来时触发一次：

```
score(e) = α · cosine(embed(msg), e.embedding)
         + β · exp(-Δt_recalled / τ_recency)
         + γ · log1p(e.recall_count)
```

- 默认参数：α=0.7, β=0.2, γ=0.1, τ_recency=14d
- 阈值过滤：cosine < 0.3 的 entry 一律不进 top-5
- 副作用：召回命中的 entry 更新 `last_recalled_at`、`recall_count++`
- 召回结果为空时**完全不注入**（避免空块占 token）

非空时注入到 user message 头部，格式：

```
<recalled-memory>
[System note: 以下是系统从历史记忆召回的相关条目，不是用户当前输入。]
- (3 days ago) 用户偏好用 pnpm 安装 npm 包，不用 npm/yarn
- (2 weeks ago) 项目计划 6 月底发 v2.0，期间避免大改架构
- (5 days ago) 团队使用 ruff 做 Python lint
</recalled-memory>
```

- 带 system note 防止模型把召回内容误读为用户当前输入
- 相对时间标注让模型有"这是旧信息"的判断依据
- 复用现有 [`StreamingContextScrubber`](../agent/memory_manager.py#L62-L200) 防止围栏标签泄漏

### 1.3 Eviction

`memory(add)` 在 T1 超限时不再 reject：

1. 计算 T1 所有 entry 的当前 score（不含 cosine 项，仅 recency + frequency）
2. 选 bottom-1 demote 到 T2：追加写 `COLD.jsonl`，从 `WORKING.jsonl` 删除
3. 重新校验容量，仍超限继续 demote

T1 → T2 是 entry 完整搬迁（包括 embedding），T2 保留所有 metadata 以便日后 `session_search` 仍能命中。

### 1.4 embedding 后端

- 默认 `BAAI/bge-small-zh-v1.5`，512-dim
- 加载方式：`fastembed` + ONNX runtime，on-disk 模型 ~95MB / 常驻 ~250MB
- fallback：模型加载失败（依赖缺失 / 平台不兼容）→ 退到 BM25 关键词检索

中英文混合场景目前先用 `bge-small-zh`（对中文 query 优化，但也能处理英文，质量略降）。Demo 阶段不做双模型分别索引。

### 1.5 与冻结快照机制兼容

[code-analysis.md §3.3](code-analysis.md) 的"冻结快照 vs 实时态"trade-off 保留，但**仅作用于 T0**：

- T0：会话起始 `load_from_disk()` 时 snapshot，整轮不变 → system prompt cache 稳定
- T1 召回内容注入到 user message，**与 system prompt cache 解耦**：本轮新写的 T1 条目，下一轮即可被召回（不必等下一会话重启）
- T2 不进 prompt

---

## 2. 数据流

```
user msg ──► MemoryRetriever.recall(msg, k=5)
                  │
                  ├─ embed(msg) 用 BGE-small
                  ├─ cosine on T1 embeddings + recency/freq 加权
                  ├─ filter: cosine < 0.3 丢弃
                  ▼
              top-K entries（可能为空）
                  │
                  ▼
         非空 → 注入 <recalled-memory> 到 user message 头部
                  │
                  ▼
              LLM API call
              （system prompt 仅含 T0 snapshot）
                  │
                  ▼
         副作用：召回命中条目的 recall_count++ / last_recalled_at = now

memory(add):
   1. 安全扫描（沿用现有 _scan_memory_content）
   2. embed(content) 同步算
   3. append 到 WORKING.jsonl
   4. 超 T1 容量 → 选 bottom-1 demote 到 COLD.jsonl
   5. 返回 tool response
```

---

## 3. 成本

| 项 | 影响 |
|---|---|
| 每轮新增 `<recalled-memory>` 块（非空时） | +300~500 tokens |
| 不再全量注入 T1 旧内容 | -1000~2000 tokens（T1 非空时） |
| 本地 embedding 推理 | 8-15ms / 次（CPU） |
| 索引常驻内存 | ~250MB |
| 模型 on-disk | ~95MB |

净效果：单轮 context 期望值下降；prompt cache 命中行为不变；MEMORY 有效容量从 2.2KB 扩展到 ~3KB 热区 + 无限冷区。

---

## 4. Feature flag 与失败模式

| 开关 | 默认 | 关掉之后 |
|---|---|---|
| `memory.tiering.enabled` | true | 回到平铺 MEMORY.md（现状） |
| `memory.retrieval.enabled` | true | T1 结构保留但不做召回 |

| 失败模式 | 处理 |
|---|---|
| embedding 模型加载失败 | 自动降级 BM25 关键词检索 |
| 召回噪声（top-K 都不相关） | cosine 阈值 0.3 拦截；不注入空块 |
| T2 文件膨胀 | 本期不处理（不进 prompt 不影响 token cost） |

---

*基于 main 分支 commit 729a778af。*
