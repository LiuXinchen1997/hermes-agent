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

### 环境要求

| 依赖 | 版本 | 说明 |
|------|------|------|
| Python | 3.11+ | 必须 |
| uv | 最新 | 推荐的包管理器，[安装文档](https://docs.astral.sh/uv/) |
| Node.js | 20+ | 可选，仅浏览器工具需要 |

> **两种部署路径**：
> - **A. 已经装过 Hermes**（pip 装的 / NousResearch 仓库 clone 的 / `setup-hermes.sh` 装的）→ 直接看下方 [已有安装如何切换到带 tiering 的版本](#已有安装如何切换到带-tiering-的版本)
> - **B. 全新机器，从零开始** → 按"第一步—第五步"走

### 第一步：克隆并安装

本次 Tiered Memory 改动**还未合并到上游 NousResearch/hermes-agent**，必须从包含改动的 fork 克隆：

```bash
git clone --recurse-submodules https://github.com/LiuXinchen1997/hermes-agent.git
cd hermes-agent

# 创建 Python 3.11 虚拟环境
uv venv venv --python 3.11
export VIRTUAL_ENV="$(pwd)/venv"

# 安装所有依赖（含开发工具）
uv pip install -e ".[all,dev]"
```

> 上游 NousResearch 仓库不含本次改动；从那里克隆得到的是不带 Tiered Memory 的原版 Hermes。后续如果上游合并了本次 PR，可以直接从 `NousResearch/hermes-agent` 克隆。

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
  user_char_limit: 1375          # T0 (USER.md) 上限；T1 启用后仍生效
  # memory_char_limit: 2200      # 旧的 MEMORY.md 上限；tiering 开启后**不再使用**

  # ── 三层记忆（本次改动新增）────────────────────
  tiering:
    enabled: true                # 总开关；false 则退回旧的平铺 MemoryStore
    prefer_local: true           # true = 优先用本地 fastembed；false = 直接用 Jaccard
    t1_char_limit: 3000          # T1 工作记忆字符上限，超出触发驱逐到 T2
    metrics_enabled: true        # 写指标到 ~/.hermes/logs/memory_metrics.jsonl
    # 下面三个是 *eviction* 打分参数（决定哪条降级到 T2）
    tau_days: 14.0               # 近期衰减半衰期（天）
    beta: 0.2                    # 近期衰减权重
    gamma: 0.1                   # 召回频次权重

    retrieval:
      enabled: true              # 每轮对话前是否自动召回
      k: 5                       # 最多召回 5 条
      min_similarity: 0.5        # 相似度阈值（fastembed）；Jaccard 建议改为 0.1
      # 下面四个是 *recall* 打分参数（决定哪条注入到 user_message）
      alpha: 0.7                 # 语义相似度权重
      beta: 0.2                  # 近期衰减权重
      gamma: 0.1                 # 召回频次权重
      tau_days: 14.0             # 近期衰减半衰期
```

> Tiering 开启后，`memory_char_limit` 字段被忽略（T1 用独立的 `t1_char_limit`）。`user_char_limit` 仍然作用于 T0（USER.md）。
>
> `tiering.{tau_days, beta, gamma}` 用于 **eviction**（决定哪条 T1 条目降级到 T2）；
> `tiering.retrieval.{tau_days, alpha, beta, gamma}` 用于 **recall**（决定每轮哪些条目注入到 user_message）。两组参数互相独立，可以分别调。

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
# 查看 T1 工作记忆内容（JSONL 一行一条，单独 pretty-print 每行）
cat ~/.hermes/memories/WORKING.jsonl | while IFS= read -r line; do
  echo "$line" | python3 -m json.tool
done
# 或者用 jq：
# jq -s . ~/.hermes/memories/WORKING.jsonl

# 实时跟踪召回指标
tail -f ~/.hermes/logs/memory_metrics.jsonl
```

---

## 已有安装如何切换到带 tiering 的版本

`~/.hermes/`（配置、memories、skills、logs、cron）**不需要动**——本次改动只换了代码层，原有数据全部保留并会自动迁移（旧 `MEMORY.md` → 新 `WORKING.jsonl`）。

根据你之前怎么装的 Hermes，选对应的路径：

### 场景 A：原来是 `pip install hermes-agent`

```bash
# 1. 卸载 PyPI 上的官方版
pip uninstall hermes-agent

# 2. 克隆 fork 并以可编辑模式重装
git clone --recurse-submodules https://github.com/LiuXinchen1997/hermes-agent.git ~/hermes-agent-tiered
cd ~/hermes-agent-tiered
uv venv venv --python 3.11
export VIRTUAL_ENV="$(pwd)/venv"
uv pip install -e ".[all,dev,memory-embeddings]"

# 3. 让 hermes 命令指向新装的
mkdir -p ~/.local/bin
ln -sf "$(pwd)/venv/bin/hermes" ~/.local/bin/hermes
```

`~/.hermes/config.yaml` 不动，下面的"开启 tiering 配置"那一步照走。

### 场景 B：已经 clone 了 NousResearch/hermes-agent 在本地

在原仓库目录里加我的 fork 作为新 remote，把 tiering 改动 merge 进来：

```bash
cd /path/to/your/hermes-agent  # 你已有的 NousResearch clone

# 加我的 fork 作为新 remote
git remote add tiered git@github.com:LiuXinchen1997/hermes-agent.git
git fetch tiered

# 切到一个本地分支再合并（避免污染你的 main）
git checkout -b try-tiering
git merge tiered/main

# 如果以前没有 editable 安装，做一下
uv pip install -e ".[all,dev,memory-embeddings]"
```

如果 merge 有冲突，多半是 `pyproject.toml` 的 `optional-dependencies` 段。挑一个保留即可。

### 场景 C：用 `setup-hermes.sh` 一键装的

`setup-hermes.sh` 默认从 NousResearch clone 到 `~/hermes-agent`。两种做法：

```bash
# 做法 1（推荐）：在原目录加 remote 合并（参照场景 B 步骤）
cd ~/hermes-agent
git remote add tiered git@github.com:LiuXinchen1997/hermes-agent.git
git fetch tiered
git checkout -b try-tiering
git merge tiered/main
# venv 已经存在不用动，但要刷新依赖：
~/.hermes/venv/bin/pip install -e ".[memory-embeddings]" --upgrade

# 做法 2：直接 reset 到 fork main（**会丢本地未提交改动**）
cd ~/hermes-agent
git remote set-url origin git@github.com:LiuXinchen1997/hermes-agent.git
git fetch origin
git reset --hard origin/main
~/.hermes/venv/bin/pip install -e ".[memory-embeddings]" --upgrade
```

### 三个场景都一样：开启 tiering 配置

打开 `~/.hermes/config.yaml`，在 `memory:` 段下追加本文档 [第三步](#第三步开启三层记忆tiered-memory) 给出的 `tiering:` 配置块（可以照搬，全部默认值就够用）。

### 验证

```bash
# 1. 检查 hermes 用的是不是 fork 版本
which hermes
python3 -c "import tools.memory_tiered_store; print('tiering available')"
# 应该输出 "tiering available"，否则说明装的还是 NousResearch 版

# 2. 启动 agent，告诉它一件事让它记住
hermes
> 记住我喜欢用 ruff 不喜欢 black

# 3. 退出后看磁盘
ls -l ~/.hermes/memories/WORKING.jsonl
# 应该存在，且内容包含你刚说的话

# 4. 看指标
tail -3 ~/.hermes/logs/memory_metrics.jsonl
# 应该看到 {"event":"add", ...} 和后续的 {"event":"recall", ...}
```

### 如何回滚到原版 Hermes

只需要把 config 里的 tiering 关掉：

```yaml
memory:
  tiering:
    enabled: false   # 关闭后所有 memory 操作回到平铺 MemoryStore
```

`WORKING.jsonl` / `COLD.jsonl` 留在盘上没人读，旧的 `MEMORY.md` 还在（迁移时不删），重新生效。

如果想彻底卸载，按你原来的安装方式反过来：场景 A 用 `pip uninstall hermes-agent` + 重新 `pip install hermes-agent`；场景 B/C 切回 `main` 分支即可（`git checkout main`，删 `try-tiering`）。
