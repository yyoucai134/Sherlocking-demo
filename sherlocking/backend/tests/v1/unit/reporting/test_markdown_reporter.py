import json
import shutil
from pathlib import Path
from uuid import uuid4

from sherlocking.backend.agents.config.loader import load_config
from sherlocking.backend.agents.journal.run_journal import RunJournal
from sherlocking.backend.agents.reporting.markdown_reporter import MarkdownReporter
from sherlocking.backend.agents.runtime.run_context import RunContext
from sherlocking.backend.agents.state.types import Observation, Source


def test_markdown_reporter_generates_report_and_journal_event() -> None:
    temp_dir = _workspace_temp_dir()
    context = _context(temp_dir)
    reporter = MarkdownReporter()
    state = {
        "research_question": "What is Sherlocking?",
        "observations": [
            Observation(
                observation_id="obs-1",
                step_id=None,
                content="Sherlocking is a research agent.",
                source_refs=("s1",),
            )
        ],
        "sources": [Source(source_id="s1", url="https://example.test", title="Example")],
    }

    result = reporter.generate_report(state, context)

    assert "What is Sherlocking?" in result.final_report
    assert "Sherlocking is a research agent." in result.final_report
    # F1: reporter 不再发 report.generated 事件（归 agent.py），不写 events.jsonl。
    assert not (temp_dir / "events.jsonl").exists()

    shutil.rmtree(temp_dir)


def _context(temp_dir: Path) -> RunContext:
    config = load_config(cli_overrides={"logs_root": str(temp_dir)})
    return RunContext(
        run_id="run-1",
        thread_id="thread-1",
        user_id="user-1",
        session_id=None,
        trace_id=None,
        config=config,
        budget={},
        output_dir=temp_dir,
        enabled_middlewares=(),
        journal=RunJournal("run-1", temp_dir / "events.jsonl"),
    )


def _workspace_temp_dir() -> Path:
    path = Path(".sherlocking/test-logs") / uuid4().hex
    path.mkdir(parents=True, exist_ok=False)
    return path
