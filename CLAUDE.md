# Sherlocking — Claude Code 项目记忆

> 本文档是项目的**记忆基线**,记录核心架构与搭建方式,并跟踪后续改造。Claude Code 每次在此目录启动会自动加载本文件。**改造记录追加到文末"改造日志"章节**;开始新改造前先读本文件 + 相关源码确认现状。

## 项目身份
- 定位:深度研究(deep research)Agent 内核,强调架构解耦、可评估、可测试
- 技术栈:Python 3.12+ / LangGraph 1.x / LangChain / DeepSeek(主 LLM)/ pydantic / rich / textual / prompt_toolkit / SQLite / Docker
- 规模:约 560 个 Python 文件,2721 个测试函数(README 声称 2400+)
- 入口:`sherlocking = sherlocking.backend.app.cli.main:main`(pyproject.toml)
- 配置:.env 的 `SHERLOCKING_*` 环境变量;首次启动 setup_wizard 引导
- 用户用途:写简历,重点讲"架构如何搭建"

## 核心搭建方式(一次运行如何组装)

### 1. Bootstrap 装配层(`sherlocking/backend/app/bootstrap.py`)
`bootstrap_runtime(expert_mode, provider, model)` 是总装入口,顺序:
1. `load_config()` 加载 AppConfig(frozen dataclass)
2. 相对路径锚定 `_PROJECT_ROOT`(externalize_dir / memory.storage_path / logs_root)
3. `ModelRouter` 构建 LLM:researcher/reporter 角色,返回 `FallbackChatModel` 路由链;`--provider` 强制单 provider
4. MCP 工具加载(`McpManager`);builtin 工具(ddg_search/read_snapshot)始终注册
5. Sandbox provider 反射加载(`config.sandbox.use` 的 `module:Class`)
6. Skill 模块加载(`build_skill_manager`)+ evolution/eval 装配
7. Multi-Agent 装配(`setup_multiagent`),注入 `_subagent_factory`(leaf 角色不能递归 spawn)
8. Memory provider + MemoryWorker 守护线程启动
9. **`CapabilityRegistry`**(DI 容器:models/tools/reporter/artifact_store/sandbox_provider/skill_store/specialist_registry/subagent_provider/memory_provider)
10. `make_lead_agent()` 构建最终 graph → 返回 `AppRuntime`

`AppRuntime` 不可变,支持热切换(返回新实例,保留 thread 连续性):`switch_expert_mode()` / `switch_model()` / `reload_mcp_tools()` / `run_question()`。

### 2. Agent 内核(`leader/factory.py` + `leader/agent.py`)
- `make_lead_agent()` 用 `create_agent(model, tools, middleware, system_prompt, state_schema, checkpointer)` 构建 **LangGraph ReAct 图**
- **21+ 个 `AgentMiddleware`** 横切钩子:`before/after_agent`、`before/after_model`、`wrap_tool_call`(含 async)。记忆/技能/沙箱/压缩/工具配对/求助/治理全是中间件,不嵌进 agent 循环
- `_build_middlewares()` 定义挂载顺序(重要契约):治理层(3+StrategyMiddleware) → SystemContext → SkillInjection(条件) → SkillMetrics(条件) → Title → RunJournal → MCP Audit(条件) → Sandbox(条件) → Memory L4(条件) → MemoryConsolidation L5(条件) → HelpRequest → DanglingToolCall → ToolCall → Orchestration(条件) → Evidence → StallDetection → Todo → Reflection → Report
- **default vs expert 模式**:同一套中间件参数化(expert=Todo 强制完成、Reflection 充分性 jump、Report 自动合成、工具含 deferred)
- `LeaderAgent` 薄壳:graph.ainvoke + 取 final_report + 存 artifact
- checkpointer 单例,thread_id 跨轮/跨重建/跨模式保留 state

### 3. 中间件基座与状态
- 基类:`langchain.agents.middleware.types.AgentMiddleware`,每个中间件 `state_schema = ThreadState`
- `ThreadState`(state/types.py):messages, user_input, observations, sources, citations, artifacts, reflection_items, errors, metadata, governance, sandbox, orchestration, recalled_memories, memory_updates

### 4. 五层长期记忆(`memory/`)
| 层 | 职责 | 关键点 |
|---|---|---|
| L1 | `schema.py` | `MemoryTrace` frozen dataclass(15 字段)+ `MemoryType` enum + 5 原子操作。**"tools have no LLM"**——原子操作纯数据变换 |
| L2 | `strategies/default/` | Ebbinghaus 衰减 `strength = base×(1-decay)^hours + log(1+access)×0.1 + importance×0.05`;**lazy decay**(retrieve 时算);复合遗忘(TTL + strength 阈值) |
| L3 | `store.py` + `retriever.py` | `MarkdownFileStore`:单文件 `traces.md` truth source + `<!-- trace: {id} -->` 分隔符 + YAML frontmatter;`HybridRetriever` 纯 BM25;检索强化写回;forgotten 过滤;增量索引 |
| L4 | `middlewares/memory_recall_middleware.py` | `abefore_model` 每轮注入 `HumanMessage`(**保护 prompt cache**,`hide_from_ui=True`);`set_turn_id` ContextVar 注入/清除 |
| L5 | `memory_consolidation_middleware.py` + `worker.py` | `aafter_model` 每 N 轮非阻塞 submit + `MemoryWorker`(daemon thread + Queue);LLM 抽取→encode→合并→consolidate;**错误 log+skip 不阻塞主循环** |

设计不变量:记忆注入是 per-call `HumanMessage`(非 system prompt);`recalled_memories` 只存索引(id+score+strength);`MemoryConfig` 4 个 STARTUP_ONLY 字段(use/storage_path/vector_store/graph_store)。

### 5. 上下文治理(`context_engineering/`)
- `DefaultStrategy`:P1-P5 阈值(0.40 externalize / 0.50 thinking / 0.60 observations / 0.80 summarize / 0.90 stop toolcall / 0.99 hard stop)
- `resolve_window_size()`(utilities.py)**穿透 `FallbackChatModel`** 取真实 provider 窗口,不硬编码
- 双策略:压缩(summarize)+ 外化(externalize);`_ensure_pairing` 补缺失 ToolMessage

### 6. 多智能体编排(`multiagent/`)
- `OrchestrationMiddleware.wrap_tool_call` 拦截 `delegate_to_*`:四计数器打点 + 写 orchestration;失败转 error ToolMessage(INV#6)
- `delegate_to_specialist` → 外部 CLI(pi/codex/claude)经 MCP,**共享同一 Docker sandbox**(`--sandbox-url` 透传)
- `delegate_to_subagent` → self-copy,隔离上下文但共享 thread sandbox(SandboxMiddleware 从 state 恢复 ContextVar)
- L2 进化:IVEFocuser 诊断 → LLMMutator 变异 → ScoreDeltaGate 门控 → GitRatchet 回滚
- L3 评估:执行判断 / 任务质量 4 维 / 响应契约,`RuntimeTracker` 反馈退化信号

### 7. 沙箱隔离(`sandbox/`)
- 双 provider:Local / Docker
- `DockerPathTranslator`:translate 直传,reverse_translate 把 `/mnt/sherlocking/user-data/<x>` 映射回 `<sandbox_root>/<sandbox_id>/<x>`(artifact 提取修复)
- `DockerPathGuard`:写路径白名单(write_file/str_replace 须在 `/mnt/sherlocking/user-data/` 下,bash 重定向须进 mount 区)——**强制 agent 写入持久化**
- 温池、空闲自动销毁(10min)、跨进程锁、WSL2 执行器(`D:\` → `/mnt/d/`)

### 8. Skill 系统(`skill/`)
- 三层:L1 基础(SQLite + version DAG + 注入中间件 + 四计数器)、L2 进化、L3 评估
- 36 个内置 skill,5 类;core 自动加载,其余 `/skill search`
- skill = "研究流程知识包"(prompt 级注入,非可执行函数)

### 9. 多 LLM 回退(`config/fallback_model.py`)
- `FallbackChatModel`:按角色优先级路由,瞬时 API 错误(限流/超时/5xx)降级,记忆活跃 provider;deepseek 链尾兜底;400/401 客户端错误不降级

### 10. UI 与可观测性
- TUI(默认,Textual)+ CLI(`sherlocking cli`,prompt_toolkit + rich)
- `RunJournal`:结构化事件写 events.jsonl;`/expand` 展开上轮 Thought

## 目录速查
- `sherlocking/backend/app/` — bootstrap.py(总装)、cli/、tui/、services/
- `sherlocking/backend/agents/` — leader/、middlewares/(21 个)、memory/、context_engineering/、multiagent/、sandbox/、skill/、mcp/、config/、state/、runtime/、journal/、capabilities/(DI 容器)
- `sherlocking/backend/tests/v1/unit/` — 对应模块单测
- `resource/` — README 图片、USAGE 多语言

## 改造日志
> 后续对项目的每次修改在此追加(日期 + 改动 + 为什么)。改造前必读此节 + 对应源码。

(暂无记录 — 2026-08-15 建立本文件)
