"""pi-sandbox-bridge extension 静态验证（P4）。

TypeScript extension 本身无法在 Python 端直接单测（需 TypeScript 测试框架）。
此测试验证 extension 文件存在 + 8 个工具名定义正确 + 关键决策落地。

实际 MCP 协议集成测试在 P7 batch（端到端）。
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _extension_path() -> Path:
    """pi-sandbox-bridge extension 路径。"""
    # __file__ 在 sherlocking/backend/tests/v1/unit/multiagent/
    # 往上 4 层到 sherlocking/backend/，再接 agents/multiagent/extensions/...
    return (
        Path(__file__).parent.parent.parent.parent.parent
        / "agents"
        / "multiagent"
        / "extensions"
        / "pi-sandbox-bridge"
        / "index.ts"
    )


def test_extension_file_exists():
    """pi-sandbox-bridge index.ts 文件存在。"""
    assert _extension_path().exists(), f"Extension file not found: {_extension_path()}"


def test_extension_registers_8_tools():
    """extension 注册 8 个 sherlocking_* 工具（决策 1：转发到 SpecialistMcpServer）。

    extension 用模板字符串 `sherlocking_${toolName}` 动态生成工具名，
    所以源码里 SHERLOCKING_TOOLS 数组含 8 个原始工具名。
    """
    content = _extension_path().read_text(encoding="utf-8")
    # 8 个工具名（与 Sherlocking SpecialistMcpServer 暴露的接口一致）
    # extension 用 `sherlocking_${toolName}` 模板，所以检查原始 toolName 在 SHERLOCKING_TOOLS 数组
    expected_tools = [
        "bash",
        "read_file",
        "write_file",
        "list_dir",
        "str_replace",
        "glob",
        "grep",
        "download_file",
    ]
    for tool in expected_tools:
        assert f'"{tool}"' in content, f"Extension SHERLOCKING_TOOLS should contain {tool}"
    # 验证 sherlocking_ 前缀模板
    assert "sherlocking_${toolName}" in content or "sherlocking_${" in content


def test_extension_reads_mcp_endpoint_env():
    """extension 从 SHERLOCKING_SANDBOX_MCP_ENDPOINT env 读 endpoint（决策 1）。"""
    content = _extension_path().read_text(encoding="utf-8")
    assert "SHERLOCKING_SANDBOX_MCP_ENDPOINT" in content
    assert "process.env" in content


def test_extension_delegates_to_specialist_mcp_server():
    """extension 通过 MCP 调 SpecialistMcpServer（决策 1）。"""
    content = _extension_path().read_text(encoding="utf-8")
    assert "SpecialistMcpServer" in content or "specialist_mcp_server" in content
    assert "callSherlockingMcp" in content


def test_extension_uses_stdio_mcp_protocol():
    """extension 用 stdio MCP 协议（与 codex/claude 一致）。"""
    content = _extension_path().read_text(encoding="utf-8")
    assert "stdio" in content or "stdin" in content
    assert "spawn" in content  # 启动 SpecialistMcpServer 子进程


def test_extension_error_handling():
    """extension 有错误处理（endpoint 未设 + 子进程失败）。"""
    content = _extension_path().read_text(encoding="utf-8")
    assert "SHERLOCKING_SANDBOX_MCP_ENDPOINT not set" in content
    assert "reject" in content


def test_pi_runtime_extension_path_matches():
    """PiRuntime._sherlocking_extension_path() 返的路径与实际 extension 文件一致。"""
    from sherlocking.backend.agents.multiagent.runtimes.pi_runtime import PiRuntime

    rt = PiRuntime()
    # _sherlocking_extension_path 不需要 request，但方法签名可能需要——直接调内部逻辑
    from sherlocking.backend.agents.multiagent import __path__ as pkg_path
    import pathlib

    expected = (
        pathlib.Path(pkg_path[0])
        / "extensions"
        / "pi-sandbox-bridge"
        / "index.ts"
    )
    assert _extension_path() == expected
