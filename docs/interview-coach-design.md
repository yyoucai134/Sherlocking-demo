# 面试教练专用 Agent —— 技术方案设计

> 版本:v5 | 状态:待评审 | 读者:项目作者
> 一句话:在 Sherlocking 通用研究内核上派生"面试教练"专用 agent——**简历优先准备 + 四阶段模拟面试 + 现场动态深挖**;静态工程模式借鉴 interview-guide,动态能力靠 Sherlocking,零内核改动。

---

## 1. 背景与目标

用户需要一个面试辅助 agent,形态为**准备 + 模拟一体**,通用岗位(技术/非技术自适应)。

| 阶段 | 做什么 | 输入 | 输出 |
|---|---|---|---|
| **Phase 1 准备** | 简历分析(必做)→ 公司/岗位研究(可选) | 简历(主输入)+ 公司/岗位 JD(可选) | 《面试档案》:用户画像 / 简历驱动问题库 / [公司段] |
| **Phase 2 模拟** | 四阶段逐轮问答,面试官保持现场研究能力 | 档案 + 对话历史 + 现场查证 | 逐题点评 + 动态追问 + 打分卡 |

## 2. 设计原则

1. **简历优先**:简历是用户能力基准,从简历出发分析;公司/JD 是后补增强,不强求先定公司
2. **动态深挖**:面试官在模拟中保持 web 研究能力,根据实时回答现场查证、动态追问——这是区别于"背题库"的核心
3. **静态结构借鉴 interview-guide**:题目模型 / 评估分层 / 阶段结构 / 去重 / 兜底链等生产验证过的模式直接借用
4. **零内核改动**:全部走 Sherlocking 一等扩展点(技能 / 命令 / 工具 / prompt)

## 3. 借鉴 interview-guide 的静态工程模式

interview-guide(成熟 Java 面试平台)的 AI 交互全是**单次结构化 LLM 调用**,无动态能力;但其静态工程模式经过生产验证,值得借用。**Sherlocking 提供它没有的:动态追问与现场查证。**

| # | 借鉴点 | interview-guide 实现 | 落到本方案 |
|---|---|---|---|
| 3.1 | 题目结构化模型 | `QuestionDTO(question, type, category, topicSummary, followUps)`,追问 `parentQuestionIndex` 关联主问题 | 档案问题库用统一结构:`{主问题, 类型, 分类, 难度, 追问链[], 关键点}`;模拟中每问一题即"已问"标记 |
| 3.2 | 简历题:方向题 6:4 | 有简历时并行生成 60% 简历题 + 40% 方向题,单侧失败降级 | 简历追问链为主体(60%),方向题为补充(40%),并行生成 + 降级 |
| 3.3 | 统一评估分层 | `UnifiedEvaluationService`:分批评估(8 题/批)→ 二次汇总 → 降级兜底 | 打分卡 = 分类聚合分 + 优势/改进 + 逐题参考答案/关键点;长面试分批+汇总+兜底 |
| 3.4 | 阶段化面试 | 总时长 → 自我介绍/技术/项目/反问按时比分配(`currentPhase` 状态机) | 模拟按四阶段推进:自我介绍 → 技术 → 项目 → 反问 |
| 3.5 | JD 驱动出题 | 粘贴 JD → AI 解析生成自定义分类/自定义 skill | `/interview target` 解析岗位 JD 生成考察分类(JD 信息量大于公司名) |
| 3.6 | 结构化输出 + 兜底 | `StructuredOutputInvoker` 统一重试 + 硬编码兜底 | 档案/打分/追问定义 JSON 输出契约 + 解析兜底 |

**不借鉴**:一次性预生成 + 静态追问(Sherlocking 要超越);Java 全栈架构。

## 4. 总体架构(方案 B:技能 + 命令)

```
用户输入(/interview …)
        │
        ▼
CLI 命令层(commands.py)            ── 命令只设 pending_* 标志
        │
        ▼
主循环(main.py 命令处理块)          ── 检测标志,调用服务(async 上下文)
        │
        ▼
InterviewService(app/services/)     ── 编排:档案读写(沙箱路径)+ 构造研究问题
        │
        ▼
runtime.run_question()             ── 既有 ReAct 图 + skill_override=["interview-coach"]
        │
        ▼
result.final_report → 写档案 / 打印追问与打分卡
```

**核心机制:背景知识三层**(深挖依赖,缺一不可)

| 层 | 内容 | 载体 | 生命周期问题 |
|---|---|---|---|
| ① 静态档案 | Phase 1《面试档案》 | 沙箱工作区文件 + 开场 prompt 嵌入 | 长面试中上下文治理(P1/P4)压缩掉开场档案 → **可重读** |
| ② 对话上下文 | 用户实时回答 + 面试官追问 | 对话历史(checkpointer) | 会被压缩 → 靠 ① 重读 + ③ 兜底 |
| ③ 动态查证 | 面试中按需 web 研究 | `web_search` / `browse_page` 现场调用 | 按需触发 |

深挖主力是 ③:回答可疑 / 提到具体技术栈 / 涉简历项目 → 现场搜证再追问。沙箱只负责"档案可重读 + 落盘"(`read_file`/`write_file`),不管深挖。

## 5. 与现有机制对接(全部已核实)

| 机制 | 用法 | 核实点 |
|---|---|---|
| 技能注入 | `skill_override=["interview-coach"]` 强制注入,或 SkillActivationMiddleware 自动建议 | `commands.py` `/skill`;`main.py` override 进 configurable |
| 技能发现 | 新技能放 `skills/`,`load_startup()` 自动发现 | `config.py` `skill_dirs=("skills",)` |
| 命令注册 | `_cmd_interview` handler + `CommandRegistry.register()` | `CommandSpec` + `CommandContext` |
| 异步限制 | 命令只设标志,研究在 main loop async 上下文执行(仿 `/report`) | `pending_report` → `_trigger_report` 先例 |
| 档案 agent 可读 | 写 `<sandbox_workspace>/interview-dossier.md`,agent `read_file` 重读 | 沙箱工具含 read/write/bash;挂载 workspace/uploads/outputs |
| 档案归档 | `<thread_dir>/` 副本 + `LocalArtifactStore` artifact | thread_dir = `.sherlocking/logs/threads/<thread_id>` |
| 现场研究 | 面试官按 SKILL 约束调 web_search/browse_page | `available.py` core 工具,两种模式都加载 |
| 对话连续性 | 模拟每轮是普通消息,checkpointer 保留历史,override 持续 | `get_checkpointer()` 单例 |
| 简历读取 | 命令直接读宿主机简历路径嵌入 prompt;或沙箱 uploads | 沙箱挂载 uploads |
| 上下文治理 | 自动生效;档案被压缩靠 read_file 重读 | DefaultStrategy P1-P5 |
| 长期记忆(可选) | 跨场次沉淀"已问题目/能力画像",下场召回 | MemoryWorker / MemoryMiddleware |

## 6. Phase 1 准备 —— 简历优先,公司可选

### 命令

```
/interview prep <简历路径>            # 必做:简历分析,产用户画像 + 简历驱动问题库
/interview target <公司> [岗位 JD]    # 可选增强:公司研究 + JD 解析考察分类
```

### `/interview prep`(必做)

流程:读简历 → `runtime.run_question(简历分析问题)`(expert 模式)→ 取 final_report 写档案 → 打印路径。

SKILL 子流程:
1. **简历分析**:提取技能栈/项目/经历;评估可防御性(真实做过什么/技术难点/量化成果);识别优势 / 风险点 / 空白
2. **问题库生成**(结构化 + 6:4):每个项目 → 追问链(背景→技术选型→难点→量化→复盘,占 60%);技能栈 → 基础+进阶题(占 40%);风险点 → 压力题
3. 输出契约:`{主问题, 类型(技术/行为/案例), 分类, 难度, 追问链[], 关键点}`

### `/interview target`(可选)

流程:读现有档案 → `runtime.run_question(公司研究 + JD 解析问题)` → "公司段"合并进档案。

SKILL 子流程:
1. 深度研究公司(业务/文化/近期动态)
2. **解析岗位 JD 生成考察分类**
3. 简历 × 岗位匹配:对得上(重点准备)/ 差距(应答话术)
4. 公司特定题:业务理解 / 文化契合 / "为什么选我们"

### 档案结构

1. 用户画像(技能栈/优势/风险点/可深挖项目)
2. 简历驱动问题库(结构化:`{主问题,类型,分类,难度,追问链,关键点}`)
3. [公司段,可选] 公司背景 + JD 考察分类 + 岗位匹配 + 公司特定题

约束:档案精炼(约 3-8KB)、分节、便于 Phase 2 引用。

## 7. Phase 2 模拟 —— 四阶段 + 动态深挖

### 命令

```
/interview start      # 开场(嵌档案,读第一题)
/interview end        # 打分卡 + 清 override
/interview off        # 随时退出,清 override
```

### `/interview start`

1. 读档案(沙箱 workspace 优先;未启用则读 `<thread_dir>` 副本整体嵌入)
2. 开场 prompt:`扮演面试官,基于以下档案开始模拟面试,一次只问一个问题,按阶段推进:自我介绍→技术→项目→反问。深挖时可用 web_search/browse_page 查证;若上下文丢失了档案,用 read_file 重读 <路径>。`
3. `runtime.run_question(开场)` → 打印第一题;`skill_override` 持续注入

### 用户作答(普通消息)

面试官每轮:
1. **点评**:优点 / 改进点 / 参考方向
2. **动态追问**:基于实时回答生成(借鉴 interview-guide 追问链结构但动态化——核心差异)
3. **已问标记**:答完题目在档案标记,避免重复;跨场次可沉淀记忆
4. **深挖触发**:回答可疑 / 提到具体栈 / 涉简历项目 / 上下文丢档案 → 现场调工具
5. **阶段推进**:自我介绍 → 技术 → 项目 → 反问

### `/interview end`

1. 打分 prompt → 输出打分卡
2. 打分卡结构(借鉴 §3.3):分类聚合分(技术/表达/逻辑/结构/专业性)+ 优势/改进 + 逐题反馈/参考答案/关键点;长面试分批评估 → 二次汇总 → 兜底
3. 打印打分卡;清除 `skill_override`

## 8. 数据设计(面试档案)

- **主副本(agent 可读)**:`<sandbox_workspace>/interview-dossier.md`(本地默认 `.sherlocking/sandbox/local/workspace/interview-dossier.md`;Docker 模式经 provider 解析)
- **归档副本**:`<thread_dir>/interview-dossier.md` + `LocalArtifactStore` artifact
- **问题库为结构化 JSON**(便于追问/去重/阶段调度解析)
- **为什么文件而非 state**:跨命令访问确定;agent 可 read_file;可人工编辑
- **为什么不放记忆系统**:档案是一次性会话数据;跨场次的"已问题目/能力画像"才走 L5(可选)

## 9. 实现清单

| 文件 | 动作 | 内容 |
|---|---|---|
| `sherlocking/backend/agents/skill/builtin_skills/core/interview-coach/SKILL.md` | 新增(核心) | 简历分析协议 + 公司/JD 协议 + 问题结构化模型 + 四阶段协议 + 打分卡维度 + 深挖触发条件 + JSON 输出契约 |
| `sherlocking/backend/app/services/interview_service.py` | 新增 | run_prep/run_target/run_start/run_end + 沙箱路径解析 + 档案读写 + 已问标记 + 结构化解析与兜底 |
| `sherlocking/backend/app/cli/commands.py` | 修改 | `_cmd_interview`(5 子命令)+ 注册 `/interview`;set pending_* |
| `sherlocking/backend/app/cli/main.py` | 修改(小) | 命令处理块加 pending_interview_* 检测与调用 |
| `sherlocking/backend/app/cli/command_completer.py` | 修改(可选) | `/interview` 子命令补全 |
| `sherlocking/backend/tests/v1/unit/...` | 新增(可选) | interview_service 单测(解析/兜底/去重) |

不动的:leader / factory / middlewares / memory / context_engineering / sandbox / multiagent / skill 引擎。

## 10. 风险与取舍

1. **LLM 是否遵守"一次一题"**(最大风险):靠 SKILL 文本约束;仍不稳则加轻量中间件强制(升级路径)
2. **现场查证跑偏**:过度搜索拖慢节奏 → SKILL 限定触发条件 + 控制搜索次数
3. **动态追问质量不稳**:相比静态追问可能偶发偏题 → 结构化模型 + 参考答案兜底
4. **档案嵌入体积**:大档案触发治理丢细节 → 档案精炼 + read_file 重读兜底
5. **沙箱未启用**:read_file 不可用 → 回退整体嵌入档案
6. **Docker 档案路径**:interview_service 统一解析,本地模式优先
7. **命令 async 限制**:沿用 pending 机制(已验证)
8. **依赖技能开关**:需 `SHERLOCKING_SKILL_ENABLED=true`
9. **零代码验证(方案 A)**:只写 SKILL.md,直接输"帮我分析这份简历:…"靠自动激活跑通,先验证 LLM 行为再决定命令层

## 11. 里程碑

- **M1(最小闭环)**:SKILL.md + `/interview prep`,真实简历跑通"简历分析 + 结构化问题库(6:4)",评审档案质量
- **M2**:`/interview target` + `/interview start/end`,跑通完整模拟面试(四阶段 + 动态追问 + 打分卡)
- **M3(可选)**:已问去重沉淀记忆 / 档案路径 Docker 解析 / 打分卡落 artifact / 跨场次能力画像
- **M4(可选升级)**:协议中间件强制一次一题 + 查证上限

## 12. 决策记录

- 选**方案 B(技能+命令)**而非纯技能:确定性入口 + 跨阶段档案握手
- **简历优先**:`/interview prep`(必做)+ `/interview target`(可选增强)
- **深挖三层**:档案(可重读)+ 对话历史 + 现场查证
- **借鉴 interview-guide**:题目模型/去重/6:4/评估分层/四阶段/JD 驱动/输出契约+兜底;**动态追问与现场查证是 Sherlocking 独有加分项**
- 选**内置技能目录 `builtin_skills/core/`**：与其他 core 技能（plan、find-skills 等）放在一起，启动时自动加载
- 选**命令写档案文件**而非 agent write_file 落盘:确定性强
