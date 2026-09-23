"""InterviewService — 面试教练服务层。

编排面试教练的五个子命令：
- prep: 简历分析 + 问题库生成
- target: 公司/JD 研究增强
- start: 开始模拟面试
- end: 结束并生成打分卡
- off: 退出面试模式

档案存储路径（优先级）：
1. 沙箱 workspace（agent 可 read_file/write_file）
2. thread_dir 副本（沙箱未启用时）
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from sherlocking.backend.app.bootstrap import AppRuntime

# 项目根目录（本文件位于 sherlocking/backend/app/services/，parents[4] = 项目根）
_PROJECT_ROOT = Path(__file__).parents[4]

# 档案文件名
_DOSSIER_FILENAME = "interview-dossier.md"

# 沙箱 workspace 路径（agent 视角）
_SANDBOX_WORKSPACE = "/mnt/sherlocking/user-data/workspace"

# 本地沙箱 workspace 路径
_LOCAL_SANDBOX_WORKSPACE = _PROJECT_ROOT / ".sherlocking" / "sandbox" / "local" / "workspace"


def _resolve_dossier_path(runtime: AppRuntime) -> Path:
    """解析档案文件的实际路径。

    优先使用沙箱 workspace 路径；若沙箱未启用，使用 thread_dir 副本。
    """
    # 尝试本地沙箱路径
    local_path = _LOCAL_SANDBOX_WORKSPACE / _DOSSIER_FILENAME
    if local_path.parent.exists():
        return local_path

    # 回退到 thread_dir
    thread_path = runtime.thread_dir / _DOSSIER_FILENAME
    return thread_path


def _sandbox_dossier_path() -> str:
    """返回 agent 视角的档案路径（供 SKILL 中 read_file 引用）。"""
    return f"{_SANDBOX_WORKSPACE}/{_DOSSIER_FILENAME}"


def _read_dossier(runtime: AppRuntime) -> str | None:
    """读取现有档案内容。不存在返回 None。"""
    path = _resolve_dossier_path(runtime)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def _write_dossier(runtime: AppRuntime, content: str) -> Path:
    """写入档案文件。自动创建父目录。返回写入的路径。"""
    path = _resolve_dossier_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    # 同时写一份到 thread_dir（归档）
    thread_copy = runtime.thread_dir / _DOSSIER_FILENAME
    thread_copy.parent.mkdir(parents=True, exist_ok=True)
    thread_copy.write_text(content, encoding="utf-8")

    return path


def _parse_resume_path(raw_path: str) -> Path | None:
    """解析简历路径。支持绝对路径和相对路径。"""
    p = Path(raw_path.strip().strip('"').strip("'"))
    if p.is_absolute():
        return p if p.exists() else None
    # 相对路径：相对于项目根
    abs_p = (_PROJECT_ROOT / p).resolve()
    return abs_p if abs_p.exists() else None


def run_prep(runtime: AppRuntime, resume_path: str) -> str:
    """执行 /interview prep：简历分析 + 问题库生成。

    Args:
        runtime: AppRuntime 实例
        resume_path: 简历文件路径

    Returns:
        结果摘要字符串
    """
    # 解析简历路径
    resume = _parse_resume_path(resume_path)
    if resume is None:
        return f"简历文件不存在: {resume_path}"

    # 读取简历内容
    try:
        resume_text = resume.read_text(encoding="utf-8")
    except Exception as e:
        return f"无法读取简历文件: {e}"

    # 控制简历长度（防止上下文溢出）
    if len(resume_text) > 8000:
        resume_text = resume_text[:8000] + "\n\n... (简历过长，已截断至 8000 字符)"

    # 构造研究问题
    research_question = f"""你是一位专业面试教练。请分析以下简历，完成两项任务：

## 任务 1：简历分析
1. 提取技能栈（编程语言、框架、工具、领域）
2. 提取项目经历（名称、角色、技术栈、关键挑战、量化成果）
3. 评估可防御性（哪些是候选人真正做过的？技术深度如何？）
4. 识别优势（哪些点最突出？）
5. 识别风险点（经验空白、描述模糊、弱项目）
6. 识别盲区（该岗位通常要求但简历未提及的技能）

## 任务 2：问题库生成
按 60:40 比例生成结构化问题库：
- 60% 简历驱动题：每个项目生成深挖链（背景→技术选型→难点→量化→复盘）
- 40% 方向题：基于技能栈的基础/进阶/场景题
- 风险点生成压力题

每道题输出 JSON 结构：
```json
{{"question": "...", "type": "technical|behavioral|case", "category": "...", "difficulty": "basic|intermediate|advanced", "follow_up_chain": [...], "key_points": [...], "source": "resume|domain"}}
```

## 输出格式
将完整分析结果写入档案文件 `{_sandbox_dossier_path()}`，使用 write_file 工具。
档案结构：
1. # Candidate Profile（用户画像）
2. # Question Bank（问题库，JSON 格式）
3. 控制档案总长度在 3-8KB，精炼、分节、便于后续引用。

---

## 简历内容
```
{resume_text}
```"""

    # 执行研究（expert 模式 + skill override）
    result = runtime.run_question(
        question=research_question,
        thread_id=runtime.thread_id,
    )

    # 尝试从结果中提取档案内容写入本地
    try:
        report = result.final_report
        if report and len(report) > 200:
            _write_dossier(runtime, report)
    except Exception:
        pass

    dossier_path = _resolve_dossier_path(runtime)
    summary = (
        f"面试准备完成！\n"
        f"档案路径: {dossier_path}\n"
        f"agent 可读路径: {_sandbox_dossier_path()}\n"
        f"下一步: /interview start 开始模拟面试"
    )
    return summary


def run_target(runtime: AppRuntime, target: str) -> str:
    """执行 /interview target：公司/JD 研究增强。

    Args:
        runtime: AppRuntime 实例
        target: 公司名或岗位 JD 文本

    Returns:
        结果摘要字符串
    """
    # 读取现有档案
    existing = _read_dossier(runtime)
    has_dossier = existing is not None

    if not has_dossier:
        return "请先执行 /interview prep <简历路径> 完成简历准备"

    research_question = f"""你是一位专业面试教练。现有面试档案如下，请根据目标公司/岗位进行增强研究。

## 现有档案
```
{existing[:3000]}
```

## 目标公司/岗位
{target}

## 任务
1. 深入研究目标公司（业务模式、文化、近期动态、技术栈）
2. 解析岗位 JD（提取必备技能、加分项、职责）
3. 简历 × 岗位匹配：标注匹配点（重点准备）和差距（准备应答话术）
4. 生成公司特定题：业务理解题、文化契合题、"为什么选我们"题

## 输出格式
将公司研究段追加到档案文件 `{_sandbox_dossier_path()}`，使用 write_file 工具追加内容。
在档案末尾新增 "# Company Research" 章节。"""

    result = runtime.run_question(
        question=research_question,
        thread_id=runtime.thread_id,
    )

    try:
        report = result.final_report
        if report and len(report) > 100:
            # 合并到现有档案
            updated = (existing or "") + "\n\n" + report
            _write_dossier(runtime, updated)
    except Exception:
        pass

    return f"公司研究完成！档案已更新。\n下一步: /interview start 开始模拟面试"


def run_start(runtime: AppRuntime) -> str:
    """执行 /interview start：开始模拟面试。

    读取档案，注入开场 prompt，设置 skill_override。

    Args:
        runtime: AppRuntime 实例

    Returns:
        开场提示字符串（第一题由 agent 在下一轮输出）
    """
    dossier = _read_dossier(runtime)
    if not dossier:
        return "请先执行 /interview prep <简历路径> 完成简历准备"

    # 构建开场 prompt（不立即执行，返回给 CLI 主循环作为下一轮用户输入）
    dossier_path = _sandbox_dossier_path()
    opening_prompt = f"""你是一位专业面试官。请基于以下面试档案，开始模拟面试。

## 面试规则
1. **一次只问一个问题** —— 这是最重要的规则
2. 按四阶段推进：自我介绍 → 技术 → 项目 → 反问
3. 每次用户回答后，先给简短点评（1-2句），再问下一个问题
4. 动态深挖：根据回答现场追问，可使用 web_search 查证
5. 如果上下文丢失了档案，用 read_file 重读 {dossier_path}

## 面试档案
{dossier}

---
现在开始面试。先请候选人做自我介绍。"""

    # 将开场 prompt 写入一个临时文件，避免 CLI 命令行长度限制
    prompt_file = runtime.thread_dir / "interview-opener.txt"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(opening_prompt, encoding="utf-8")

    return f"__INTERVIEW_START__:{prompt_file}"


def run_end(runtime: AppRuntime) -> str:
    """执行 /interview end：生成打分卡。

    Args:
        runtime: AppRuntime 实例

    Returns:
        打分卡字符串
    """
    dossier = _read_dossier(runtime)

    dossier_context = f"\n## 面试档案\n{dossier[:3000]}" if dossier else ""

    scoring_question = f"""你是一位专业面试官。面试已结束，请基于对话历史生成打分卡。{dossier_context}

## 输出格式
1. 总体评分（1-10）
2. 五维度评分：技术深度 / 表达沟通 / 问题解决 / 结构化 / 专业性
3. 优势列表（3-5 条）
4. 改进建议（3-5 条）
5. 逐题反馈（题目、回答概要、评分、参考答案）

请输出完整的打分卡。"""

    result = runtime.run_question(
        question=scoring_question,
        thread_id=runtime.thread_id,
    )

    return result.final_report or "打分卡生成失败"


def run_off() -> str:
    """退出面试模式。"""
    return "__INTERVIEW_OFF__"