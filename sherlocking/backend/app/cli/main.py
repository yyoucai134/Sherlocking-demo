from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Sequence

from dotenv import load_dotenv

# 显式从项目根加载 .env——load_dotenv() 默认只查 CWD，从非项目根启动时
# SHERLOCKING_SKILL_* 等配置不进 env（CWD-relative），导致 skill 模块被误跳过。
# main.py 位于 sherlocking/backend/app/cli/，parents[4] 即项目根。
_PROJECT_ROOT = Path(__file__).parents[4]
load_dotenv(_PROJECT_ROOT / ".env")

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import CompleteStyle
from prompt_toolkit.styles import Style
from rich.console import Console
from sherlocking.backend.app.bootstrap import bootstrap_runtime, AppRuntime
from sherlocking.backend.app.cli.banner import render_banner
from sherlocking.backend.app.cli.command_completer import SlashCommandCompleter
from sherlocking.backend.app.cli.commands import get_registry, handle_command
from sherlocking.backend.app.cli.status_bar import build_bottom_toolbar
from sherlocking.backend.app.cli.stream_handler import StreamRenderer
from sherlocking.backend.app.services.stream_service import SherlockingStreamClient
from sherlocking.backend.agents.intent import default_intent_tree
from sherlocking.backend.agents.leader.agent import _resolve_actual_model_name
from sherlocking.backend.agents.prompts import get_prompt_manager


def main(argv: Sequence[str] | None = None) -> int:
    # 首次启动配置向导：.env 不存在时引导用户配置
    from sherlocking.backend.app.cli.setup_wizard import ensure_config
    if not ensure_config(_PROJECT_ROOT):
        return 1
    load_dotenv(_PROJECT_ROOT / ".env", override=True)

    parser = argparse.ArgumentParser(prog="sherlocking")
    parser.add_argument("--provider", default=None)
    parser.add_argument("--model", default=None)
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("question")
    run_parser.add_argument("--expert", action="store_true", default=True, help="enable expert mode (deep research, default for run)")
    run_parser.add_argument("--no-expert", action="store_false", dest="expert", help="disable expert mode (lightweight)")
    run_parser.add_argument("--thread-id", default="default-thread")
    run_parser.add_argument("--run-id", default=None)
    run_parser.add_argument("--logs-root", default=None)
    run_parser.add_argument("--no-artifact", action="store_true")

    subparsers.add_parser("cli", help="traditional scrolling CLI (prompt_toolkit + rich)")

    web_parser = subparsers.add_parser("web", help="start web server (FastAPI + WebSocket)")
    web_parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    web_parser.add_argument("--port", type=int, default=8080, help="listen port (default: 8080)")
    web_parser.add_argument("--reload", action="store_true", help="enable auto-reload for development")

    args = parser.parse_args(argv)

    if args.command is None:
        return run_chat(provider=args.provider, model=args.model, legacy=False)

    if args.command == "cli":
        return run_chat(provider=args.provider, model=args.model, legacy=True)

    if args.command == "web":
        from sherlocking.backend.app.gateway import run_web_server
        run_web_server(host=args.host, port=args.port, reload=args.reload)
        return 0

    if args.command == "run":
        overrides: dict = {}
        if args.logs_root:
            overrides["logs_root"] = args.logs_root
        if args.no_artifact:
            overrides["save_artifact"] = False
        runtime = bootstrap_runtime(
            expert_mode=args.expert,
            provider=args.provider,
            model=args.model,
            cli_overrides=overrides,
        )
        result = runtime.run_question(
            question=args.question,
            thread_id=args.thread_id,
            run_id=args.run_id,
        )
        print(result.final_report)
        print(f"run_id: {result.run_id}")
        print(f"events_jsonl: {result.events_path}")
        if result.artifact_path:
            print(f"final_report_md: {result.artifact_path}")
        return 0

    return 1


def run_chat(provider: str | None = None, model: str | None = None, legacy: bool = False) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    # bootstrap 在 asyncio.run 之前（同步阶段），避免 MCP 的 asyncio.run 嵌套
    runtime = bootstrap_runtime(provider=provider, model=model)

    if not legacy:
        # 默认走 TUI 全屏应用（textual）
        from sherlocking.backend.app.tui import SherlockingTUI
        app = SherlockingTUI(runtime=runtime, provider=provider, model=model)
        app.run()
        return 0

    return asyncio.run(_run_chat_async(runtime, provider, model))


def _build_stream_config(runtime: AppRuntime, run_context: Any, skill_override: list[str] | None = None) -> dict:
    """构建 graph config（stream 用，与 LeaderAgent.run 一致）。"""
    rc = run_context.config.runtime
    return {
        "configurable": {
            "expert_mode": rc.expert_mode,
            "run_id": run_context.run_id,
            "thread_id": run_context.thread_id,
            "journal": run_context.journal,
            "output_dir": str(run_context.output_dir),
            "plan_enabled": rc.plan_enabled,
            "timezone": rc.timezone,
            "model": _resolve_actual_model_name(runtime.capability_registry),
            "skill_override": skill_override or [],
        },
        "recursion_limit": rc.max_loop_steps * rc.graph_node_multiplier,
    }


async def _run_chat_async(runtime: AppRuntime, provider: str | None, model: str | None) -> int:
    console = Console()
    # cli_state：主循环共享状态——mode/model 供 bottom_toolbar 显示，current_tokens/fraction/window
    # 由 renderer 收到 budget_update 事件时回填（见 stream_handler._update_budget）
    cli_state: dict[str, Any] = {
        "pending_expert_mode": None,
        "pending_mcp_reload": None,
        "skill_override": [],
        "mode": "expert" if runtime.config.runtime.expert_mode else "default",
        "model": _resolve_actual_model_name(runtime.capability_registry),
        "current_tokens": 0,
        "current_fraction": 0.0,
        "current_window": 0,
    }
    # skill_provider：惰性取当前 runtime 的 active skill 名（闭包读最新 runtime，
    # switch/reload 后 runtime 重绑定，闭包见新值）。供 /skill <name> 补全。
    def _skill_names_provider():
        mgr = getattr(runtime, "skill_manager", None)
        if mgr is None:
            return []
        return [s["name"] for s in mgr.list_skills()]

    session: PromptSession = PromptSession(
        completer=SlashCommandCompleter(get_registry(), skill_provider=_skill_names_provider),
        complete_while_typing=True,
        complete_style=CompleteStyle.COLUMN,
        bottom_toolbar=lambda: build_bottom_toolbar(cli_state),
        style=Style([
            ("completion-menu.completion.current", "bg:#6A5ACD fg:#ffffff"),
            ("completion-menu.completion", "bg:#2b2b2b fg:#aaaaaa"),
            ("bottom-toolbar", "bg:#2b2b2b fg:#aaaaaa"),
        ]),
    )
    renderer = StreamRenderer(console=console, cli_state=cli_state)

    def _print_status() -> None:
        mode_label = "expert" if runtime.config.runtime.expert_mode else "default"
        model_name = _resolve_actual_model_name(runtime.capability_registry)
        console.print(f"[dim]Sherlocking v1.0.0 | mode: {mode_label} | {model_name} | thread: {runtime.thread_id[:20]}[/dim]")

    def _print_welcome() -> None:
        """硬编码开场白（从 prompts/system/cli/welcome.md 加载），不调 LLM。"""
        mode_label = "expert" if runtime.config.runtime.expert_mode else "default"
        model_name = _resolve_actual_model_name(runtime.capability_registry)
        try:
            welcome = get_prompt_manager().load(
                "cli", "welcome",
                mode_label=mode_label, model_name=model_name, thread_id=runtime.thread_id[:20],
            )
            console.print(welcome)
        except Exception:
            # welcome.md 加载失败时 fallback 简短文本
            console.print(f"[bold]你好，我是 Sherlocking。[/bold] mode: {mode_label}")
            console.print("[dim]/report 生成报告 | /expert 深度研究 | /default 轻量对话 | /help 全部命令[/dim]\n")

    def _handle_report_intent(intent: Any, rt: Any) -> bool:
        """报告意图 handler：触发报告合成。"""
        topic = intent.payload.get("topic") if intent.payload else None
        _trigger_report(topic, rt, console)
        return True

    def _trigger_report(topic: str | None, rt: AppRuntime, con: Console) -> None:
        """default 模式手动触发报告：调 reporting 服务 + rich Markdown 输出。

        报告生成逻辑（graph.get_state + reporter + artifact）在 agents/reporting 层，
        CLI 仅负责 presentation（expert 提示 + Markdown 渲染）。
        """
        if rt.config.runtime.expert_mode:
            con.print("[yellow]expert 模式已自动生成报告，无需手动触发。[/yellow]\n")
            return
        try:
            from sherlocking.backend.agents.reporting import generate_report_from_thread
            result = generate_report_from_thread(runtime=rt, topic=topic)
        except Exception as exc:
            con.print(f"[red]✗ 报告生成失败: {exc}[/red]\n")
            return
        from rich.markdown import Markdown
        con.print(Markdown(result.final_report))
        if result.artifact_path:
            con.print(f"[dim]report saved: {result.artifact_path}[/dim]\n")
        else:
            con.print()

    intent_tree = default_intent_tree(report_handler=_handle_report_intent)

    provider_label = provider or "default"
    console.print(render_banner("SHERLOCKING"))
    # MCP 工具数量徽标——bootstrap 后统计已加载的 MCP 工具
    try:
        from sherlocking.backend.agents.agent_tools.available import get_available_tools
        from sherlocking.backend.agents.agent_tools.mcp_metadata import is_mcp_tool
        tools = get_available_tools(include_mcp=True)
        mcp_count = sum(1 for t in tools if is_mcp_tool(t))
        console.print(f"[green]●[/green] {mcp_count} MCP tools loaded\n")
    except Exception:
        # 工具加载失败不阻塞 CLI 启动
        pass
    _print_welcome()
    console.print()

    # 主循环
    while True:
        try:
            with patch_stdout():
                user_input = await session.prompt_async("> ")
        except (EOFError, KeyboardInterrupt):
            # Ctrl+C / Ctrl+D 输入时退出进程
            console.print()
            return 0

        prompt = user_input.strip()
        if prompt in {"/exit", "/quit"}:
            return 0
        if not prompt:
            continue
        if prompt.startswith("/"):
            should_exit = handle_command(prompt, console, renderer, cli_state, runtime)
            if should_exit:
                return 0

            # /expert /default 切换：下轮重建 agent（复用 thread_id + checkpointer state）
            pending = cli_state.get("pending_expert_mode")
            if pending is not None:
                runtime = runtime.switch_expert_mode(expert_mode=pending)
                cli_state["pending_expert_mode"] = None
                # 同步 bottom_toolbar 显示的 mode/model
                cli_state["mode"] = "expert" if pending else "default"
                cli_state["model"] = _resolve_actual_model_name(runtime.capability_registry)
                _print_status()
                label = "expert" if pending else "default"
                console.print(f"[green]Switched to {label} mode[/green]\n")

            # /report 命令：触发报告合成
            pending_report = cli_state.get("pending_report")
            if pending_report is not None:
                cli_state["pending_report"] = None
                _trigger_report(pending_report, runtime, console)

            # /mcp reload 命令：重建 leader_agent graph（复用 reload_mcp_tools）
            if cli_state.get("pending_mcp_reload"):
                cli_state["pending_mcp_reload"] = None
                runtime = runtime.reload_mcp_tools()
                console.print("[green]MCP tools reloaded[/green]\n")

            # /model <provider> [model] 命令：热切换 LLM（重建 model+registry+leader_agent，保留 thread）
            pending_model = cli_state.get("pending_model_switch")
            if pending_model is not None:
                cli_state["pending_model_switch"] = None
                provider, model = pending_model
                try:
                    runtime = runtime.switch_model(provider=provider, model=model)
                    cli_state["model"] = _resolve_actual_model_name(runtime.capability_registry)
                    _print_status()
                    console.print(f"[green]Switched to {provider}/{model or 'default'}[/green]\n")
                except Exception as exc:
                    console.print(f"[red]Model switch failed: {exc}[/red]\n")

            # /interview prep 命令：简历分析 + 问题库生成
            pending_interview_prep = cli_state.pop("pending_interview_prep", None)
            if pending_interview_prep:
                from sherlocking.backend.app.services.interview_service import run_prep
                console.print("[bold cyan]面试教练: 简历分析 + 问题库生成中...[/bold cyan]\n")
                result = await asyncio.to_thread(run_prep, runtime, pending_interview_prep)
                console.print(f"[green]{result}[/green]\n")

            # /interview target 命令：公司/JD 研究增强
            pending_interview_target = cli_state.pop("pending_interview_target", None)
            if pending_interview_target:
                from sherlocking.backend.app.services.interview_service import run_target
                console.print("[bold cyan]面试教练: 公司研究中...[/bold cyan]\n")
                result = await asyncio.to_thread(run_target, runtime, pending_interview_target)
                console.print(f"[green]{result}[/green]\n")

            # /interview start 命令：开始模拟面试
            pending_interview_start = cli_state.pop("pending_interview_start", None)
            _interview_start_ok = False  # 标记是否成功注入 prompt
            if pending_interview_start:
                from sherlocking.backend.app.services.interview_service import run_start
                console.print("[bold cyan]面试教练: 激活面试模式...[/bold cyan]\n")
                result = run_start(runtime)
                if result.startswith("__INTERVIEW_START__:"):
                    cli_state["skill_override"] = ["interview-coach"]
                    prompt_file = Path(result.split(":", 1)[1])
                    if prompt_file.exists():
                        prompt = prompt_file.read_text(encoding="utf-8")
                        console.print(f"[green]面试教练模式已激活！[/green]\n")
                        _interview_start_ok = True
                    else:
                        console.print("[yellow]面试档案不存在，请先执行 /interview prep[/yellow]\n")
                else:
                    console.print(f"[yellow]{result}[/yellow]\n")

            # /interview end 命令：生成打分卡
            pending_interview_end = cli_state.pop("pending_interview_end", None)
            if pending_interview_end:
                from sherlocking.backend.app.services.interview_service import run_end
                console.print("[bold cyan]面试教练: 生成打分卡...[/bold cyan]\n")
                result = await asyncio.to_thread(run_end, runtime)
                console.print(result)
                cli_state["skill_override"] = []
                console.print("\n[green]面试教练模式已退出[/green]\n")
                continue

            # /interview start 成功时 fall through 到研究执行，其余 / 命令正常 continue
            if not _interview_start_ok:
                continue

        # 用户输入卡片化回显（/命令不套卡片——它们是系统操作，不是对话）
        renderer.render_user_input(prompt)

        # 意图识别（graph 之前）：命中则不进 graph
        if intent_tree.detect_and_dispatch(prompt, runtime):
            continue

        # 流式研究
        try:
            ctx = runtime.run_manager.create_run(
                thread_id=runtime.thread_id,
                user_id="default-user",
                run_id=None,
                model_name=runtime.researcher_model_name,
                thread_dir=runtime.thread_dir,
            )
            runtime.run_manager.mark_running(ctx.run_id)
            config = _build_stream_config(runtime, ctx, skill_override=cli_state.get("skill_override") or [])
            client = SherlockingStreamClient(graph=runtime.leader_agent.graph, config=config)

            # 注入 round 起始时间 + 模型名，供 _render_done 输出耗时尾行
            import time as _time
            renderer.state["round_t0"] = _time.monotonic()
            renderer.state["model"] = _resolve_actual_model_name(runtime.capability_registry)

            async for event in client.stream(prompt):
                renderer.render(event)

            runtime.run_manager.mark_success(ctx.run_id)
            console.print(f"\n[dim]run_id: {ctx.run_id} | events: {ctx.events_path}[/dim]\n")
        except (KeyboardInterrupt, asyncio.CancelledError):
            console.print("\n[yellow]⚠ Interrupted[/yellow]\n")
            runtime.run_manager.mark_failed(ctx.run_id, "interrupted")
            continue
        except Exception as exc:
            renderer._stop_spinner()
            console.print(f"\n[red]✗ Error: {exc}[/red]\n")
            runtime.run_manager.mark_failed(ctx.run_id, str(exc))
            continue


if __name__ == "__main__":
    raise SystemExit(main())
