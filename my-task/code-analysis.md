# Hermes-Agent 是怎么自进化的？

> 自进化 = "用得越久越懂你"。Hermes 把这件事拆成两条线：
> - **Skill（技能）**：怎么做某类事的步骤手册
> - **Memory（记忆）**：是什么、用户喜欢什么的事实笔记
>
> 这份文档讲清楚每条线里的模块都干啥、什么时候触发、为什么这样设计，最后讲两者怎么配合。

---

## 一、先抓总：一张图看懂

```
┌─────────────────────── 你的主会话 ────────────────────────┐
│                                                            │
│   用户消息 ──► AIAgent ──► 调工具（含 skill / memory）     │
│                  ▲      │                                  │
│                  │      │ 每 ~10 次工具调用后 + 回完话：   │
│                  │      ▼                                  │
│                  │   ┌────────────────────────────────┐    │
│                  │   │ background_review fork         │    │
│                  │   │ （路径 D 主产出方）            │    │
│                  │   │ 复盘对话 → 沉淀新 skill /      │    │
│                  │   │ patch 已有 skill / 写 memory   │    │
│                  │   └────────────────────────────────┘    │
│                  │                                         │
│                  │ system prompt 里携带：                  │
│                  │   1. 当前已加载的 skill 列表            │
│                  │   2. MEMORY.md / USER.md 的冻结快照     │
│                  │   3. 外部 provider 召回的内容（如有）   │
└──────────────────┼─────────────────────────────────────────┘
                   │
       ┌───────────┴────────────┐
       │                        │
   skill 这条线              memory 这条线
       │                        │
   ~/.hermes/skills/        ~/.hermes/memories/
   一堆 SKILL.md 文件        MEMORY.md + USER.md
       │                        │
       │ 7 天一次（独立线程）：  │ 会话开始时一次性
       ▼                        ▼
   Curator review fork       MemoryStore 加载并冻结
   合并 / 归档 / 打快照       快照 → 注入 system prompt
```

简单说：
- **前台主会话**负责"用"和"写"
- **每回合复盘 fork**（background_review）每 ~10 次工具调用后自动起一次，**沉淀经验**到 skill / memory
- **Curator**每 7 天起一次，把上面沉淀的零碎成果**合并归档**
- **记忆系统**在每次会话开头把"该带的东西"拷一份进 system prompt

---

## 二、Skill 这条线

### 2.1 Skill 长什么样？

一个 skill = 一个文件夹，里面 `SKILL.md` 是主文件，外加可选的 `references/`、`templates/`、`scripts/`、`assets/`。

```
~/.hermes/skills/
├── pr-toolkit/
│   ├── SKILL.md              ← frontmatter 里有 name/description
│   ├── references/glossary.md
│   ├── templates/release-note.md
│   └── scripts/list-prs.sh
├── .archive/                  ← 被归档的 skill 在这（可恢复）
├── .usage.json                ← 每个 skill 的使用计数
├── .curator_state             ← Curator 的调度状态
└── .curator_backups/          ← 每次整理前的快照
```

### 2.2 Skill 的四种类型（按"谁拥有"区分）

Hermes 用两份名册文件 + `.usage.json` 把 skill 分成四类：

| 类型 | 怎么落地 | 标记位置 | Curator 自动管它吗 |
|---|---|---|---|
| **Bundled**（出厂自带） | 安装 Hermes 时随包落地 | `~/.hermes/skills/.bundled_manifest` 里有它的 `name:hash` | ❌ 永不 |
| **Hub-installed**（社区安装） | 用户跑 `hermes hub install <name>` | `~/.hermes/skills/.hub/lock.json` 的 `installed` 字段 | ❌ 永不 |
| **User-created**（前台手写） | 主会话里用户让 agent 调 `skill_manage(create)` | `.usage.json` 里有记录，但 **`created_by` 为空** | ❌ 不进 Curator 名单 |
| **Agent-created**（后台沉淀） | **后台 fork** 派生的子 agent 调 `skill_manage(create)`——主要来自每回合复盘 fork（路径 D），Curator 偶尔附带（路径 E） | `.usage.json` 里 **`created_by == "agent"`** | ✅ **Curator 唯一管辖范围** |

**判定逻辑**（[skill_usage.py:159-300](tools/skill_usage.py#L159-L300)）：
- `is_agent_created(name)` 检查 "不在 bundled 名册 + 不在 hub 名册" —— bundled/hub 永远过不了这关，连 `.usage.json` 都不会被写入
- 即使过了 `is_agent_created`，还得 `.usage.json` 中 `created_by == "agent"` 才进 Curator 名单——这把 user-created 也排除掉

**为啥要分四类？**
- Bundled/Hub 是 upstream 维护的，本地擅自合并下次更新就冲突
- User-created 是用户的产物，Curator 没资格替用户决定哪个该归档
- 只有 Agent-created 是后台 fork 自己沉淀出来的产物，Curator 才能自由编排它

**前台 vs 后台的边界靠 ContextVar 实现** —— `tools/skill_provenance.py` 里的 `_write_origin`：默认 `"foreground"`，**任何 background_review fork**（每回合复盘 fork 与 Curator review fork 都算）派生子 agent 时设为 `"background_review"`。`skill_manage(create)` 只有在 `background_review` 下才会调 `mark_agent_created()` 写 `created_by="agent"`（[skill_manager_tool.py:780-782](tools/skill_manager_tool.py#L780-L782)）。

---

### 2.3 Skill 的生命周期状态

每个被 Curator 管辖的 skill（即 agent-created）有两个**正交**的状态位：

#### A. `state` —— 三选一
[skill_usage.py:53-56](tools/skill_usage.py#L53-L56)

| 状态 | 怎么进来 | 物理位置 | 还能被加载吗 |
|---|---|---|---|
| `active` | 初始默认 | `~/.hermes/skills/<name>/` | ✅ |
| `stale` | `last_used_at` 距今 > 30 天（默认）；Curator 状态机自动标 | 同上，文件不动 | ✅（只是标了，仍可用） |
| `archived` | `last_used_at` 距今 > 90 天（默认）；Curator 状态机自动归档 | 移到 `~/.hermes/skills/.archive/<name>/` | ❌ 不再出现在 skill 命令列表 |

跃迁规则（[curator.py:256-296](agent/curator.py#L256-L296)）：
- `active → stale`（仅打标）
- `stale → archived`（实际搬目录）
- `stale → active`（被人用了就自动回血）

bundled / hub / user-created 永不进入这个状态机——`_mutate()` 写 `.usage.json` 前会过 `is_agent_created` 把 bundled/hub 拦掉，而 user-created 因为 `created_by` 为空也不会出现在 `agent_created_report()` 里。

#### B. `pinned` —— 布尔标志
- 默认 `false`，用户通过 `hermes curator pin <name>` / `unpin <name>` 切换
- `pinned == true` 时：
  - Curator 状态机**跳过**它（永不被自动标 stale / 归档）
  - `skill_manage(delete)` 也会被 `_pinned_guard` 拦下（[skill_manager_tool.py:137-160](tools/skill_manager_tool.py#L137-L160)）
  - 但 **`patch` / `edit` / `write_file` 不受限**——pinned 只防"丢失"，不防"演进"

典型用法：你不想让 Curator 把某个 umbrella 误判合并掉，就 pin 它。

---

### 2.4 Skill 是怎么创建的？四种路径

#### 路径 A：随 Hermes 出厂（bundled）
**时机**：安装 / 升级时
**怎么做**：`hermes_bootstrap.py` 把内置目录拷到 `~/.hermes/skills/`，同时把每个 `<name>:<hash>` 写进 `.bundled_manifest`，下次升级按 hash 比对决定是否覆盖。

#### 路径 B：从 Skills Hub 安装
**时机**：用户主动跑 `hermes hub install <name>`
**怎么做**：`tools/skills_hub.py` 下载、跑安全扫描（`scan_skill`）、解压到 `~/.hermes/skills/`，并往 `.hub/lock.json` 的 `installed` 字典写一条。

#### 路径 C：前台主会话里 agent 创建（user-created）
**时机**：用户在主会话里说"以后这类事都按这个流程做"，或 agent 觉得"刚解决的这类问题以后还会遇到"
**怎么做**：
- agent 调 `skill_manage(action="create", name=..., content=<SKILL.md 全文>)`（[skill_manager_tool.py:373-427](tools/skill_manager_tool.py#L373-L427)）
- 文件落到 `~/.hermes/skills/<name>/SKILL.md`
- 此时 `skill_provenance` ContextVar 还是默认的 `"foreground"`
- **不调 `mark_agent_created()`**，所以这个 skill 永远不会进 Curator 名单——属于用户

#### 路径 D：每回合复盘 fork（background_review，**agent-created 的主要产出方**）
**时机**：主会话每跑完一轮，[`conversation_loop.py:4138-4162`](agent/conversation_loop.py#L4138-L4162) 在回完话后检查计数器，**累计 ≥ 10 次工具调用**（`_iters_since_skill`，默认 `skills.creation_nudge_interval = 10`）就触发；memory 侧有平行计数器 `_turns_since_memory`（默认 `memory.nudge_interval = 10`，按 user turn 算）

**怎么做**（[agent/background_review.py:46-227](agent/background_review.py#L46-L227)）：
1. 主会话回答**已经发给用户之后**，开一个 daemon 线程
2. 在 daemon 内 fork 一个 `AIAgent`，**继承父进程的 provider / 模型 / 凭据 / system prompt**（同一份 prefix cache），但工具白名单缩到 memory + skill 管理类
3. `skill_provenance` ContextVar 设为 `"background_review"`
4. 喂 `_SKILL_REVIEW_PROMPT` / `_MEMORY_REVIEW_PROMPT` / `_COMBINED_REVIEW_PROMPT`，要求 fork "回顾刚才那 10 轮对话，看看有没有值得沉淀的 skill 修订或 memory"
5. fork 决定四种动作之一（按偏好排序，能用早面的就别用后面的）：
   - **patch 当前会话已加载过的 skill**（最优——它本来就在用，最贴合）
   - **patch 一个相关的 umbrella skill**
   - **加 `references/` 或 `templates/` 或 `scripts/` 支持文件**到已有 umbrella
   - **`skill_manage(create)` 创建新的 class-level umbrella**——这一步在 background_review 下会自动调 `mark_agent_created()` 写 `created_by="agent"`
6. fork 跑完关闭，主会话毫无感知

**触发 vs 不触发的判断**：
- ✅ 当前回合产生了**非平凡技巧 / 修复 / 用户风格纠正 / 已加载 skill 的错漏**
- ❌ "环境依赖型失败"（command not found 之类）—— prompt 明确禁止沉淀，避免长期成为僵化约束
- ❌ "一次性任务叙事"（"summarize today's PR" 这种）—— 不构成"一类工作"
- ❌ "工具坏了 / 不能用 X" 的负向声明—— 几个月后早就修好了，但沉淀下来的 skill 会让 agent 长期拒绝用某个工具

如果什么都不该沉淀，fork 直接说 "Nothing to save." 然后退出。

#### 路径 E：Curator review fork 内创建 umbrella（agent-created，附带）
**时机**：7 天一次的 Curator 大整理（详见 §2.7），主要任务是把路径 D 沉淀出来的零碎合并成更大的 umbrella
**怎么做**：与路径 D 同源——也是 `skill_provenance == "background_review"` 下调 `skill_manage(create)`，差别只在 prompt（Curator prompt 更偏"伞形合并"，复盘 prompt 更偏"日常学习"）和触发频率
**和路径 D 的关系**：D 是**每天的学生**（沉淀新经验），E 是**每周的图书管理员**（把零散的学习成果归档整理）。E 几乎只在 D 已经产出一堆相关 agent-created skill 后才有事可做

---

### 2.5 Skill 上能做什么操作？

#### 工具入口
**两个**：
- `skill_view(name)` —— 加载到 prompt，**触发 `bump_view`**
- `skill_manage(action, ...)` —— 六个 action：`create / edit / patch / delete / write_file / remove_file`

#### 操作对每种类型的可用性

| 操作 | bundled / hub | user-created | agent-created（unpinned） | agent-created（pinned） |
|---|---|---|---|---|
| `skill_view` 加载到 prompt | ✅ | ✅ | ✅（除非 archived） | ✅ |
| `skill_manage(patch)` 局部改 | ✅ | ✅ | ✅ | ✅ |
| `skill_manage(edit)` 整体覆盖 | ✅ | ✅ | ✅ | ✅ |
| `skill_manage(write_file)` 加附件 | ✅ | ✅ | ✅ | ✅ |
| `skill_manage(remove_file)` 删附件 | ✅ | ✅ | ✅ | ✅ |
| `skill_manage(delete)` 删整体 | ✅ | ✅ | ✅ | ❌ pinned 拦截 |
| 自动 stale / archived 跃迁 | ❌ | ❌ | ✅ | ❌ 跳过 |
| 被 Curator 合并进 umbrella | ❌ | ❌ | ✅ | ❌ 跳过 |
| `hermes curator pin/unpin` | 无意义 | 无意义 | ✅ | ✅ |

**几个隐藏约束**：
- `create` 要求名字全局唯一（`_find_skill` 全目录扫一遍），重名直接拒
- `patch` 走 `fuzzy_match.fuzzy_find_and_replace`，对缩进 / 大小写有容错；但要求匹配数符合预期（默认必须唯一，否则报错）
- 所有 `edit`/`patch`/`write_file` 都是"先写新文件、扫描、失败回滚"的事务式（[skill_manager_tool.py:447-554](tools/skill_manager_tool.py#L447-L554)）
- `delete` 强烈建议带 `absorbed_into=<umbrella>` 或 `absorbed_into=""`，否则 Curator 没法分清"合并 vs 真删"

#### 每次成功操作后的副作用

[skill_manager_tool.py:765-788](tools/skill_manager_tool.py#L765-L788) — 任意 `skill_manage` 成功返回后做三件事：
1. **清缓存**：`clear_skills_system_prompt_cache(clear_snapshot=True)`——下一轮重新装 skill 列表
2. **更新计数**：
   - `create`（仅 background_review 下）→ `mark_agent_created()`
   - `patch / edit / write_file / remove_file` → `bump_patch()`
   - `delete` → `forget()` 把 `.usage.json` 中对应条目删掉
3. **错误降级**：telemetry 失败永远不会让工具调用失败，最多 debug 日志

---

### 2.6 Skill 是怎么被使用的？

#### 加载到 prompt 的三条路径

1. **用户输入 `/<skill-name>`**
   - `agent/skill_commands.py` 启动时扫描所有 `SKILL.md` 的 frontmatter，建一张 `/<cmd> → skill_dir` 的映射
   - 用户按 slash 命中 → 自动调 `skill_view` 把 SKILL.md 内容塞进新一轮的 user message

2. **agent 自主判断**
   - agent 在 reasoning 时发现"这个 skill 相关" → 主动调 `skill_view(name=...)`
   - 返回的内容直接拼进对话

3. **Cron 定时任务挂载**
   - `~/.hermes/cron/jobs.json` 里某个 job 配了 `skill: "xxx"` 或 `skills: ["a", "b"]`
   - 定时起的 agent 会把对应 skill 装进 system prompt（这也是为啥 Curator 改 skill 名时必须连带改 cron 引用）

#### 加载的副作用

`skill_view` 成功一律调 `bump_view(name)`：
- `view_count += 1`
- `last_viewed_at = now`

注意 **view 和 use 是两个不同计数器**：
- `view_count` —— "打开看了"就算（`skill_view` 触发）
- `use_count` —— 实际被纳入 prompt 路径或被引用时算（`bump_use`，[skill_usage.py:416-422](tools/skill_usage.py#L416-L422)）

Curator 后面判定"还有人用吗"看的是 `latest_activity_at`，取 `last_used_at / last_viewed_at / last_patched_at` 三者最大值（[skill_usage.py:124-141](tools/skill_usage.py#L124-L141)）。

#### 什么情况下 skill 不会被加载？

- `state == "archived"` —— 目录已经移到 `.archive/`，slash 命令表里直接看不到
- 平台不匹配 —— frontmatter 里写了 `platform: telegram` 但 agent 跑在 cli 平台（[skill_commands.py:286-293](agent/skill_commands.py#L286-L293)）
- 用户配置禁用 —— `skills.platform_disabled` 或 `skills.disabled` 列表里出现
- 同名冲突 —— 多个 skill 同名时按"local skills 目录优先 → 外部 skills 目录"顺序，先到的胜出

---

### 2.7 Curator 是谁？干啥的？

> **重要前置**：Curator **不是**日常学习的主力。Hermes 有两套后台 fork——
>
> | | 每回合复盘 fork（路径 D） | Curator review fork（路径 E） |
> |---|---|---|
> | 频率 | 每 ~10 次工具调用 | 7 天一次 |
> | 主要任务 | 沉淀新 skill / 更新 memory | 合并冗余 skill / 归档过期 skill |
> | 文件 | `agent/background_review.py` | `agent/curator.py` |
> | 产生新 skill 吗 | ✅ 这是 agent-created 的主要来源 | 偶尔（合并时新建 umbrella） |
>
> Curator 是 **"图书管理员"**——它整理路径 D 沉淀下来的成果，本身不太负责"学新东西"。

Curator 每隔一段时间（默认 7 天）起一次，做两件事：

#### 阶段一：纯函数状态机（不调 LLM，几秒搞定）
[curator.py:256-296](agent/curator.py#L256-L296) — 走遍所有"agent-created"的 skill，按时间窗调状态：
- 最近一次用过它的时间 > 30 天 → `active → stale`
- 最近一次用过它的时间 > 90 天 → 真的把目录移到 `.archive/`
- 标了 stale 但又有人用了 → 自动回血到 active
- `pinned == true` 的全部跳过

#### 阶段二：派生一个子 AIAgent 让 LLM 做"伞形合并"
[curator.py:1622-1756](agent/curator.py#L1622-L1756) — 这是 Curator 真正"长智商"的地方：
1. 用辅助模型（可在 `auxiliary.curator.{provider,model}` 配）派生一个 `AIAgent`，关掉所有递归提示、关掉 memory、关掉 context 文件，平台标记为 `"curator"`
2. 喂一个长长的 [review prompt](agent/curator.py#L330-L444)，要求 LLM **把零碎的窄 skill 合并成"伞形 umbrella"**——例如把 `pr-summary` / `pr-digest` / `list-pr-diff` 合并成一个 `pr-toolkit`
3. 严格约束：
   - **禁止 delete**，最大破坏性动作是 `archive`（可恢复）
   - **禁止动 bundled / hub / pinned**
   - 删除时必须显式声明 `absorbed_into=<umbrella>` 或 `absorbed_into=""`
   - 最后必须输出固定格式的 YAML 块标明 `consolidations:` 和 `prunings:`
4. 跑完后从子 agent 的 `_session_messages` 抽出所有工具调用，写进报告

#### 阶段三：写报告
[curator.py:970-1342](agent/curator.py#L970-L1342) — 在 `~/.hermes/logs/curator/<时间戳>/` 下生成 `run.json` + `REPORT.md`。其中"哪些 skill 是真的归档了 vs 哪些被合并进 umbrella"是用 **三路汇总** 判断的（按优先级）：

| 优先级 | 信号来源 |
|---|---|
| 高 | LLM 在 `skill_manage(delete)` 时显式声明的 `absorbed_into` |
| 中 | LLM 最终回答里 YAML 块声明的 `consolidations`/`prunings` |
| 低 | 启发式：扫所有工具调用 args，看被删 skill 名是否出现在仍存活 skill 的内容里 |

三个信号互相校验：LLM 声明合并到一个并不存在的 umbrella 就会被打回幻觉，回退到启发式或视作 pruned。

### 2.8 什么时候启动 Curator？

[curator.py:199-249](agent/curator.py#L199-L249) — `should_run_now()` 串五道门：
1. 配置 `curator.enabled` 是不是 true（默认是）
2. 用户没手动 pause
3. `.curator_state` 里 `last_run_at` 距今 ≥ `interval_hours`（默认 168 小时 / 7 天）
4. 上层 gateway 还会再叠加一个 `min_idle_hours`（默认 2 小时）确保用户真的在 idle
5. **首次安装时不立即跑**——把 `last_run_at` 种子设为"现在"，等下个 7 天才真正动手

用户也可以手动绕过门槛：`hermes curator run` 立即跑，`hermes curator run --dry-run` 看会做啥但不真动。

### 2.9 出错了能恢复吗？

能。每次 Curator 真要开干前都会先打个 tar.gz 快照（[curator_backup.py:212-282](agent/curator_backup.py#L212-L282)）：
- 整个 `~/.hermes/skills/` 流式压成 `skills.tar.gz`（排除 `.curator_backups/` 防自递归、排除 `.hub/`）
- **同时** 把 `~/.hermes/cron/jobs.json` 也复制进去——因为 cron 通过 skill 名引用 skill，Curator 合并 skill 会改名，回滚必须连带改回去
- 默认保留最近 5 份，更老的自动删

回滚命令：`hermes curator restore [<backup-id>]`。会先把当前 skills 目录搬到一个临时 staging（这样回滚自身也能再回滚），再把快照里的内容铺回去，最后只改 cron 的 `skills`/`skill` 字段、不动 schedule 和 enabled。

---

## 三、Memory 这条线

### 3.1 Memory 是什么？两份文件而已

- `~/.hermes/memories/MEMORY.md` — agent 自己写的笔记（环境事实、项目约定、踩过的坑）
- `~/.hermes/memories/USER.md` — 关于用户的画像（偏好、沟通风格、忌讳）

两份都是纯文本，多条记录之间用 `\n§\n` 分隔。**有字符上限**：MEMORY 默认 2200 字符，USER 默认 1375 字符——故意不算 token 数，因为字符数与模型无关。

### 3.2 谁来写？什么时候写？

写记忆走 `memory` 工具（[memory_tool.py:465](tools/memory_tool.py#L465)），三个 action：`add` / `replace` / `remove`。**agent 主动调，不是用户点按钮调**。

工具描述里写明了触发时机（[memory_tool.py:518-538](tools/memory_tool.py#L518-L538)）：
- 用户纠正你 / 说"以后别这样"
- 用户透露偏好（角色、时区、代码风格）
- 你发现了环境事实（OS、装了哪些工具、项目结构）
- 你学到了一个用户特有的约定或 API 怪癖

**不该存**：任务进度、会话结果、完成日志、临时 TODO。这些进 session_search 找历史就行。

### 3.3 冻结态 vs 实时态——记忆系统的设计精髓

这是整个记忆系统最巧妙的地方。Hermes 维护 **两份并行的记忆状态**（[memory_tool.py:107-142](tools/memory_tool.py#L107-L142)）：

```
   ─────────────────────────── 会话开始 ──────────────────────────
                              │
                              ▼
            load_from_disk()
            ─────────────────────────────────────────────────
            1. 读 ~/.hermes/memories/MEMORY.md → memory_entries 列表
            2. 读 ~/.hermes/memories/USER.md   → user_entries 列表
            3. **立即拍照** → _system_prompt_snapshot 字典
                          ↘                ↘
                          实时态          冻结态
                          ↓               ↓
                    内存 + 磁盘         仅内存
                    可读可写           只读，永不动
                          │               │
   ─────────── 整个会话过程中 ──────────────────
                          │               │
   每次 LLM 调用前         │               │
   组装 system prompt 时 ──┼───────────────┘  ◄── 走这条
                          │
   每次 memory(add/...)：  │
   - 进文件锁              │
   - reload                ├── 只动这条
   - 改列表 + 落盘         │
   - tool response 用      │
     实时态返回给 agent    │
                          │
   ─────────────────────────── 会话结束 ───────────────────────
   两份状态都销毁；下次会话重新走 load_from_disk()
```

#### 冻结态（`_system_prompt_snapshot`）
- **存储**：进程内存里一个字典 `{"memory": "<渲染好的整块 string>", "user": "<同>"}`，**不落盘**
- **形态**：已经带表头和字符数指示的整段渲染结果，例如：
  ```
  ══════════════════════════════════════════════
  MEMORY (your personal notes) [37% — 815/2,200 chars]
  ══════════════════════════════════════════════
  用户用 pnpm
  §
  项目 Python 3.11
  ```
- **生命周期**：`load_from_disk()` 拍一次照 → **整个会话纹丝不动** → 会话结束随 store 销毁
- **唯一用法**：`format_for_system_prompt(target)` 返回它，被 system prompt 组装器在每次 LLM API 调用前拼进 system prompt 固定位置（[memory_tool.py:361-372](tools/memory_tool.py#L361-L372)）

#### 实时态（`memory_entries` / `user_entries`）
- **存储**：双份
  - 进程内存：`MemoryStore` 上两个 `List[str]`
  - 磁盘：`~/.hermes/memories/MEMORY.md` 和 `USER.md`，多条之间用 `\n§\n` 分隔
  - 两份必须等价——每次写操作都立即原子落盘，崩溃也不丢
- **生命周期**：`load_from_disk()` 时初始化 → 每次 `memory` 工具调用就改一次 → 会话结束随 store 销毁
- **两个调用点**：
  1. **agent 看自己刚写的**——工具响应里返回的 `entries` 字段就是实时态，agent **本轮**就能看到新加的条目
  2. **下一次 `_reload_target`**——再调一次 `memory` 工具时，进文件锁后会重新读盘把最新实时态拿回来（防其它进程并发写）

#### 写入流程（`add` 为例，[memory_tool.py:224-267](tools/memory_tool.py#L224-L267)）

```python
def add(self, target, content):
    # 1. 内容预检：去空白、安全扫描（注入/凭据外泄正则）
    if _scan_memory_content(content): return error

    # 2. 进文件锁（fcntl LOCK_EX，跨进程互斥）
    with self._file_lock(self._path_for(target)):
        # 3. 重读磁盘，把可能的外部修改吸收进实时态
        self._reload_target(target)
        # 4. 显式去重 / 5. 字符上限 / 6. append 到实时态 / 7. 原子落盘
        entries.append(content)
        self.save_to_disk(target)   # tempfile + os.replace

    # 8. 把实时态包进 tool response 返还
    return self._success_response(target, "Entry added.")
```

注意整个流程**完全不动冻结态**。

#### 对照表

| | 冻结态 | 实时态 |
|---|---|---|
| 形态 | 渲染好的整块 string（带表头） | 字符串列表 |
| 在内存 | ✅ | ✅ |
| 在磁盘 | ❌ | ✅ MEMORY.md / USER.md |
| 中途会变吗 | ❌ 整个会话**绝对不变** | ✅ 每次 `memory` 调用都改 |
| 谁来读 | system prompt 组装器（每轮 LLM 前） | tool 响应给 agent + 下次 reload 用 |
| 设计目的 | **保 prompt prefix cache** | 即时反馈 + 持久化 |

#### 为什么要分两份？时间线说话

```
T0  会话启动 load_from_disk()
    磁盘 MEMORY.md: ["用 pnpm", "Python 3.11"]
    实时态 = 冻结态 = ["用 pnpm", "Python 3.11"]

T1  第 1 轮 LLM 调用
    system prompt 塞冻结态 → LLM 看到 2 条

T2  agent 学到新事实：memory(add, "用户在 Termux 上跑")
    实时态 = ["用 pnpm", "Python 3.11", "用户在 Termux 上跑"]
    MEMORY.md 落盘也是这 3 条
    冻结态：**还是** ["用 pnpm", "Python 3.11"]
    tool response 返回的 entries：3 条 ◄── agent 在本轮能看到刚写的

T3  第 2 轮 LLM 调用
    system prompt 还用冻结态 → 仍然只看到 2 条
    ✅ Anthropic / OpenAI prompt cache 命中
    （agent 如想用第 3 条，得在自己 reasoning 时回忆之前的 tool response）

T4  会话结束 → 内存里两份状态都销毁，磁盘上 3 条保留

T5  第二天新会话 load_from_disk()
    实时态 = 冻结态 = 3 条 ← 这次 system prompt 才包含
```

**Trade-off**：
- ✅ Anthropic prompt caching / OpenAI implicit cache 全程命中，cost 大约只有正常的 10%
- ❌ 本轮写的记忆，本轮 system prompt 看不到（agent 得自己"记得"之前 tool response 里返回过）

这个 trade-off Hermes 选了前者——因为记忆本来就是**"跨会话才有意义"**的东西，本轮立刻进 system prompt 也没多大用，但 cache miss 是每轮真金白银的开销。

### 3.4 写入怎么保证安全？

[memory_tool.py:224-267](tools/memory_tool.py#L224-L267) — `add()` 完整流程：

1. `_scan_memory_content()` 扫内容（[memory_tool.py:67-104](tools/memory_tool.py#L67-L104)）
   - 14 条威胁正则：`ignore previous instructions` / `you are now ...` / `curl ... $TOKEN` / `cat .env` / SSH 后门……
   - 10 个不可见 Unicode 字符（用于隐藏注入）
   - 匹配到任何一个 → 直接拒绝，不写
2. 进入 `fcntl.LOCK_EX` 文件锁（跨进程互斥）
3. 重新从盘上读一次（防其它进程并发写造成脏读）
4. `if content in entries: return "已存在"` —— 显式去重
5. 字符上限校验
6. 写到 tempfile → `os.replace()` 原子替换

### 3.5 多个 provider 是怎么回事？

除了本地 `MEMORY.md`/`USER.md`，Hermes 还支持外部记忆服务（mem0、honcho、supermemory、hindsight 等）作为 **provider**。

[agent/memory_manager.py:260-302](agent/memory_manager.py#L260-L302) — `MemoryManager` 是聚合层，**有个硬规则：外部 provider 最多注册一个**。注册第二个直接 warning 拒绝。理由是：tool schema 会膨胀、记忆后端会互相打架。

`MemoryManager` 暴露四个聚合接口：

| 方法 | 时机 | 在做什么 |
|---|---|---|
| `build_system_prompt()` | 系统 prompt 组装时 | 把每个 provider 的静态说明拼起来 |
| `prefetch_all(query)` | 每次 LLM 调用**之前** | 同步去问 provider"和这条用户消息相关的过往记忆是啥"，结果包在 `<memory-context>` 围栏里塞进 user message |
| `queue_prefetch_all(query)` | 每次回合**结束后** | 提前异步预热下一轮 |
| `sync_all(user, asst)` | 每次回合**结束后** | 把这一轮原文同步给 provider 入库 |

### 3.6 流式输出会泄漏围栏吗？

`prefetch_all()` 把召回内容包成 `<memory-context>...</memory-context>` 注入。模型生成回复时偶尔会把围栏标签也吐出来。如果直接给用户看到原始流，用户就会看到 `<memory-context>...` 这种内部脚手架。

[agent/memory_manager.py:62-200](agent/memory_manager.py#L62-L200) — `StreamingContextScrubber` 是个状态机，**一边接 chunk 一边剥**：
- 在围栏外 → 找 `<memory-context>` 开头；遇到则吞掉，进入 span
- 在围栏内 → 累积到 `</memory-context>`，跳出 span
- chunk 末尾可能是不完整的 tag → 暂存等下个 chunk
- 流末尾若 span 未关 → 缓冲直接丢弃（"宁可截答，不要泄漏内部上下文"）

---

## 四、Skill 和 Memory 怎么配合？

两条线在很多层面是隔离的，又在某些点必然相遇。

### 4.1 分工：程序性 vs 陈述性

| | Skill | Memory |
|---|---|---|
| 内容形态 | 完整的步骤手册（SKILL.md + 支持文件） | 一两句陈述事实 |
| 触发方式 | 用户 `/<name>` 或 agent 主动 `skill_view` | 自动注入 system prompt |
| 何时写 | "我学会怎么做某类事了" | "我知道了一个事实" |
| 何时整理 | Curator 后台 7 天一次 | 用户 / agent 主动 `replace`/`remove` |
| 谁来"自动管理" | Curator 子 agent | 没有专门的整理器 |

### 4.2 Curator review fork 故意不读 memory

[curator.py:1691-1707](agent/curator.py#L1691-L1707) — Curator 派生的子 AIAgent 设了 `skip_memory=True`。意味着：
- 它不会读用户的 MEMORY.md / USER.md
- 它不会触发 provider 的 prefetch / sync
- 它的行为 100% 由 review prompt + 当前 skill 列表决定

**为啥这样？** Curator 是后台批处理，不参与对话，没必要污染用户的记忆缓存；同时也避免记忆里的"风格偏好"误导 Curator 的合并决策。

### 4.3 system prompt 缓存策略相反

两条线在"如何与 prompt cache 共处"上选了相反的方向：

| | skill 改了之后 | memory 改了之后 |
|---|---|---|
| 怎么办 | 主动清缓存（`clear_skills_system_prompt_cache(clear_snapshot=True)`） | 故意不动缓存（继续用冻结快照） |
| 为啥 | skill 改动多是结构性的，下一轮就要生效 | memory 改动多，本轮就生效会破坏缓存 |

### 4.4 cron 引用串起两边

cron 任务可以指定运行时挂载某个 skill（参数 `skill` 或 `skills`）。Curator 合并 skill 会改名，cron 就会指向不存在的 skill。所以：
1. Curator 备份时把 `~/.hermes/cron/jobs.json` 也存进快照
2. 合并完成后通过 `cron.jobs.rewrite_skill_refs()` 把 cron 里旧名字改成 umbrella 名字
3. 回滚时只改 cron 的 skill 字段、不动 schedule/enabled/prompt 等用户活态字段

---

## 五、走一遍完整时间线

假设你今天开始用 Hermes：

```
Day 0   你在主会话说"我用 pnpm 不 npm"
        → agent 调 memory(action="add", target="user", content="用户用 pnpm 不用 npm")
        → 写进 ~/.hermes/memories/USER.md，本轮 system prompt 还是旧的

Day 0   关掉会话，明天再开 → 新 MemoryStore 加载、冻结新快照
        → system prompt 里就带上"用户用 pnpm"了

Day 1   你说"以后帮我生成周报都按下面格式"
        → agent 在主会话里调 skill_manage(create, name="weekly-report")
        → SKILL.md 写到 ~/.hermes/skills/weekly-report/
        → 因为是前台创建，.usage.json 里不写 created_by="agent"
        → Curator 看不到、不会动它

Day 2   你说"/weekly-report" → skill_view 加载 → bump_view → use_count++
       （但因为不是 agent-created，Curator 仍然不管）

Day 7   你又陆续让 agent 创建了几个 PR 相关的小 skill
        但这些也是前台创建的，Curator 一律不动

Day 365 假设某天 agent 在主会话里"自我反思"派生了一个 background_review
        发现自己手上有 6 个 PR 相关的零碎逻辑都是 agent 自己用过的
        → background_review 创建 umbrella "pr-toolkit"
        → 这个 umbrella 因为是 background_review 创建，被标 created_by="agent"
        → 进入 Curator 管辖

Day 372 距上次 last_run_at 满 7 天，should_run_now 返回 true
        Curator 触发：
        1. 打 tar.gz 快照
        2. 跑纯函数状态机：active/stale/archived 状态切换
        3. 派生子 AIAgent，看到 pr-toolkit 和 6 个旧 skill
           → 把 6 个零碎归档进 .archive/，标 absorbed_into="pr-toolkit"
        4. 写 logs/curator/<时间戳>/REPORT.md

Day 380 万一发现合并错了 → hermes curator restore
        → 从 tar.gz 还原 skills/ 目录 + 修复 cron 引用
```

---

## 六、记几个关键文件就能定位 80% 的逻辑

| 文件 | 是什么 |
|---|---|
| [agent/curator.py](agent/curator.py) | Curator 的调度、状态机、LLM review 编排 |
| [agent/curator_backup.py](agent/curator_backup.py) | tar.gz 快照与回滚 |
| [tools/skill_manager_tool.py](tools/skill_manager_tool.py) | `skill_manage` 工具：6 个 action 的具体实现 |
| [tools/skill_usage.py](tools/skill_usage.py) | `.usage.json` 读写、计数器、归档/恢复 |
| [tools/skill_provenance.py](tools/skill_provenance.py) | "谁创建的"ContextVar，前后台边界 |
| [tools/memory_tool.py](tools/memory_tool.py) | `MemoryStore`：双状态、文件锁、内容扫描 |
| [agent/memory_manager.py](agent/memory_manager.py) | 多 provider 聚合 + 流式输出剥离器 |
| [agent/memory_provider.py](agent/memory_provider.py) | `MemoryProvider` ABC，外部记忆插件接口 |

需要继续看可改进点：[self-evolution-analysis.md](self-evolution-analysis.md)。
