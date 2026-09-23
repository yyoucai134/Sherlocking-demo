"""Sherlocking Web Gateway — FastAPI WebSocket 服务器。

提供 Web 前端界面，支持流式对话、侧边栏信息面板、命令面板等功能。

启动方式:
    sherlocking web                  # 默认 http://127.0.0.1:8080
    sherlocking web --host 0.0.0.0 --port 9000
    sherlocking web --reload         # 开发模式（热重载）
"""

from sherlocking.backend.app.gateway.server import run_web_server, create_static_dir

__all__ = ["run_web_server", "create_static_dir"]