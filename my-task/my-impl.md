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

`render_block()` 将命中条目格式化为 `<recalled-memory>` XML 块，注入用户消息头部。

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
2. 将 `<recalled-memory>` 块拼接到 **API 发送的 user_message 头部**
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

### 环境要求

| 依赖 | 版本 | 说明 |
|------|------|------|
| Python | 3.11+ | 必须 |
| uv | 最新 | 推荐的包管理器，[安装文档](https://docs.astral.sh/uv/) |
| Node.js | 20+ | 可选，仅浏览器工具需要 |

### 第一步：克隆并安装

```bash
git clone --recurse-submodules https://github.com/NousResearch/hermes-agent.git
cd hermes-agent

# 创建 Python 3.11 虚拟环境
uv venv venv --python 3.11
export VIRTUAL_ENV="$(pwd)/venv"

# 安装所有依赖（含开发工具）
uv pip install -e ".[all,dev]"
```

如果需要启用**语义嵌入**（提升 Tiered Memory 召回质量），额外安装：

```bash
uv pip install -e ".[memory-embeddings]"
# 首次运行时会自动下载 BAAI/bge-small-zh-v1.5 模型（约 90MB）
```

不安装 `memory-embeddings` 时会自动降级为 Jaccard 关键词匹配，功能正常但召回质量较弱。

### 第二步：配置

```bash
# 创建 hermes 运行目录
mkdir -p ~/.hermes/{cron,sessions,logs,memories,skills}

# 复制示例配置
cp cli-config.yaml.example ~/.hermes/config.yaml

# 创建环境变量文件，填入 LLM API Key（二选一）
echo "OPENROUTER_API_KEY=你的key" >> ~/.hermes/.env
# 或者
echo "ANTHROPIC_API_KEY=你的key" >> ~/.hermes/.env
```

### 第三步：开启三层记忆（Tiered Memory）

编辑 `~/.hermes/config.yaml`，在 `memory:` 段落下添加 `tiering` 配置：

```yaml
memory:
  memory_enabled: true
  user_profile_enabled: true
  memory_char_limit: 2200
  user_char_limit: 1375

  # ── 三层记忆（本次改动新增）────────────────────
  tiering:
    enabled: true            # 总开关；false 则退回旧的平铺 MemoryStore
    prefer_local: true       # true = 优先用本地 fastembed；false = 直接用 Jaccard
    t1_char_limit: 3000      # T1 工作记忆字符上限，超出触发驱逐到 T2
    tau_days: 14.0           # 近期衰减半衰期（天）
    metrics_enabled: true    # 写指标到 ~/.hermes/logs/memory_metrics.jsonl

    retrieval:
      enabled: true          # 每轮对话前是否自动召回
      k: 5                   # 最多召回 5 条
      min_similarity: 0.5    # 相似度阈值（fastembed）；Jaccard 建议改为 0.1
      alpha: 0.7             # 语义相似度权重
      beta: 0.2              # 近期衰减权重
      gamma: 0.1             # 召回频次权重
      tau_days: 14.0
```

### 第四步：运行 Agent

```bash
# 全局软链接（可选，方便从任意目录启动）
mkdir -p ~/.local/bin
ln -sf "$(pwd)/venv/bin/hermes" ~/.local/bin/hermes

# 验证安装
hermes doctor

# 启动交互式对话
hermes
```

首次启动后，如果磁盘上存在旧的 `MEMORY.md`，会自动迁移为 `WORKING.jsonl`（T1）；新增的记忆从此都写入 `WORKING.jsonl`，旧文件保留作为备份。

### 第五步：运行测试

```bash
# 推荐方式（与 CI 一致，4 个 worker 并行）
scripts/run_tests.sh

# 只跑 Tiered Memory 相关测试
pytest tests/tools/test_memory_tiering.py -v
```

测试全部使用 `KeywordEmbedder`，无需安装 `fastembed` 也可运行。

### 验证 Tiered Memory 是否生效

启动 agent 后，告诉它记住一些事情（例如"记住我喜欢 Python"），然后在新对话中问一个相关问题。如果 `~/.hermes/memories/WORKING.jsonl` 文件存在并有内容，且 `~/.hermes/logs/memory_metrics.jsonl` 中出现了 `"event":"recall"` 的记录，说明三层记忆已正常工作。

```bash
# 查看 T1 工作记忆内容
cat ~/.hermes/memories/WORKING.jsonl | python3 -m json.tool

# 实时跟踪召回指标
tail -f ~/.hermes/logs/memory_metrics.jsonl
```
