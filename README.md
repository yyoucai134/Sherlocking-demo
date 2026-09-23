<div align="center">

### 具备长期记忆的深度研究智能体内核

**📚 文档：** [English](USAGE.md) · [简体中文](resource/USAGE.zh-CN.md) · [日本語](resource/USAGE.ja.md)

<sub>ReAct 循环 · 上下文治理 · 五层记忆 · 多智能体编排 · 技能自进化 · 沙箱隔离</sub>

</div>

---

## 概述

Sherlocking 是一个深度研究智能体内核，专为那些关心智能体**如何**被架构的人而设计。它不追求功能清单的堆砌，而是建立一个清晰、解耦、可评估的基础——从 ReAct 核心循环到上下文工程治理，从五层长期记忆系统到共享沙箱隔离的多智能体编排，从沙箱路径强制执行到三层技能自进化系统。

每个模块都经过独立设计、独立测试、可独立验证。**2400+ 个测试**守护着每一层。

---

## 核心模块

### 🧠 ReAct 研究内核

单个 `LeaderAgent` 编排研究循环。LangGraph 负责外层流程编排（`prepare → leader_agent → finalize`），而 **21 个中间件**横切每一个生命周期钩子：`before/after_agent`、`before/after_model`、`wrap_tool_call`。

**突破点：** 中间件是一等公民——记忆召回、技能注入、沙箱生命周期、记忆固化、工具调用配对、求助请求和上下文治理都是可插拔的横切关注点，而非嵌入在智能体循环中。`app → agents` 的依赖严格保持单向。

### 📐 上下文工程治理

`DefaultStrategy` 根据实时 token 预算动态外置历史消息。窗口大小通过**穿透 `FallbackChatModel`** 解析到当前生效供应商的真实上下文窗口——不使用硬编码阈值。双策略——压缩（摘要）与外置（卸载）——在长时间研究会话中防止上下文溢出，同时不丢失关键信息。

**突破点：** 治理层将 token 预算视为一等运行时关注点。分数分母是*真实*的模型窗口（在调用时解析），而非静态配置——这使得 P5 熔断阈值在供应商切换时依然准确。

### 🧬 五层长期记忆

Sherlocking 实现了一套受认知科学启发的五层记忆系统，每一层都可独立测试：

| 层级 | 角色 | 关键突破 |
|-------|------|-----------------|
| **L1** | Schema + 协议 | `MemoryTrace` 冻结数据类（15 个字段）+ `MemoryType` 枚举（episodic/semantic/procedural）+ 5 个原子操作（Encode/Retrieve/Associate/Consolidate/Reconsolidate）——**工具不含 LLM**，纯数据操作 |
| **L2** | 默认策略 | 艾宾浩斯衰减公式（`strength = base×(1-decay)^hours + log(1+access)×0.1 + importance×0.05`）+ 复合遗忘（TTL + 强度阈值）+ 6 条硬编码决策（A1-F2）——**惰性衰减**，强度在检索时计算，无后台任务 |
| **L3** | 存储 + 检索器 | `MarkdownFileStore`（单一 `traces.md` 真相源 + `<!-- trace: {id} -->` 分隔符 + YAML frontmatter）+ `HybridRetriever`（纯 BM25，无向量/图依赖）——**检索强化回写**（1A：命中后 store.update 强化 strength）+ **遗忘过滤**（3B：metadata.forgotten=True 被排除）+ **增量索引**（5B：store 装饰器触发 retriever.on_trace_*） |
| **L4** | 中间件 + 引导 | `MemoryMiddleware.abefore_model`——每次调用注入 `HumanMessage`（保护 prompt 缓存，`hide_from_ui=True`）+ `set_turn_id` ContextVar（可追溯性 C：actor = turn:N）+ 引导生命周期（懒加载双重检查锁 + `set_memory_config` 全局单例同步） |
| **L5** | 自动固化 | `MemoryConsolidationMiddleware.aafter_model`——每 N 轮**非阻塞**提交 + `MemoryWorker`（守护线程 + `threading.Queue` + LLM 构造注入）——LLM 抽取情景记忆 → `manager.encode` → 候选 ≥ N → LLM 生成合并内容 → `manager.consolidate`（max=10，E1）——**出错：记录日志并跳过**，绝不阻塞主循环 |

**关键设计：** 记忆注入采用每次调用的 `HumanMessage`（而非系统提示词），以保护 LLM 的 prompt 缓存前缀。state 中的 `recalled_memories` 仅存储索引（id+score+strength），不存储完整内容。`MemoryConfig` 有 4 个仅启动期字段（use/storage_path/vector_store/graph_store）——其余可通过 `set_memory_config()` 在运行时替换。

### 🤝 多智能体编排

Sherlocking 支持将子任务委派给外部编码智能体和内部的自我副本：

- **专家委派**——`delegate_to_specialist(goal, success_criteria)` 通过 MCP `SpecialistMcpServer`（暴露 8 个沙箱工具）路由到外部 CLI（pi / codex / claude）。每个专家作为独立进程运行，拥有自己的 LLM，但通过 `--sandbox-url` 透传共享**同一个 Docker 沙箱**。
- **子智能体（自我副本）**——`delegate_to_subagent(goal)` 创建一个上下文隔离（不继承消息历史）但共享线程沙箱的 Sherlocking 自我副本。`SandboxMiddleware.abefore_model` 从 `state["sandbox"]` 恢复 `ContextVar`——子智能体复用父级的 `sandbox_id`，无需重新获取。
- **L2 进化层**——数据驱动的专家自进化：当 `effective_rate < threshold` 时 `MetricMonitor` 触发，`IVEFocuser` 诊断，`LLMMutator` 变异，`ScoreDeltaGate` 把关，性能下降时 `GitRatchet` 回滚。
- **L3 评估层**——三层评估：执行判定（每个技能每个任务由 LLM 判定）、任务质量评分（四维加权）、响应契约检查。`RuntimeTracker` 将退化信号反馈给 L2。

**突破点：** 「共享线程沙箱」（INV#3）如今**真正落地**——子智能体从 state 恢复 ContextVar，专家连接到同一挂载区写入，而非临时容器内部路径。

### 🛡️ 带路径强制的沙箱隔离

两种提供者：**Local**（宿主进程，用于开发）和 **Docker**（容器隔离，用于生产）。

**Docker 模式突破点：**
- **`DockerPathTranslator`**——`translate_path` 直接透传（容器路径 = 绑定挂载路径），`reverse_translate` 将 `/mnt/sherlocking/user-data/<x>` 映射为 `<sandbox_root>/<sandbox_id>/<x>`（Windows 宿主路径）——修复 `present_files` 产物提取链路（`shutil.copy2` 现在拿到真实的 Windows 路径，而非容器路径）
- **`DockerPathGuard`**——写路径白名单：`write_file`/`str_replace` 路径必须位于 `/mnt/sherlocking/user-data/` 下，bash 重定向目标（`>{1,2}\s*(/[^\s;|&]*)`）必须在挂载区内——**强制智能体写入持久化**，避免在 `--rm` 下丢失于容器内部 `/tmp`
- **预热池**——预先创建的容器降低冷启动延迟
- **空闲自动销毁**——`SHERLOCKING_SANDBOX_IDLE_TIMEOUT=600`（10 分钟）
- **跨进程锁**——并发的 Sherlocking 实例不会冲突（三函数锁：open/lock/unlock）
- **WSL2 执行器**——`WslDockerExecutor` 为 WSL2 中的 Docker 守护进程将 `D:\foo\bar` 转换为 `/mnt/d/foo/bar`

### 🔌 MCP 工具体系

三种传输方式：`stdio`、`sse`、`http`。核心工具在启动时加载；非核心工具按需延迟加载。工具等价回退链（例如 `web_search` → MCP server → 内置 ddg）确保鲁棒性。工具元数据驱动外置阈值。通过 `.sherlocking/mcp_servers.yaml` 配置。

### 🎯 三层技能架构

技能是**研究过程知识包**——提示词级别的注入，而非可执行函数。「如何验证一个来源」是技能。「执行一次网络搜索」是工具。

- **第 1 层（基础）：** 基于 SQLite 的存储，带版本 DAG、质量过滤的 LLM 混合选择、注入中间件，以及四个计数器指标（selections / applied / completions / fallbacks）。
- **第 2 层（进化）：** `IVEFocuser` 诊断、`LLMMutator` 变异、`ScoreDeltaGate` 把关、`GitRatchet` 棘轮回滚。当有效命中率低于阈值时，技能自动进化。
- **第 3 层（评估）：** 三层评估——执行判定、任务质量评分（四维加权）、响应契约检查。`RuntimeTracker` 监控应用率趋势并将退化信号反馈给第 2 层。

36 个内置技能，涵盖 5 个类别（core / research / software-development / creative / productivity）。核心技能自动加载；其他通过 `/skill search` 发现。

### 🎨 三重 UI

- **TUI**（默认）：全屏 Textual 应用，包含欢迎视图 + 对话视图。左侧可滚动日志、底部输入框、带实时 token 用量的状态栏。宽屏时显示右侧会话信息面板。
- **CLI**（`sherlocking cli`）：传统滚动模式，使用 `prompt_toolkit` + `rich`。支持斜杠命令补全 + 底部工具栏。
- **Web**（`sherlocking web`）：基于浏览器的界面，通过 WebSocket 实时流式传输。与 TUI 一致的暗色主题，欢迎页 + 对话视图 + 可折叠侧边栏（上下文详情、token 用量、沙箱、MCP 服务器）。支持命令面板（Ctrl+P）和设置面板。可部署到任意服务器。

### 🔄 多 LLM 回退

`FallbackChatModel` 构建基于角色的路由链（researcher / reporter）。当出现瞬时 API 故障（限流、超时、5xx）时，自动降级到下一个供应商。DeepSeek 始终位于链尾，作为最终兜底。

### 📊 可观测性

`RunJournal` 记录结构化事件（`skill.select`、`skill.apply`、`memory.encode`、`memory.consolidate`、`compaction`、`budget`）。线程目录持久化运行产物。`/expand` 命令展开上一轮的完整 Thought 文本与工具结果。

---

## 架构

<sub>外层流程：`prepare → before_agent → LeaderAgent（ReAct 循环）→ after_agent → finalize`。21 个中间件横切每一个钩子。记忆召回（L4）发生在 `before_model`，记忆固化（L5）发生在 `after_model`。技能注入（L1+L2+L3）在 `before_model`。工具调用通过 `wrap_tool_call` 路由到 Sandbox / MCP / Builtin。多智能体委派通过 `delegate_to_specialist` / `delegate_to_subagent`。</sub>

---

## 快速开始

```bash
# 1. 克隆仓库
git clone <repo-url> && cd Sherlocking

# 2. 创建环境（Python 3.12+）
python -m venv .venv
.venv\Scripts\activate         # Windows
# source .venv/bin/activate    # Linux / macOS

# 3. 安装
pip install -e ".[dev]"

# 4. 配置
cp .env.example .env
# 编辑 .env —— 至少填写：DEEPSEEK_API_KEY=sk-xxx

# 5. 启动（默认 TUI）
sherlocking
```

<sub>输入一个问题即可开始研究。输入 `/` 进行命令补全，输入 `/help` 查看所有命令。</sub>

### 启动 Web UI

```bash
# 启动 Web 服务器（默认 http://127.0.0.1:8080）
sherlocking web

# 部署到局域网 / 服务器（可从其他设备访问）
sherlocking web --host 0.0.0.0 --port 9000

# 开发模式，带自动重载
sherlocking web --reload
```

<sub>在浏览器中打开 `http://localhost:8080`。Web UI 支持实时流式传输、侧边栏信息面板、命令面板（Ctrl+P）和设置面板（Ctrl+B）。</sub>

### 启用高级功能

```env
# 长期记忆（L4 召回 + L5 自动固化）
SHERLOCKING_MEMORY_USE=default
SHERLOCKING_MEMORY_PHASE2_ENABLED=true
SHERLOCKING_MEMORY_PHASE2_TURNS=10

# 技能系统（L1 基础 + L2 进化 + L3 评估）
SHERLOCKING_SKILL_ENABLED=true
SHERLOCKING_SKILL_EVOLVE_ENABLED=true
SHERLOCKING_SKILL_EVAL_ENABLED=true
SHERLOCKING_SKILL_MAX_INJECT=15

# 多智能体（专家委派 + L2/L3）
SHERLOCKING_MULTIAGENT_ENABLED=true
SHERLOCKING_MULTIAGENT_L2_ENABLED=true
SHERLOCKING_MULTIAGENT_L3_ENABLED=true

# Docker 沙箱（容器隔离）
SHERLOCKING_SANDBOX_USE=sherlocking.backend.agents.sandbox.docker.docker_sandbox_provider:DockerSandboxProvider
SHERLOCKING_SANDBOX_EXECUTOR=wsl              # Windows + WSL2 Docker

# MCP 工具
SHERLOCKING_MCP_ENABLED=true
```

---

## 技术栈

- **语言：** Python 3.12+
- **智能体编排：** LangGraph 1.x、LangChain 1.x
- **终端界面：** Rich 13+、Textual 0.40+、prompt_toolkit 3+
- **存储：** SQLite（skill_store）
- **沙箱：** Docker
- **配置：** PyYAML

---

## 基准测试

Sherlocking 内置一套基准测试框架，用于在固定研究任务上评估智能体表现。

```bash
# 运行所有基准任务（10 个任务）
python benchmark/run_benchmark.py

# 仅运行指定任务
python benchmark/run_benchmark.py --filter bm-001,bm-003

# 与上一次运行结果对比
python benchmark/run_benchmark.py --compare

# 跳过 L3 评估采集（更快，仅程序化检查）
python benchmark/run_benchmark.py --no-eval

# 指定供应商
python benchmark/run_benchmark.py --provider deepseek --model deepseek-chat
```

结果保存到 `benchmark/results/<timestamp>/`：
- `summary.md` — 含每个任务得分的人类可读报告
- `summary.json` — 机器可读的结构化数据
- `per-task/<task_id>/` — 每个任务的完整报告、事件日志与得分

详情请见 [`benchmark/tasks.yaml`](benchmark/tasks.yaml) 与 [`benchmark/run_benchmark.py`](benchmark/run_benchmark.py)。

---

## Star

<sub>如果这个项目对你有帮助，欢迎点一个 ⭐。</sub>

</div>
