"""Sherlocking Web Gateway — FastAPI 服务器 + WebSocket 流式通信。

提供两个核心能力：
1. **静态文件服务**：托管 Web 前端页面（index.html）
2. **WebSocket 端点**：流式推送 SherlockingStreamClient 事件到浏览器

启动方式：
    sherlocking web          # 默认 http://localhost:8080
    sherlocking web --port 9000 --host 0.0.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import posixpath
import secrets
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response

from sherlocking.backend.app.bootstrap import bootstrap_runtime, AppRuntime
from sherlocking.backend.app.services.stream_service import SherlockingStreamClient, StreamEvent

# 静态文件目录（本文件同级的 static/ 文件夹）
_STATIC_DIR = Path(__file__).parent / "static"

# 项目根目录（server.py 位于 sherlocking/backend/app/gateway/ 下，parents[4] 即根目录）
_PROJECT_ROOT = Path(__file__).resolve().parents[4]
# 日志文件目录：与配置 logs_root=.sherlocking/logs 保持一致，该目录已在 Docker 中持久化挂载
_LOG_DIR = _PROJECT_ROOT / ".sherlocking" / "logs"
_LOG_FILE = _LOG_DIR / "web.log"
_LOG_MAX_BYTES = 10 * 1024 * 1024  # 单个日志文件 10MB
_LOG_BACKUP_COUNT = 5  # 最多保留 5 个轮转文件（约 50MB 封顶，防止撑爆小硬盘）

logger = logging.getLogger("sherlocking.web")

# WebSocket token auth. Keep the token out of the URL so it is not leaked via
# browser history, reverse-proxy access logs, or referrer headers.
_WEB_AUTH_TOKEN = os.getenv("SHERLOCKING_WEB_AUTH_TOKEN", "").strip()
_MAX_AUTH_FAILURES = 3
_MAX_UPLOAD_BYTES = int(os.getenv("SHERLOCKING_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
_UPLOAD_ROOT = "/mnt/sherlocking/user-data/uploads"
_active_runtimes: dict[str, AppRuntime] = {}
_active_runtimes_lock = threading.Lock()


class _SidFilter(logging.Filter):
    """给所有日志记录注入会话 ID 字段（无会话的系统日志用占位符）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "sid"):
            record.sid = "--------"
        return True


def _setup_logging() -> None:
    """配置全局日志：同时输出到控制台（docker logs）和轮转文件。

    日志级别由环境变量 SHERLOCKING_LOG_LEVEL 控制（DEBUG/INFO/WARNING/ERROR），
    默认 INFO。项目自身 30+ 模块的埋点按此级别输出，第三方库压到 WARNING 防刷屏。
    """
    level_name = os.getenv("SHERLOCKING_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s [%(sid)-8s] %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.WARNING)  # 第三方库（httpx/httpcore 等）默认只看警告

    # ① 控制台 handler —— docker compose logs 可见
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(_SidFilter())
    root.addHandler(console_handler)

    # ② 文件轮转 handler —— 容器重启不丢，磁盘占用有上限
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            _LOG_FILE,
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(_SidFilter())
        root.addHandler(file_handler)
    except OSError as exc:
        # 日志目录不可写时不能阻止服务启动，退化为仅控制台输出
        root.warning("日志文件目录不可用 %s: %s（仅输出到控制台）", _LOG_DIR, exc)

    # 项目自身代码的埋点按配置级别解锁（30+ 模块的 INFO/DEBUG 日志）
    logging.getLogger("sherlocking").setLevel(level)
    logger.info("日志系统就绪 | 级别=%s | 文件=%s", level_name, _LOG_FILE)


def _preview(text: str, limit: int = 200) -> str:
    """把长文本截断成单行预览，防止工具结果刷屏/撑爆日志。"""
    one_line = " ".join(str(text).split())
    if len(one_line) <= limit:
        return one_line
    return one_line[:limit] + f" …(共{len(one_line)}字)"



def _make_app() -> FastAPI:
    """创建 FastAPI 应用，挂载静态文件 + WebSocket 路由。"""
    app = FastAPI(title="Sherlocking Web", version="1.0.0")

    # 静态文件挂载（CSS/JS/图片等）
    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # 首页
    @app.get("/")
    async def index():
        """返回 Web 前端主页面。"""
        index_path = _STATIC_DIR / "index.html"
        if index_path.exists():
            return FileResponse(str(index_path))
        return {"message": "Sherlocking Web Gateway is running. Place index.html in static/ folder."}

    # 健康检查
    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "sherlocking-web"}

    def require_http_auth(authorization: str | None) -> None:
        """Validate the same bearer token used by the WebSocket gateway."""
        if not _WEB_AUTH_TOKEN:
            return
        expected = f"Bearer {_WEB_AUTH_TOKEN}"
        if authorization is None or not secrets.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="authentication required")

    def get_runtime_for_thread(thread_id: str) -> AppRuntime:
        with _active_runtimes_lock:
            runtime = _active_runtimes.get(thread_id)
        if runtime is None:
            raise HTTPException(status_code=404, detail="thread is not connected")
        return runtime

    def safe_upload_name(filename: str) -> str:
        """Flatten a client filename and reject traversal/empty names."""
        normalized = filename.replace("\\", "/")
        name = posixpath.basename(normalized).strip()
        if not name or name in {".", ".."} or len(name) > 180:
            raise HTTPException(status_code=400, detail="invalid filename")
        return name

    async def get_thread_sandbox(thread_id: str):
        runtime = get_runtime_for_thread(thread_id)
        provider = runtime.capability_registry.sandbox_provider
        if provider is None:
            raise HTTPException(status_code=409, detail="sandbox is disabled")
        sandbox_id = await asyncio.to_thread(provider.acquire, thread_id, user_id="web-user")
        sandbox = provider.get(sandbox_id)
        if sandbox is None:
            raise HTTPException(status_code=503, detail="sandbox is unavailable")
        return sandbox_id, sandbox

    @app.post("/api/upload")
    async def upload_file(
        request: Request,
        filename: str = Query(...),
        thread_id: str = Header(..., alias="X-Thread-ID"),
        authorization: str | None = Header(None),
    ):
        """Stream one file into the current thread's sandbox uploads directory."""
        require_http_auth(authorization)
        safe_name = safe_upload_name(filename)
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > _MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="file exceeds upload size limit")

        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > _MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="file exceeds upload size limit")
            chunks.append(chunk)

        sandbox_id, sandbox = await get_thread_sandbox(thread_id)
        virtual_path = f"{_UPLOAD_ROOT}/{safe_name}"
        await asyncio.to_thread(sandbox.update_file, virtual_path, b"".join(chunks))
        return {
            "filename": safe_name,
            "path": virtual_path,
            "bytes": total,
            "sandbox_id": sandbox_id,
        }

    @app.get("/api/files")
    async def list_files(
        thread_id: str = Query(...),
        authorization: str | None = Header(None),
    ):
        """List files uploaded to the current thread's uploads directory."""
        require_http_auth(authorization)
        _sandbox_id, sandbox = await get_thread_sandbox(thread_id)
        entries = await asyncio.to_thread(sandbox.list_dir, _UPLOAD_ROOT, max_depth=1)
        files = [entry for entry in entries if entry and "/" not in entry and "\\" not in entry]
        return {"files": files}

    @app.get("/api/files/{filename}")
    async def download_file(
        filename: str,
        thread_id: str = Query(...),
        authorization: str | None = Header(None),
    ):
        """Download one file from the current thread's uploads directory."""
        require_http_auth(authorization)
        safe_name = safe_upload_name(filename)
        _sandbox_id, sandbox = await get_thread_sandbox(thread_id)
        virtual_path = f"{_UPLOAD_ROOT}/{safe_name}"
        try:
            content = await asyncio.to_thread(sandbox.download_file, virtual_path)
        except Exception as exc:
            raise HTTPException(status_code=404, detail="file not found") from exc
        media_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
        )

    # favicon（避免浏览器 404 日志）
    @app.get("/favicon.ico")
    async def favicon():
        # 返回一个简单的 SVG favicon（Sherlocking 原木暖色系渐变圆点）
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
            '<defs><linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="100%">'
            '<stop offset="0%" style="stop-color:#a06f47"/>'
            '<stop offset="100%" style="stop-color:#d4a441"/>'
            '</linearGradient></defs>'
            '<circle cx="16" cy="16" r="14" fill="url(#g)"/>'
            '<text x="16" y="21" text-anchor="middle" fill="#fffaf2" font-size="14" font-weight="bold" font-family="Georgia,serif">S</text>'
            '</svg>'
        )
        return Response(content=svg, media_type="image/svg+xml")

    return app


# 全局 FastAPI 应用实例
app = _make_app()


@app.websocket("/ws")
async def websocket_chat(ws: WebSocket):
    """WebSocket 聊天端点——接收用户消息，流式返回 Sherlocking 事件。

    协议（JSON）：
    - 客户端 → 服务端：{"type": "chat", "content": "用户问题"}
    - 客户端 → 服务端：{"type": "command", "content": "/help"}
    - 客户端 → 服务端：{"type": "stop"}  —— 取消当前运行中的任务
    - 服务端 → 客户端：{"type": "thinking", "content": "..."}
    - 服务端 → 客户端：{"type": "answer", "content": "..."}
    - 服务端 → 客户端：{"type": "tool_start", "tool_name": "...", "tool_args": {...}}
    - 服务端 → 客户端：{"type": "tool_end", "tool_name": "...", "tool_result": "..."}
    - 服务端 → 客户端：{"type": "budget_update", "budget": {"total": ..., "fraction": ..., "window": ...}}
    - 服务端 → 客户端：{"type": "sandbox_update", "content": "sandbox_id"}
    - 服务端 → 客户端：{"type": "done"}
    - 服务端 → 客户端：{"type": "error", "content": "错误信息"}
    - 服务端 → 客户端：{"type": "status", "content": "状态信息"}
    """
    await ws.accept()
    # 每个连接一个 8 位会话 ID，贯穿该连接所有日志，方便 grep 出完整对话轨迹
    sid = uuid4().hex[:8]
    log = logging.LoggerAdapter(logger, {"sid": sid})
    client = f"{ws.client.host}:{ws.client.port}" if ws.client else "未知来源"
    log.info("🔌 WebSocket 已连接 | 来源=%s", client)

    authenticated = not _WEB_AUTH_TOKEN
    auth_failures = 0
    if not authenticated:
        await ws.send_json({"type": "auth_required"})

    # 为每个 WebSocket 连接创建独立的 AppRuntime
    runtime: AppRuntime | None = None
    # 当前正在运行的任务（用于取消）
    current_task: asyncio.Task | None = None

    try:
        # 发送连接成功消息
        await ws.send_json({
            "type": "status",
            "content": "connected",
            "version": "1.0.0",
        })

        while True:
            # 接收客户端消息
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "content": "无效的 JSON 格式"})
                continue

            msg_type = msg.get("type", "")
            content = msg.get("content", "").strip()

            # Authentication happens before runtime/sandbox initialization.
            if not authenticated:
                if msg_type != "auth":
                    await ws.send_json({"type": "auth_required"})
                    continue
                supplied_token = str(msg.get("token", ""))
                if not secrets.compare_digest(supplied_token, _WEB_AUTH_TOKEN):
                    auth_failures += 1
                    log.warning("🔒 WebSocket 鉴权失败 | attempt=%d", auth_failures)
                    await ws.send_json({"type": "auth_failed", "content": "鉴权失败"})
                    if auth_failures >= _MAX_AUTH_FAILURES:
                        await ws.close(code=1008, reason="authentication failed")
                        return
                    continue
                authenticated = True
                await ws.send_json({"type": "auth_ok"})
                await ws.send_json({"type": "status", "content": "connected"})
                continue

            # 心跳检测
            if msg_type == "ping":
                await ws.send_json({"type": "pong"})
                continue

            # 用户请求停止当前任务
            if msg_type == "stop":
                if current_task and not current_task.done():
                    log.warning("⏹ 收到停止指令，取消当前任务")
                    current_task.cancel()
                    await ws.send_json({"type": "status", "content": "正在停止..."})
                else:
                    await ws.send_json({"type": "status", "content": "当前没有运行中的任务"})
                continue

            if not content and msg_type != "ping":
                continue

            # 如果有任务正在跑，拒绝新的请求
            if current_task and not current_task.done():
                await ws.send_json({
                    "type": "error",
                    "content": "上一条消息还在处理中，请等待完成或发送 {\"type\":\"stop\"} 停止",
                })
                continue

            # 初始化 runtime（首次消息时懒加载）
            if runtime is None:
                await ws.send_json({"type": "status", "content": "正在初始化 Sherlocking 引擎..."})
                try:
                    runtime = _init_runtime()
                    with _active_runtimes_lock:
                        _active_runtimes[runtime.thread_id] = runtime
                    await ws.send_json({
                        "type": "status",
                        "content": "ready",
                        "model": runtime.researcher_model_name,
                        "mode": "expert" if runtime.config.runtime.expert_mode else "default",
                        "thread_id": runtime.thread_id,
                    })
                except Exception as e:
                    await ws.send_json({"type": "error", "content": f"初始化失败: {e}"})
                    await ws.close()
                    return

            # 处理 / 命令（命令不需要异步任务，可以同步处理）
            if content.startswith("/"):
                await _handle_command(ws, content, runtime)
                continue

            # 处理聊天消息——包装成 Task，并与"接收消息"并行等待
            # 这样主循环不会被任务阻塞，用户发 {"type":"stop"} 时能立刻收到并取消
            if msg_type == "chat":
                current_task = asyncio.create_task(
                    _stream_chat(ws, content, runtime, log)
                )
                while not current_task.done():
                    # 并发等待：任务完成 或 客户端发来新消息
                    recv_task = asyncio.create_task(ws.receive_text())
                    done, _pending = await asyncio.wait(
                        {current_task, recv_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    if recv_task not in done:
                        # 聊天任务先结束（完成或被取消），取消未完成的接收
                        recv_task.cancel()
                        break

                    # 收到了客户端的新消息
                    try:
                        raw_next = recv_task.result()
                    except WebSocketDisconnect:
                        current_task.cancel()
                        raise
                    try:
                        next_msg = json.loads(raw_next)
                    except json.JSONDecodeError:
                        continue

                    next_type = next_msg.get("type", "")
                    if next_type == "stop":
                        if not current_task.done():
                            log.warning("⏹ 收到停止指令，取消当前任务")
                            current_task.cancel()
                            await ws.send_json({"type": "status", "content": "正在停止..."})
                    elif next_type == "ping":
                        await ws.send_json({"type": "pong"})
                    else:
                        # 任务运行期间不接受新的聊天/命令，提示用户可停止
                        await ws.send_json({
                            "type": "error",
                            "content": "上一条消息还在处理中，请等待完成或点击「停止」按钮中断",
                        })

                # 吸收取消结果并通知前端
                try:
                    await current_task
                except asyncio.CancelledError:
                    try:
                        await ws.send_json({"type": "error", "content": "已被用户停止"})
                    except Exception:
                        pass
                finally:
                    current_task = None

    except WebSocketDisconnect:
        log.info("🔌 WebSocket 已断开")
        # 断开时也要取消正在跑的任务
        if current_task and not current_task.done():
            current_task.cancel()
    except Exception as e:
        log.exception("💥 WebSocket 异常: %s", e)
        try:
            await ws.send_json({"type": "error", "content": str(e)})
        except Exception:
            pass
    finally:
        # 清理资源
        if runtime is not None:
            with _active_runtimes_lock:
                if _active_runtimes.get(runtime.thread_id) is runtime:
                    _active_runtimes.pop(runtime.thread_id, None)
            try:
                runtime.thread_journal.append("web.session_ended", {})
            except Exception:
                pass


def _init_runtime() -> AppRuntime:
    """初始化 Sherlocking 运行时（在线程池中执行，避免阻塞事件循环）。"""
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(bootstrap_runtime)
        return future.result(timeout=60)


async def _handle_command(ws: WebSocket, cmd: str, runtime: AppRuntime) -> None:
    """处理 / 命令（简化版，仅支持常用命令）。"""
    cmd_lower = cmd.lower().strip()

    # /help — 列出可用命令
    if cmd_lower in ("/help", "/?"):
        commands_help = [
            "/help — 显示命令帮助",
            "/clear — 清空对话",
            "/expert — 切换到专家模式",
            "/default — 切换到默认模式",
            "/thinking on|off — 开关思考过程显示",
            "/model — 显示当前模型",
            "/status — 显示运行状态",
        ]
        await ws.send_json({
            "type": "command_result",
            "content": "\n".join(commands_help),
        })
        return

    # /clear — 清屏（前端处理）
    if cmd_lower == "/clear":
        await ws.send_json({"type": "command_result", "content": "clear"})
        return

    # /expert — 切换专家模式
    if cmd_lower == "/expert":
        await ws.send_json({"type": "status", "content": "正在切换到专家模式..."})
        await ws.send_json({"type": "command_result", "content": "已切换到专家模式（下一轮生效）"})
        return

    # /default — 切换默认模式
    if cmd_lower == "/default":
        await ws.send_json({"type": "status", "content": "正在切换到默认模式..."})
        await ws.send_json({"type": "command_result", "content": "已切换到默认模式（下一轮生效）"})
        return

    # /model — 显示当前模型
    if cmd_lower == "/model":
        model_name = runtime.researcher_model_name
        await ws.send_json({
            "type": "command_result",
            "content": f"当前模型: {model_name}",
        })
        return

    # /status — 显示运行状态
    if cmd_lower == "/status":
        status_info = (
            f"线程ID: {runtime.thread_id}\n"
            f"模型: {runtime.researcher_model_name}\n"
            f"模式: {'专家' if runtime.config.runtime.expert_mode else '默认'}"
        )
        await ws.send_json({"type": "command_result", "content": status_info})
        return

    # 未知命令
    await ws.send_json({
        "type": "command_result",
        "content": f"未知命令: {cmd}。输入 /help 查看可用命令。",
    })


async def _stream_chat(
    ws: WebSocket,
    question: str,
    runtime: AppRuntime,
    log: logging.LoggerAdapter,
) -> None:
    """流式聊天——通过 SherlockingStreamClient 消费 graph 事件，实时推送到 WebSocket。

    与 TUI/CLI 的 _run_research 逻辑一致，但输出目标改为 WebSocket JSON。
    同时把完整对话轨迹（思考/工具调用/结果/耗时）写入日志，便于服务器排查。
    """
    # 轨迹统计：ReAct 循环串行执行，用工具名配对开始/结束时间；
    # counters 贯穿整次对话，helper 内原地累加，结束时汇总
    run_started = time.monotonic()
    tool_times: dict[str, float] = {}
    counters: dict[str, Any] = {"tool_count": 0, "answer_chars": 0, "thinking_chars": 0}
    ctx = None

    try:
        log.info("💬 用户提问: %s", _preview(question, 100))

        # 创建 run context
        ctx = runtime.run_manager.create_run(
            thread_id=runtime.thread_id,
            user_id="web-user",
            run_id=None,
            model_name=runtime.researcher_model_name,
            thread_dir=runtime.thread_dir,
        )
        runtime.run_manager.mark_running(ctx.run_id)
        log.debug("运行上下文已创建 | run_id=%s | thread_id=%s", ctx.run_id, runtime.thread_id)

        # 构建 stream config（与 CLI 的 _build_stream_config 一致）
        from sherlocking.backend.agents.leader.agent import _resolve_actual_model_name
        config = {
            "configurable": {
                "expert_mode": runtime.config.runtime.expert_mode,
                "run_id": ctx.run_id,
                "thread_id": runtime.thread_id,
                "journal": ctx.journal,
                "output_dir": str(ctx.output_dir),
                "plan_enabled": runtime.config.runtime.plan_enabled,
                "timezone": runtime.config.runtime.timezone,
                "model": _resolve_actual_model_name(runtime.capability_registry),
            },
            "recursion_limit": (
                runtime.config.runtime.max_loop_steps
                * runtime.config.runtime.graph_node_multiplier
            ),
        }

        client = SherlockingStreamClient(graph=runtime.leader_agent.graph, config=config)

        # 流式消费事件：转发给浏览器的同时，按事件类型记录轨迹日志
        async for event in client.stream(question):
            # 转换为可序列化的字典
            serializable = _serialize_event(event)
            _log_stream_event(
                serializable,
                log=log,
                tool_times=tool_times,
                counters=counters,
            )
            await ws.send_json(serializable)

        if ctx is not None:
            runtime.run_manager.mark_success(ctx.run_id)
        log.info(
            "✅ 对话完成 | 总耗时 %.1fs | 工具调用 %d 次 | 思考 %d 字 | 回答 %d 字",
            time.monotonic() - run_started,
            counters["tool_count"],
            counters["thinking_chars"],
            counters["answer_chars"],
        )

    except asyncio.CancelledError:
        log.warning("⏹ 对话被用户取消 | 已耗时 %.1fs", time.monotonic() - run_started)
        await ws.send_json({"type": "error", "content": "运行已取消"})
        if ctx is not None:
            try:
                runtime.run_manager.mark_failed(ctx.run_id, "cancelled")
            except Exception:
                pass
    except Exception as e:
        log.exception("💥 对话处理异常: %s", e)
        await ws.send_json({"type": "error", "content": str(e)})
        if ctx is not None:
            try:
                runtime.run_manager.mark_failed(ctx.run_id, str(e))
            except Exception:
                pass


def _log_stream_event(
    event: dict,
    *,
    log: logging.LoggerAdapter,
    tool_times: dict[str, float],
    counters: dict[str, Any],
) -> None:
    """按事件类型记录一条对话轨迹（answer/thinking 流式分片只在 DEBUG 记录）。"""
    etype = event.get("type", "")
    content = event.get("content") or ""

    if etype == "tool_start":
        name = event.get("tool_name") or "unknown"
        tool_times[name] = time.monotonic()
        counters["tool_count"] += 1
        try:
            args_preview = _preview(json.dumps(event.get("tool_args") or {}, ensure_ascii=False), 300)
        except (TypeError, ValueError):
            args_preview = _preview(str(event.get("tool_args")), 300)
        log.info("🔧 调用工具 #%d %s | 参数: %s", counters["tool_count"], name, args_preview)

    elif etype == "tool_end":
        name = event.get("tool_name") or "unknown"
        started = tool_times.pop(name, None)
        elapsed = time.monotonic() - started if started else 0.0
        log.info("🔧 %s 返回 | 耗时 %.1fs | 结果: %s", name, elapsed, _preview(event.get("tool_result") or "", 300))

    elif etype == "thinking":
        # 思考是流式分片：累计全文，等回答开始时再记录结论（结论通常在末尾）
        counters["thinking_chars"] += len(content)
        counters["thinking_text"] = (counters.get("thinking_text", "") + content)[-500:]
        log.debug("🧠 思考流 +%d 字（累计 %d）", len(content), counters["thinking_chars"])

    elif etype == "answer":
        # 第一条回答到来 = 思考结束，补一条思考结论（取末尾 150 字，最有信息量）
        if counters["thinking_chars"] > 0 and not counters.get("thinking_logged"):
            conclusion = _preview(counters.get("thinking_text", ""), 150)
            log.info("🧠 思考完成（%d 字）: %s", counters["thinking_chars"], conclusion)
            counters["thinking_logged"] = 1
        counters["answer_chars"] += len(content)
        log.debug("💬 回答流 +%d 字（累计 %d）", len(content), counters["answer_chars"])

    elif etype == "budget_update":
        budget = event.get("budget") or {}
        fraction = budget.get("fraction")
        if isinstance(fraction, (int, float)):
            log.debug("📊 上下文预算已用 %.0f%%", fraction * 100)

    elif etype == "sandbox_update":
        log.info("📦 沙箱状态变更: %s", content)

    elif etype == "done":
        log.debug("收到 done 事件")

    elif etype == "error":
        log.error("❌ 流式错误事件: %s", _preview(content, 300))



def _serialize_event(event: StreamEvent) -> dict:
    """将 StreamEvent 转换为可 JSON 序列化的字典。

    处理 tool_args 中可能存在的不可序列化对象（如 Pydantic models）。
    """
    result: dict = {
        "type": event.get("type", ""),
        "content": event.get("content", ""),
        "tool_name": event.get("tool_name"),
        "tool_result": event.get("tool_result"),
        "msg_id": event.get("msg_id"),
    }

    # 处理 tool_args —— 可能是 dict 或 Pydantic model
    tool_args = event.get("tool_args")
    if tool_args is not None:
        try:
            if isinstance(tool_args, dict):
                result["tool_args"] = tool_args
            elif hasattr(tool_args, "model_dump"):
                result["tool_args"] = tool_args.model_dump()
            elif hasattr(tool_args, "dict"):
                result["tool_args"] = tool_args.dict()
            else:
                result["tool_args"] = str(tool_args)
        except Exception:
            result["tool_args"] = str(tool_args)

    # 处理 budget 字段
    budget = event.get("budget")
    if budget is not None:
        result["budget"] = dict(budget)

    # 处理 skills 字段
    skills = event.get("skills")
    if skills is not None:
        result["skills"] = list(skills)

    return result


def create_static_dir() -> None:
    """确保 static 目录存在（首次启动时创建）。"""
    _STATIC_DIR.mkdir(parents=True, exist_ok=True)


def run_web_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    reload: bool = False,
) -> None:
    """启动 Sherlocking Web 服务器。

    Args:
        host: 绑定地址（默认 127.0.0.1，部署时用 0.0.0.0）
        port: 监听端口（默认 8080）
        reload: 是否开启热重载（开发用）
    """
    if host not in {"127.0.0.1", "localhost", "::1"} and not _WEB_AUTH_TOKEN:
        raise RuntimeError(
            "SHERLOCKING_WEB_AUTH_TOKEN must be set when the Web server binds "
            "to a non-localhost address"
        )
    import uvicorn

    # 先初始化日志（控制台 + 文件轮转），再启动 uvicorn
    _setup_logging()
    create_static_dir()

    print(f"\n  Sherlocking Web Gateway")
    print(f"  ─────────────────────")
    print(f"  地址: http://{host}:{port}")
    print(f"  WebSocket: ws://{host}:{port}/ws")
    print(f"  日志文件: {_LOG_FILE}")
    print(f"  按 Ctrl+C 退出\n")

    uvicorn.run(
        "sherlocking.backend.app.gateway.server:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )
