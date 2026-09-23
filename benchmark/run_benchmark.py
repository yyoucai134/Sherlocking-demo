"""Sherlocking Agent Benchmark Runner — 自动化跑批 + 评分 + 汇总报告.

用法:
  # 跑全部考题
  python benchmark/run_benchmark.py

  # 只跑指定题目
  python benchmark/run_benchmark.py --filter bm-001,bm-003

  # 指定 LLM provider（覆盖 .env）
  python benchmark/run_benchmark.py --provider deepseek --model deepseek-chat

  # 轻量模式（非 expert）
  python benchmark/run_benchmark.py --no-expert

  # 跳过 L3 评分收集（只跑任务不评分）
  python benchmark/run_benchmark.py --no-eval

  # 与上次结果对比
  python benchmark/run_benchmark.py --compare

产出:
  benchmark/results/<timestamp>/
    summary.md          — 人类可读汇总报告
    summary.json         — 机器可读结构化结果
    per-task/            — 每道题的完整产物（报告原文 + 事件日志 + 评分）
      <task_id>/
        final_report.md
        events.jsonl
        scores.json
        result.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import yaml

# 项目根目录（benchmark/ 的上一级）
_PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

# 加载 .env
from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / ".env")

_CST = timezone(timedelta(hours=8))


# ── 数据结构 ─────────────────────────────────────────────

@dataclass
class TaskResult:
    """单道考题的跑批结果."""
    task_id: str
    question: str
    category: str
    success: bool = False
    error: str = ""
    # 程序化检查
    keyword_hits: dict[str, bool] = field(default_factory=dict)
    keyword_pass_rate: float = 0.0
    length_pass: bool = False
    report_length: int = 0
    # L3 评分（如果开启）
    l3_scores: dict[str, Any] = field(default_factory=dict)
    # 运行元数据
    run_id: str = ""
    duration_seconds: float = 0.0
    final_report: str = ""
    events_path: str = ""
    artifact_path: str = ""


@dataclass
class BenchmarkSummary:
    """整轮 benchmark 汇总."""
    timestamp: str
    provider: str
    expert_mode: bool
    total_tasks: int
    succeeded: int
    failed: int
    avg_keyword_pass_rate: float
    avg_l3_overall: float | None
    avg_duration_seconds: float
    task_results: list[TaskResult] = field(default_factory=list)
    # 与上次对比的 delta
    delta: dict[str, float] = field(default_factory=dict)


# ── 程序化检查（不依赖 LLM，纯规则匹配） ──────────────────

def check_keywords(report: str, keywords: list[str]) -> dict[str, bool]:
    """检查报告是否包含期望关键词（大小写不敏感）."""
    report_lower = report.lower()
    return {kw: kw.lower() in report_lower for kw in keywords}


def check_length(report: str, min_length: int) -> bool:
    """检查报告是否达到最小字数."""
    return len(report.strip()) >= min_length


# ── L3 评分收集 ───────────────────────────────────────────

def collect_l3_scores(task_id: str, run_id: str) -> dict[str, Any]:
    """从 SQLite skill_store 中收集 L3 评测分数.

    查询路径:
    1. task_quality_scores 表 — 按 task_id 查 4 维分数
    2. skill_eval_judgments 表 — 按 task_id 查技能判断记录
    """
    scores: dict[str, Any] = {}

    # 定位 skills.db
    db_path = _PROJECT_ROOT / ".sherlocking" / "skills.db"
    if not db_path.exists():
        # 尝试 config 中的路径
        try:
            from sherlocking.backend.agents.config.loader import load_config
            config = load_config()
            skill_db = Path(config.skill.db_path) if hasattr(config, "skill") else None
            if skill_db and skill_db.exists():
                db_path = skill_db
        except Exception:
            pass

    if not db_path.exists():
        scores["error"] = "skills.db not found"
        return scores

    try:
        from sherlocking.backend.agents.skill.store import SQLiteSkillStore
        store = SQLiteSkillStore(str(db_path))

        # 1. 查 task_quality_scores
        task_score = store.get_task_scores(task_id)
        if task_score is not None:
            scores["task_quality"] = {
                "task_completion": task_score.task_completion,
                "response_quality": task_score.response_quality,
                "efficiency": task_score.efficiency,
                "tool_usage": task_score.tool_usage,
                "overall_score": task_score.overall_score,
                "rationale": task_score.rationale,
            }

        # 2. 查 skill_eval_judgments（按 task_id）
        with store._mu:
            rows = store._conn.execute(
                "SELECT * FROM skill_eval_judgments WHERE task_id=? ORDER BY timestamp DESC",
                (task_id,),
            ).fetchall()
        if rows:
            scores["skill_judgments"] = [
                {
                    "skill_id": r["skill_id"],
                    "skill_name": r["skill_name"],
                    "skill_applied": bool(r["skill_applied"]),
                    "deviation_note": r["deviation_note"],
                }
                for r in rows
            ]

    except Exception as exc:
        scores["error"] = f"collect_l3_scores failed: {exc}"

    return scores


# ── 核心跑批逻辑 ──────────────────────────────────────────

def run_single_task(
    task: dict[str, Any],
    runtime: Any,
    eval_enabled: bool,
) -> TaskResult:
    """跑单道考题，返回 TaskResult.

    流程:
    1. runtime.run_question(question) → AgentRunResult
    2. 程序化检查（关键词 + 字数）
    3. 收集 L3 评分（如果开启）
    """
    task_id = task["id"]
    question = task["question"]
    result = TaskResult(
        task_id=task_id,
        question=question,
        category=task.get("category", ""),
    )

    start = time.monotonic()

    try:
        # 调用 agent
        run_id = f"bm-{task_id}-{datetime.now(_CST).strftime('%H%M%S')}"
        agent_result = runtime.run_question(
            question=question,
            thread_id=f"benchmark-{task_id}",
            run_id=run_id,
        )
        result.run_id = run_id
        result.final_report = agent_result.final_report or ""
        result.events_path = agent_result.events_path or ""
        result.artifact_path = agent_result.artifact_path or ""

    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        result.duration_seconds = time.monotonic() - start
        return result

    result.duration_seconds = time.monotonic() - start

    # 程序化检查
    keywords = task.get("keywords", [])
    min_length = task.get("min_length", 0)
    result.keyword_hits = check_keywords(result.final_report, keywords)
    result.keyword_pass_rate = (
        sum(result.keyword_hits.values()) / len(result.keyword_hits)
        if result.keyword_hits else 0.0
    )
    result.length_pass = check_length(result.final_report, min_length)
    result.report_length = len(result.final_report.strip())

    # 综合成功判定：关键词命中率 ≥ 60% 且字数达标
    result.success = (
        result.keyword_pass_rate >= 0.6
        and result.length_pass
        and not result.error
    )

    # L3 评分收集
    if eval_enabled:
        result.l3_scores = collect_l3_scores(task_id, result.run_id)

    return result


def save_per_task_artifacts(
    result: TaskResult,
    output_dir: Path,
) -> None:
    """保存单道题的完整产物到 per-task/ 目录."""
    task_dir = output_dir / "per-task" / result.task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    # 最终报告
    (task_dir / "final_report.md").write_text(
        result.final_report, encoding="utf-8",
    )

    # 事件日志（如果存在）
    if result.events_path and Path(result.events_path).exists():
        import shutil
        shutil.copy2(
            result.events_path,
            task_dir / "events.jsonl",
        )

    # 评分
    (task_dir / "scores.json").write_text(
        json.dumps(result.l3_scores, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # 结果摘要
    result_data = {
        "task_id": result.task_id,
        "question": result.question,
        "category": result.category,
        "success": result.success,
        "error": result.error,
        "keyword_hits": result.keyword_hits,
        "keyword_pass_rate": result.keyword_pass_rate,
        "length_pass": result.length_pass,
        "report_length": result.report_length,
        "l3_scores": result.l3_scores,
        "run_id": result.run_id,
        "duration_seconds": result.duration_seconds,
        "events_path": result.events_path,
        "artifact_path": result.artifact_path,
    }
    (task_dir / "result.json").write_text(
        json.dumps(result_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def generate_report(
    summary: BenchmarkSummary,
    output_dir: Path,
) -> None:
    """生成 summary.md（人类可读）和 summary.json（机器可读）."""

    # ── summary.md ──
    lines = [
        f"# Sherlocking Benchmark Report — {summary.timestamp}",
        "",
        f"- **Provider:** {summary.provider}",
        f"- **Expert Mode:** {'✅' if summary.expert_mode else '❌'}",
        f"- **Total Tasks:** {summary.total_tasks}",
        f"- **Succeeded:** {summary.successed} ✅",
        f"- **Failed:** {summary.failed} ❌",
        f"- **Avg Keyword Pass Rate:** {summary.avg_keyword_pass_rate:.1%}",
        f"- **Avg L3 Overall Score:** "
        + (f"{summary.avg_l3_overall:.2f}" if summary.avg_l3_overall is not None else "N/A"),
        f"- **Avg Duration:** {summary.avg_duration_seconds:.1f}s",
    ]

    # 对比 delta
    if summary.delta:
        lines += ["", "## Delta vs Last Run", ""]
        lines.append("| Metric | Last | Current | Delta |")
        lines.append("|--------|------|---------|-------|")
        for key, delta_val in summary.delta.items():
            arrow = "📈" if delta_val > 0 else ("📉" if delta_val < 0 else "➡️")
            lines.append(
                f"| {key} | - | - | {delta_val:+.2f} {arrow} |"
            )

    # 逐题详情
    lines += ["", "## Per-Task Details", ""]
    lines.append("| Task | Category | Success | Keywords | Length | L3 Score | Duration |")
    lines.append("|------|----------|---------|----------|--------|----------|----------|")
    for tr in summary.task_results:
        kw = f"{tr.keyword_pass_rate:.0%}"
        length = f"{tr.report_length}" + (" ✅" if tr.length_pass else " ❌")
        l3 = (
            f"{tr.l3_scores.get('task_quality', {}).get('overall_score', 0):.2f}"
            if tr.l3_scores.get("task_quality") else "N/A"
        )
        succ = "✅" if tr.success else "❌"
        lines.append(
            f"| {tr.task_id} | {tr.category} | {succ} | {kw} | {length} | {l3} | {tr.duration_seconds:.1f}s |"
        )

    # 错误详情
    errors = [tr for tr in summary.task_results if tr.error]
    if errors:
        lines += ["", "## Errors", ""]
        for tr in errors:
            lines += [f"### {tr.task_id}", "```", tr.error[:2000], "```", ""]

    (output_dir / "summary.md").write_text(
        "\n".join(lines), encoding="utf-8",
    )

    # ── summary.json ──
    (output_dir / "summary.json").write_text(
        json.dumps(asdict(summary), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def compare_with_last(
    current: BenchmarkSummary,
    results_root: Path,
) -> dict[str, float]:
    """与上一次 benchmark 结果对比，返回 delta."""
    # 找到上一次的 summary.json（排除当前这次）
    past_dirs = sorted(
        [d for d in results_root.iterdir()
         if d.is_dir() and (d / "summary.json").exists()],
        key=lambda p: p.name,
    )
    # 去掉当前这次
    if past_dirs and past_dirs[-1].name == current.timestamp:
        past_dirs = past_dirs[:-1]
    if not past_dirs:
        return {}

    last_path = past_dirs[-1] / "summary.json"
    last = json.loads(last_path.read_text(encoding="utf-8"))

    delta: dict[str, float] = {}
    if last.get("avg_keyword_pass_rate") is not None:
        delta["avg_keyword_pass_rate"] = (
            current.avg_keyword_pass_rate - last["avg_keyword_pass_rate"]
        )
    if last.get("avg_l3_overall") is not None and current.avg_l3_overall is not None:
        delta["avg_l3_overall"] = current.avg_l3_overall - last["avg_l3_overall"]
    delta["succeeded"] = float(current.succeeded - last.get("succeeded", 0))
    delta["avg_duration"] = current.avg_duration_seconds - last.get("avg_duration_seconds", 0)
    return delta


# ── 主入口 ───────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sherlocking Agent Benchmark Runner",
    )
    parser.add_argument(
        "--tasks", default=None,
        help="考题文件路径（默认 benchmark/tasks.yaml）",
    )
    parser.add_argument(
        "--filter", default=None,
        help="只跑指定题目（逗号分隔 task id，如 bm-001,bm-003）",
    )
    parser.add_argument("--provider", default=None, help="LLM provider")
    parser.add_argument("--model", default=None, help="LLM model name")
    parser.add_argument("--no-expert", action="store_true", help="关闭 expert 模式")
    parser.add_argument("--no-eval", action="store_true", help="跳过 L3 评分收集")
    parser.add_argument("--compare", action="store_true", help="与上次结果对比")
    args = parser.parse_args()

    # 加载考题
    tasks_path = Path(args.tasks) if args.tasks else _PROJECT_ROOT / "benchmark" / "tasks.yaml"
    if not tasks_path.exists():
        print(f"考题文件不存在: {tasks_path}")
        return 1

    with open(tasks_path, encoding="utf-8") as f:
        tasks = yaml.safe_load(f)

    if not tasks or not isinstance(tasks, list):
        print("考题文件为空或格式不正确")
        return 1

    # 过滤题目
    if args.filter:
        wanted = set(args.filter.split(","))
        tasks = [t for t in tasks if t["id"] in wanted]
        if not tasks:
            print(f"过滤后无匹配题目: {args.filter}")
            return 1

    print(f"Loaded {len(tasks)} benchmark tasks")

    # 初始化 agent runtime
    from sherlocking.backend.app.bootstrap import bootstrap_runtime

    expert_mode = not args.no_expert
    cli_overrides: dict[str, Any] = {}
    # benchmark 专用 logs 目录
    timestamp = datetime.now(_CST).strftime("%Y%m%dT%H%M%S")
    logs_root = _PROJECT_ROOT / "benchmark" / "results" / timestamp / "logs"
    cli_overrides["logs_root"] = str(logs_root)

    print(f"Bootstrapping runtime (expert={expert_mode}, provider={args.provider or 'default'})...")
    runtime = bootstrap_runtime(
        expert_mode=expert_mode,
        provider=args.provider,
        model=args.model,
        cli_overrides=cli_overrides,
    )
    print("Runtime ready.")

    # 输出目录
    output_dir = _PROJECT_ROOT / "benchmark" / "results" / timestamp
    output_dir.mkdir(parents=True, exist_ok=True)

    # 逐题跑批
    task_results: list[TaskResult] = []
    eval_enabled = not args.no_eval

    for i, task in enumerate(tasks, 1):
        print(f"\n[{i}/{len(tasks)}] {task['id']}: {task['question'][:60]}...")
        result = run_single_task(task, runtime, eval_enabled)
        task_results.append(result)

        status = "✅ PASS" if result.success else "❌ FAIL"
        print(f"  → {status} ({result.duration_seconds:.1f}s, kw={result.keyword_pass_rate:.0%})")

        # 保存单题产物
        save_per_task_artifacts(result, output_dir)

        # 如果出错，打印错误摘要
        if result.error:
            print(f"  ⚠ Error: {result.error[:200]}")

    # 汇总统计
    succeeded = sum(1 for r in task_results if r.success)
    avg_kw = sum(r.keyword_pass_rate for r in task_results) / len(task_results)
    avg_dur = sum(r.duration_seconds for r in task_results) / len(task_results)

    # L3 平均分
    l3_scores = [
        r.l3_scores.get("task_quality", {}).get("overall_score")
        for r in task_results
        if r.l3_scores.get("task_quality")
    ]
    avg_l3 = sum(l3_scores) / len(l3_scores) if l3_scores else None

    summary = BenchmarkSummary(
        timestamp=timestamp,
        provider=args.provider or "default",
        expert_mode=expert_mode,
        total_tasks=len(task_results),
        succeeded=succeeded,
        failed=len(task_results) - succeeded,
        avg_keyword_pass_rate=avg_kw,
        avg_l3_overall=avg_l3,
        avg_duration_seconds=avg_dur,
        task_results=task_results,
    )

    # 与上次对比
    if args.compare:
        results_root = _PROJECT_ROOT / "benchmark" / "results"
        summary.delta = compare_with_last(summary, results_root)

    # 生成报告
    generate_report(summary, output_dir)

    print(f"\n{'='*60}")
    print(f"Benchmark complete: {succeeded}/{len(task_results)} passed")
    if avg_l3 is not None:
        print(f"Avg L3 Score: {avg_l3:.2f}")
    print(f"Avg Keyword Pass Rate: {avg_kw:.1%}")
    print(f"Avg Duration: {avg_dur:.1f}s")
    print(f"Report: {output_dir / 'summary.md'}")
    print(f"{'='*60}")

    return 0 if succeeded == len(task_results) else 1


if __name__ == "__main__":
    sys.exit(main())
