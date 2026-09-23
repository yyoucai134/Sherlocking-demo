"""ConversationLog — 可滚动对话区 Widget（textual RichLog）。

渲染 StreamEvent → rich renderable，写入 RichLog 自动滚动。
Thought 折叠 + 计时逻辑与 ``app/cli/stream_handler.py`` 的 StreamRenderer 一致，
但输出目标是 RichLog.write() 而非 console.print()。

复用 ``stream_handler`` 的 ``_tool_color`` / ``_truncate_args`` / ``_result_summary``
helper，避免逻辑重复——TUI 与 CLI 渲染口径完全一致。
"""

from __future__ import annotations

import math
import time
from typing import Any

from rich.box import Box
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from textual.geometry import Offset
from textual.widgets import RichLog

from sherlocking.backend.app.cli.stream_handler import (
    _result_summary,
    _tool_color,
    _truncate_args,
)
from sherlocking.backend.app.services.stream_service import StreamEvent
from sherlocking.backend.app.tui import theme

# 仅左侧竖条的 box——左强调条 + 填充底，用户消息与助手回答共用同一视觉语言，
# 仅强调色不同（用户=蓝紫，助手=青绿），呼应参考设计的对话栏形式。
# 8 行分别对应 top/head/head_row/mid/row/foot_row/foot/bottom：Panel 用 top 画
# 首行、mid 画内容行、bottom 画末行——若仅 mid 行留竖条字符，首/末行会被渲染成
# 空白（背景仍填充），导致竖条比卡片矮一头一尾；这里每一行左列都设为同一字符，
# 保证竖条从卡片第一行到最后一行完整贯通。用 "│"（轻量竖线，U+2502）而非块
# 字符（如 "▏"/"▌"）——块字符的"墨色"贴在字符格左侧边缘，视觉上会比同列的
# InputBox（Textual border-left: solid，渲染同款 "│"）看起来往左偏了小半个
# 字符；换成同一根线字符后两处竖条视觉粗细一致、左边缘完全对齐。
_LEFT_BAR = Box(
    "│   \n"
    "│   \n"
    "│   \n"
    "│   \n"
    "│   \n"
    "│   \n"
    "│   \n"
    "│   \n"
)


class ConversationLog(RichLog):
    """对话区——接收 StreamEvent 并渲染为 rich renderable，自动滚动。

    state 字段与 ``StreamRenderer.state`` 对齐，供 /expand 等命令读取。
    """

    can_focus = False
    """不可聚焦——RichLog 默认可聚焦，鼠标点击对话区会触发 Textual 内置
    ``:focus`` 态的背景高亮蒙版（tint），且该蒙版覆盖范围和底部 InputBox
    的矩形边界不对齐，视觉上像"错位的灰色蒙版"。对话区只用于展示/滚动，
    不需要键盘焦点，直接禁用 focus 从根源上消除这块蒙版。
    """

    DEFAULT_CSS = f"""
    ConversationLog {{
        height: 1fr;
        background: {theme.BG};
        border: none;
        padding: 0 2;
        scrollbar-size: 0 1;
        scrollbar-background: {theme.SCROLLBAR_TRACK};
        scrollbar-color: {theme.SCROLLBAR_THUMB};
        scrollbar-color-hover: {theme.SCROLLBAR_THUMB_HOVER};
        scrollbar-color-active: {theme.SCROLLBAR_THUMB_ACTIVE};
    }}
    """


    def __init__(self) -> None:
        super().__init__(markup=True, auto_scroll=True)
        self._user_pinned = True
        """用户是否仍锚定在底部。True=允许自动跟随；用户手动滚离底部置 False，
        暂停自动跟随，滚回底部（或新轮开始）再恢复——避免流式输出时回看却被拉回。"""
        self._auto_scrolling = False
        """标记 RichLog.write() 内部触发的自动滚动，与用户主动滚动（滚轮/End/滚动条）区分。"""
        self.state: dict[str, Any] = {
            "full_answer": "",
            "tool_results": [],
            "thinking_enabled": True,
            "thinking_log": [],
            "_thinking_t0": None,
            "_thinking_buffer": "",
            "round_t0": None,
            "model": None,
            "_round_active": False,
        }

    def _on_mouse_scroll_down(self, event: Any) -> None:
        """平滑滚轮向下：动画滑行而非瞬时跳变。

        Textual 默认鼠标滚轮走 ``_scroll_down_for_pointer``，硬编码
        ``animate=False``（离散整行跳变）。这里接管并用 ``animate=True`` 让视图
        滑到目标位置，视觉连续；块边界由 Textual 取整，但动画掩去离散感。
        """
        if event.ctrl or event.shift:
            if self.allow_horizontal_scroll:
                self.scroll_relative(
                    x=self.app.scroll_sensitivity_x, animate=True
                )
                event.stop()
            return
        if self.allow_vertical_scroll:
            self.scroll_relative(
                y=self.app.scroll_sensitivity_y, animate=True
            )
            self._user_pinned = (
                self.scroll_y + self.app.scroll_sensitivity_y
            ) >= self.max_scroll_y
            event.stop()

    def _on_mouse_scroll_up(self, event: Any) -> None:
        """平滑滚轮向上——同向下用动画滑行，滚离底部即暂停自动跟随。"""
        if event.ctrl or event.shift:
            if self.allow_horizontal_scroll:
                self.scroll_relative(
                    x=-self.app.scroll_sensitivity_x, animate=True
                )
                event.stop()
            return
        if self.allow_vertical_scroll:
            self.scroll_relative(
                y=-self.app.scroll_sensitivity_y, animate=True
            )
            self._user_pinned = False
            event.stop()

    def write(self, content: Any, *args: Any, **kwargs: Any) -> Any:
        """包裹父类 write：标记内部自动滚动，便于 scroll_end 区分调用来源。"""
        self._auto_scrolling = True
        try:
            return super().write(content, *args, **kwargs)
        finally:
            self._auto_scrolling = False

    def scroll_end(
        self,
        *,
        animate: bool = True,
        speed: float | None = None,
        duration: float | None = None,
        easing: Any = None,
        force: bool = False,
        on_complete: Any = None,
        level: Any = "basic",
        immediate: bool = False,
        x_axis: bool = True,
        y_axis: bool = True,
    ) -> None:
        """自动跟随拦截：write() 内部滚动在用户手动滚离底部时暂停，回到底部恢复。

        RichLog.write() 默认以 ``animate=False`` 调 scroll_end——瞬时跳到底部。
        这里强制 ``animate=True`` + 固定时长，让自动跟随平滑滑行而非跳变。
        用户主动触发（End 键 / 外部代码）一律滚到底并恢复跟随。
        """
        if self._auto_scrolling:
            # 自动跟随：平滑滑行到底（write 传入的 animate 默认 False 是瞬时跳变）
            if self._user_pinned:
                super().scroll_end(
                    animate=True, speed=speed, duration=0.15, easing=easing,
                    force=force, on_complete=on_complete, level=level,
                    immediate=immediate, x_axis=x_axis, y_axis=y_axis,
                )
            elif self.scroll_y >= self.max_scroll_y:
                # 已手动滚回底部——恢复自动跟随
                self._user_pinned = True
                super().scroll_end(
                    animate=True, speed=speed, duration=0.15, easing=easing,
                    force=force, on_complete=on_complete, level=level,
                    immediate=immediate, x_axis=x_axis, y_axis=y_axis,
                )
            # else：用户正停留在上方阅读，暂停自动跟随，保持当前位置
            return
        self._user_pinned = True
        super().scroll_end(
            animate=animate, speed=speed, duration=duration, easing=easing,
            force=force, on_complete=on_complete, level=level,
            immediate=immediate, x_axis=x_axis, y_axis=y_axis,
        )

    def _scroll_to(
        self,
        x: float | None = None,
        y: float | None = None,
        *,
        animate: bool = True,
        speed: float | None = None,
        duration: float | None = None,
        easing: Any = None,
        force: bool = False,
        on_complete: Any = None,
        level: Any = "basic",
        release_anchor: bool = True,
    ) -> bool:
        """所有滚动路径的汇聚点——按目标位置更新锚定状态（滚动条/键盘/程序化滚动）。"""
        if y is not None:
            if y >= self.max_scroll_y:
                self._user_pinned = True
            else:
                self._user_pinned = False
        return super()._scroll_to(
            x, y, animate=animate, speed=speed, duration=duration, easing=easing,
            force=force, on_complete=on_complete, level=level,
            release_anchor=release_anchor,
        )

    def clear(self) -> Any:
        """清空后重置锚定，避免 /clear 后新内容被暂停的自动跟随吞掉。"""
        self._user_pinned = True
        return super().clear()

    @property
    def scroll_offset(self) -> Offset:
        """渲染层块映射：RichLog.render_line 用 ``lines[scroll_y + y]`` 索引，
        必须是整数——返回 ``ceil`` 映射，避免 banker's rounding 的非对称回退。
        scroll_y 内部保持 float（平滑），只在渲染时 ceil 成整行。
        """
        return Offset(round(self.scroll_x), math.ceil(self.scroll_y))

    def validate_scroll_y(self, value: float) -> float:
        """保持 scroll_y 连续 float，仅钳制到有效范围，绝不取整。

        父类默认 validate_scroll_y 已是不取整的 clamp，这里显式覆盖并检查
        max_scroll_y：避免在布局瞬态下 max_scroll_y 偏小时把用户滚动位置
        钳回顶部（回退到第一块）。
        """
        if value <= 0:
            return 0.0
        max_y = self.max_scroll_y
        if max_y <= 0:
            return value
        return value if value < max_y else float(max_y)

    def validate_scroll_target_y(self, value: float) -> float:
        """与 validate_scroll_y 同规则（不取整）——关键修正。

        父类默认 ``validate_scroll_target_y`` 是 ``round(clamp(...))``，取整会让
        ``scroll_target_y`` 与 ``scroll_y`` 分歧（如 scroll_y=1.5 而 target=2、
        scroll_y=0.5 而 target=0），触发 Textual 把滚动位置"修正"对齐到整块，
        正是"滚到块边界回退到第一块"的根源。这里不取整，target 与 scroll_y
        永不分歧。
        """
        return self.validate_scroll_y(value)

    def render_banner(self, banner_text: Text) -> None:
        """启动时渲染 Logo banner。"""
        self.write(banner_text)
        self.write("")

    def render_user_input(self, text: str) -> None:
        """用户输入——全宽左竖条填充卡片。新轮开始重置锚定，回到底部看新回答。"""
        self._user_pinned = True
        self._flush_thinking()
        self.write(Panel(
            Text(text, style=theme.TEXT_PRIMARY),
            box=_LEFT_BAR,
            border_style=theme.ACCENT_USER,
            style=f"on {theme.SURFACE}",
            padding=(0, 2),
            expand=True,
        ))
        self.write("")

    def render_event(self, event: StreamEvent) -> None:
        """StreamEvent → rich renderable，写入 RichLog。"""
        etype = event["type"]

        # budget_update：不渲染到对话区，仅更新外部 cli_state（由 App 处理）
        if etype == "budget_update":
            return

        # 新轮首个事件时清上一轮 state
        if not self.state["_round_active"]:
            self.state["full_answer"] = ""
            self.state["tool_results"] = []
            self.state["thinking_log"] = []
            self.state["_round_active"] = True

        # 从 thinking 切到其他事件时 flush
        if etype != "thinking" and self.state["_thinking_t0"] is not None:
            self._flush_thinking()

        if etype == "thinking":
            self._render_thinking(event)
        elif etype == "answer":
            self._render_answer(event)
        elif etype == "tool_start":
            self._render_tool_start(event)
        elif etype == "tool_end":
            self._render_tool_end(event)
        elif etype == "skill_active":
            self._render_skill_active(event)
        elif etype == "done":
            self._render_done()
        elif etype == "error":
            self._render_error(event)
        elif etype == "compaction_start":
            self.write(Text(f"[compaction] {event.get('tool_name', '')} triggered", style="dim"))
        elif etype == "compaction_progress":
            self.write(Text(f"  {event['content']}", style="dim"))
        elif etype == "compaction_end":
            saved = event.get("tool_result") or ""
            self.write(Text(f"[compaction] done {event['content']} (saved={saved})", style="dim"))

    def _render_thinking(self, event: StreamEvent) -> None:
        """thinking——仅累积 buffer，不实时输出。与 CLI StreamRenderer 一致：
        段落结束（_flush_thinking）才打一行 ``+ Thought: Xms`` 摘要，原文供 /expand。
        """
        if not self.state["thinking_enabled"]:
            return
        content = event["content"]
        if not content:
            return
        if self.state["_thinking_t0"] is None:
            self.state["_thinking_t0"] = time.monotonic()
        self.state["_thinking_buffer"] += content

    def _flush_thinking(self) -> None:
        """thinking 段落收尾——打一行 ``+ Thought: Xms`` 摘要 + 记录 thinking_log。"""
        t0 = self.state["_thinking_t0"]
        if t0 is None:
            return
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        text = self.state["_thinking_buffer"]
        self.write(Text(f"+ Thought: {elapsed_ms}ms", style=theme.ACCENT_THOUGHT))
        self.state["thinking_log"].append({"ms": elapsed_ms, "text": text})
        self.state["_thinking_t0"] = None
        self.state["_thinking_buffer"] = ""

    def _render_answer(self, event: StreamEvent) -> None:
        content = event["content"]
        if content:
            self.state["full_answer"] += content

    def _render_tool_start(self, event: StreamEvent) -> None:
        """工具调用请求行——与 CLI 一致：``tool_name(args)...`` 单行展示参数摘要。"""
        tool_name = event["tool_name"] or "unknown"
        args_str = _truncate_args(event["tool_args"])
        color = _tool_color(tool_name)
        line = Text()
        line.append(f"{tool_name}", style=color)
        if args_str:
            line.append(f"({args_str})...", style=theme.TEXT_DIM)
        else:
            line.append("...", style=theme.TEXT_DIM)
        self.write(line)

    def _render_skill_active(self, event: StreamEvent) -> None:
        skills = event.get("skills") or []
        if not skills:
            return
        label = Text("Skills", style=theme.TEXT_DIM)
        for skill in skills:
            label.append("\n  " + str(skill), style=theme.TEXT_DIM)
        self.write(label)

    def _render_tool_end(self, event: StreamEvent) -> None:
        """工具结果行——与 CLI 一致：``✓ tool_name → result_summary``。

        结果全文仍存入 state["tool_results"] 供 /expand 查看；此处仅展示 80 字摘要，
        让用户实时看到工具是否成功、大致返回了什么，而不是完全静默。
        """
        tool_name = event["tool_name"] or "unknown"
        summary = _result_summary(event["tool_result"])
        color = _tool_color(tool_name)
        line = Text()
        line.append("  ✓ ", style=color)
        line.append(tool_name, style=color)
        line.append(f" → {summary}", style=theme.TEXT_DIM)
        self.write(line)
        if event["tool_result"]:
            self.state["tool_results"].append({"tool": tool_name, "result": event["tool_result"]})

    def _render_done(self) -> None:
        full = self.state["full_answer"].strip()
        if full:
            from sherlocking.backend.app.services.stream_service import _strip_skills_leak
            full = _strip_skills_leak(full)
        if full:
            self.write("")
            self.write(Markdown(full, style=theme.TEXT_PRIMARY))
        # 耗时尾行——■ Build · model · Ns
        round_t0 = self.state.get("round_t0")
        model = self.state.get("model")
        if round_t0 is not None and model:
            import time
            elapsed = time.monotonic() - round_t0
            tail = Text()
            tail.append("  ■ ", style=theme.ACCENT_ASSISTANT)
            tail.append("Build", style=theme.TEXT_SECONDARY)
            tail.append(f" · {model} · {elapsed:.1f}s", style=theme.TEXT_DIM)
            self.write(tail)
        self.write("")
        self.state["full_answer"] = ""
        self.state["_round_active"] = False
        self.state["round_t0"] = None

    def _render_error(self, event: StreamEvent) -> None:
        self.write(Text(f"✗ {event['content']}", style=theme.ACCENT_WARN))

    def expand_last_round(self) -> None:
        """展开上一轮 Thought + 工具结果全文。"""
        thinking_log = self.state.get("thinking_log", [])
        results = self.state.get("tool_results", [])
        if not results and not thinking_log:
            self.write(Text("No previous round data to expand", style=theme.TEXT_DIM))
            return
        for entry in thinking_log:
            self.write(Panel(
                Text(entry["text"], style=theme.TEXT_SECONDARY),
                title=f"Thought ({entry['ms']}ms)",
                title_style=theme.ACCENT_THOUGHT,
                border_style=theme.BORDER,
            ))
        for r in results:
            self.write(Panel(
                Text(r["result"], style=theme.TEXT_SECONDARY),
                title=r["tool"],
                title_style=theme.TOOL_BLUE,
                border_style=theme.BORDER,
            ))
