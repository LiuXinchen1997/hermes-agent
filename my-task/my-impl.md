# 代码改动总结：三层记忆系统（Tiered Memory）

## 整体目标

将 agent 原有的"平铺式"记忆存储（MEMORY.md 单文件）升级为**三层分级记忆系统**，引入向量语义检索，让 agent 在每轮对话时能自动回忆出与当前问题最相关的记忆条目，而不是把所有记忆都堆进 system prompt。

---

## 新增文件

### `tools/memory_tiered_store.py` — 核心存储层

定义 `TieredMemoryStore`（继承自原有 `MemoryStore`），实现三层结构：

| 层级 | 文件 | 说明 |
|------|------|------|
| T0 | `USER.md` | 长期用户画像，继承自 MemoryStore，始终注入 system prompt |
| T1 | `WORKING.jsonl` | 工作记忆，带嵌入向量，按需召回 |
| T2 | `COLD.jsonl` | 冷存档，只追加不自动注入 |

核心机制：
- **T1 → T2 自动驱逐**：新增记忆后若 T1 总字符超过上限（默认 3000），自动将"分数最低"的条目降级到 T2（依据近期使用时间 + 召回频次打分选出受害者）。
- **一次性迁移**：首次启动时如果存在旧的 MEMORY.md，自动将其内容迁移为 WORKING.jsonl 中的 T1 条目。
- **兼容旧接口**：`add/replace/remove/format_for_system_prompt` 方法签名不变，"memory" 目标走 T1 路径，"user" 目标透传给 super()。

### `tools/memory_embedder.py` — 嵌入后端

提供统一的 `Embedder` 接口，两种实现：

- **`FastembedEmbedder`**：使用 `fastembed` + ONNX 运行 `BAAI/bge-small-zh-v1.5`，返回真实语义向量，余弦相似度。
- **`KeywordEmbedder`**：基于 Jaccard token 集合重叠率，无需任何模型，降级兜底。

`make_embedder(prefer_local=True)` 工厂函数：优先尝试 FastembedEmbedder，失败则自动降级到 KeywordEmbedder，不会因为缺少依赖而报错。

### `tools/memory_retriever.py` — 每轮召回引擎

`MemoryRetriever` 在每次对话轮次开始时对 T1 中所有条目打分，返回最相关的 top-K：

```
score(e) = α · cosine(embed(msg), e.embedding)
         + β · exp(-Δt_recalled / τ_recency)
         + γ · log1p(e.recall_count)
```

- **α**（默认 0.7）：语义相似度权重
- **β**（默认 0.2）：近期使用衰减权重（τ=14天半衰期）
- **γ**（默认 0.1）：召回频次权重

召回命中的条目自动更新 `last_recalled_at` 和 `recall_count`（持久化到磁盘）。

`render_block()` 将命中条目格式化为 `<memory-context>` XML 块，注入用户消息头部。

### `tools/memory_metrics.py` — 指标记录器

以 append-only JSONL 格式写入 `~/.hermes/logs/memory_metrics.jsonl`，记录 recall/add/replace/remove/evict 事件，用于评估分层效果。所有写入失败静默吞掉，不影响主流程。

### `tests/tools/test_memory_tiering.py` — 测试套件

覆盖 Entry 序列化、TieredMemoryStore 增删改、MemoryRetriever 召回逻辑、KeywordEmbedder 相似度等核心路径，全部使用 KeywordEmbedder（无需模型）。

---

## 修改文件

### `agent/agent_init.py` — Feature Flag 接入

在 agent 初始化时，通过配置项 `memory.tiering.enabled` 决定使用哪套存储：

```
memory.tiering.enabled = true  →  TieredMemoryStore + MemoryRetriever
memory.tiering.enabled = false →  原有 MemoryStore（不变）
```

初始化失败时自动回退到旧 MemoryStore，**保证 agent 永远不会因记忆模块异常而拒绝启动**。

同时在 `agent._memory_retriever` 上挂载 `MemoryRetriever` 实例（未开启分层时为 None）。

### `agent/conversation_loop.py` — 每轮消息前注入

在 `run_conversation()` 组装 API 请求之前，若 `_memory_retriever` 存在：

1. 对当前用户消息做 recall，取得相关 T1 条目
2. 将 `<memory-context>` 块拼接到 **API 发送的 user_message 头部**
3. **不修改 `persist_user_message`**，确保召回块不会污染对话历史、外部记忆同步或 background review 的对话快照

### `pyproject.toml` — 新增可选依赖

```toml
memory-embeddings = ["fastembed>=0.3.0"]
```

安装此 extra 才能使用本地语义嵌入；不安装则自动降级为 Jaccard 关键词匹配，功能可用但召回质量较弱。

---

## 关键设计决策

1. **完全向后兼容**：未开启 `memory.tiering.enabled` 时，代码路径与原来完全相同。
2. **多级兜底**：fastembed 不可用 → KeywordEmbedder；TieredMemoryStore 初始化失败 → 旧 MemoryStore；recall 报错 → 静默跳过，不中断对话。
3. **召回块只送给 API，不持久化**：避免污染历史记录和外部同步，每轮都基于真实用户消息重新召回。
4. **性能考量**：`batch_similarity()` 接口保证 query 只编码一次（对模型后端 O(N) 节省至关重要）。

---

## 本地部署与运行

需要 Python 3.11+，推荐用 [uv](https://docs.astral.sh/uv/) 管 venv。

```bash
# 1. 从 fork 克隆（改动还没合到上游 NousResearch）
git clone --recurse-submodules https://github.com/LiuXinchen1997/hermes-agent.git
cd hermes-agent

# 2. 装 hermes 本身 + 依赖（在 clone 出来的目录下跑）
#    `-e .`  ─ 把当前目录作为「可编辑包」安装，这一步就是 *安装 hermes*
#    `[all]` ─ Hermes 全功能 extras（多 provider、TTS、网关等）
#    `[dev]` ─ pytest / ruff 等开发工具，跑测试用
#    `[memory-embeddings]` ─ 本次改动专属：本地 BGE 嵌入模型（不装会自动降级到 Jaccard 关键词匹配）
uv venv venv --python 3.11
export VIRTUAL_ENV="$(pwd)/venv"
uv pip install -e ".[all,dev,memory-embeddings]"

# 装完后会有 venv/bin/hermes 可执行文件，验证一下：
venv/bin/hermes --version

# 3. 配 LLM key（按 Hermes 标准流程，如还没配过）
mkdir -p ~/.hermes
cp cli-config.yaml.example ~/.hermes/config.yaml
echo "OPENROUTER_API_KEY=你的key" >> ~/.hermes/.env  # 或 ANTHROPIC_API_KEY

# 4. 开 tiering（编辑 ~/.hermes/config.yaml 在 memory: 段下加）
#    memory:
#      tiering:
#        enabled: true
#    其它键全部用默认即可；完整字段见 docs/memory_tiering.md

# 5. 启动
venv/bin/hermes
```

首次启动时若已有旧 `~/.hermes/memories/MEMORY.md`，会自动迁移为 `WORKING.jsonl`（原文件保留作备份）。

跑测试：

```bash
venv/bin/pytest tests/tools/test_memory_tiering.py -v
```
