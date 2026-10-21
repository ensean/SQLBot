"""Span contract required by AgentCore Observability / Evaluations."""

import ast
import json
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NoOpTracer, StatusCode

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from common.utils import agent_trace  # noqa: E402

EXPORTER = InMemorySpanExporter()


def setUpModule():
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
    trace.set_tracer_provider(provider)


def _run_turn(question, body, chat_id=42):
    """Mirror LLMService.run_task_cache: the turn runs inside an executor worker thread."""

    def task():
        with agent_trace.agent_turn(
            agent_trace.session_id_for_chat(chat_id),
            question,
            **{"sqlbot.workspace.id": 1, "sqlbot.chat.record_id": None},
        ):
            body()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(task).result()
    spans = {s.name: s for s in EXPORTER.get_finished_spans()}
    return spans


class AgentTraceTestCase(unittest.TestCase):
    def setUp(self):
        EXPORTER.clear()

    def test_turn_with_sql_tool_call(self):
        def body():
            with agent_trace.tool_call(
                "execute_sql", "SELECT 1", **{"sqlbot.datasource.id": 7}
            ) as span:
                agent_trace.record_sql_result(
                    span, "SELECT 1", {"data": [{"a": 1}], "fields": ["a"]}
                )

        spans = _run_turn("how many orders?", body)
        root, tool = spans["invoke_agent sqlbot"], spans["execute_tool execute_sql"]

        for span in (root, tool):
            self.assertEqual(
                span.instrumentation_scope.name, "opentelemetry.instrumentation.sqlbot"
            )
            self.assertEqual(span.attributes["session.id"], "sqlbot-42")
        self.assertIsNone(root.parent)
        self.assertEqual(tool.parent.span_id, root.context.span_id)
        self.assertEqual(tool.context.trace_id, root.context.trace_id)

        self.assertEqual(root.attributes["gen_ai.operation.name"], "invoke_agent")
        self.assertEqual(root.attributes["gen_ai.task.input"], "how many orders?")
        self.assertEqual(
            root.attributes["agentcore.invocation.user_prompt"], "how many orders?"
        )
        self.assertIn("SELECT 1", root.attributes["gen_ai.task.output"])
        self.assertEqual(
            root.attributes["gen_ai.task.output"],
            root.attributes["agentcore.invocation.agent_response"],
        )
        self.assertEqual(root.attributes["sqlbot.workspace.id"], 1)
        self.assertNotIn("sqlbot.chat.record_id", root.attributes)
        self.assertEqual(root.status.status_code, StatusCode.UNSET)

        self.assertEqual(tool.attributes["gen_ai.operation.name"], "execute_tool")
        self.assertEqual(tool.attributes["gen_ai.tool.name"], "execute_sql")
        self.assertEqual(tool.attributes["gen_ai.tool.call.arguments"], "SELECT 1")
        self.assertEqual(
            json.loads(tool.attributes["gen_ai.tool.call.result"]),
            {"row_count": 1, "rows": [{"a": 1}]},
        )
        self.assertEqual(tool.attributes["sqlbot.datasource.id"], 7)

    def test_turn_error_marks_root_span(self):
        spans = _run_turn(
            "bad question", lambda: agent_trace.record_error("Execute SQL Failed")
        )
        root = spans["invoke_agent sqlbot"]
        self.assertEqual(root.status.status_code, StatusCode.ERROR)
        self.assertEqual(
            root.attributes["gen_ai.task.output"], "ERROR: Execute SQL Failed"
        )

    def test_turn_stopped_before_execution_keeps_generated_sql(self):
        spans = _run_turn("only sql", lambda: agent_trace.record_sql("SELECT 2"))
        self.assertEqual(
            spans["invoke_agent sqlbot"].attributes["gen_ai.task.output"],
            "SQL:\nSELECT 2",
        )

    def test_result_is_truncated(self):
        rows = [{"v": "x" * 100, "n": i} for i in range(50)]

        def body():
            with agent_trace.tool_call("execute_sql", "SELECT v") as span:
                agent_trace.record_sql_result(span, "SELECT v", {"data": rows})

        result = _run_turn("many rows", body)["execute_tool execute_sql"].attributes[
            "gen_ai.tool.call.result"
        ]
        self.assertLessEqual(len(result), agent_trace.MAX_CONTENT_CHARS)
        self.assertTrue(result.startswith('{"row_count": 50'))

    def test_tool_exception_is_recorded_and_reraised(self):
        def body():
            with self.assertRaises(RuntimeError):
                with agent_trace.tool_call("execute_sql", "SELECT boom"):
                    raise RuntimeError("db down")

        tool = _run_turn("boom", body)["execute_tool execute_sql"]
        self.assertEqual(tool.status.status_code, StatusCode.ERROR)
        self.assertEqual(tool.events[0].name, "exception")

    def test_noop_tracer_is_harmless(self):
        with patch.object(agent_trace, "_tracer", NoOpTracer()):
            with agent_trace.agent_turn("sqlbot-1", "q"):
                with agent_trace.tool_call("execute_sql", "SELECT 1") as span:
                    agent_trace.record_sql_result(
                        span, "SELECT 1", {"data": [{"a": 1}]}
                    )
                agent_trace.record_error("ignored")
        self.assertEqual(EXPORTER.get_finished_spans(), ())


class LLMServiceWiringTestCase(unittest.TestCase):
    """Source guard: the chat pipeline must stay wired to the spans (llm.py needs the full stack to import)."""

    @classmethod
    def setUpClass(cls):
        tree = ast.parse(
            (BACKEND_DIR / "apps/chat/task/llm.py").read_text(encoding="utf-8")
        )
        service = next(
            n
            for n in tree.body
            if isinstance(n, ast.ClassDef) and n.name == "LLMService"
        )
        cls.methods = {
            n.name: ast.unparse(n)
            for n in service.body
            if isinstance(n, ast.FunctionDef)
        }

    def test_run_task_cache_wraps_turn(self):
        self.assertIn("agent_trace.agent_turn(", self.methods["run_task_cache"])
        self.assertIn("agent_trace.record_sql(", self.methods["run_task_cache"])

    def test_execute_sql_is_a_tool_call(self):
        self.assertIn(
            "agent_trace.tool_call('execute_sql'", self.methods["execute_sql"]
        )
        self.assertIn("agent_trace.record_sql_result(", self.methods["execute_sql"])

    def test_save_error_is_recorded(self):
        self.assertIn("agent_trace.record_error(", self.methods["save_error"])


if __name__ == "__main__":
    unittest.main()
