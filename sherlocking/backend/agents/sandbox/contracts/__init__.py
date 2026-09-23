"""Sandbox 抽象契约层（Protocol + ABC）。

方案 C 三组件契约 + Provider/Provisioner 契约。
具体实现见 runtimes/ / translators/ / guards/ / local/ / docker/。
"""

from __future__ import annotations

from sherlocking.backend.agents.sandbox.contracts.path_translator import PathTranslator
from sherlocking.backend.agents.sandbox.contracts.sandbox_backend import SandboxBackend
from sherlocking.backend.agents.sandbox.contracts.sandbox_provider import SandboxProvider
from sherlocking.backend.agents.sandbox.contracts.sandbox_runtime import SandboxRuntime
from sherlocking.backend.agents.sandbox.contracts.security_guard import SecurityGuard

__all__ = [
    "PathTranslator",
    "SandboxBackend",
    "SandboxProvider",
    "SandboxRuntime",
    "SecurityGuard",
]
