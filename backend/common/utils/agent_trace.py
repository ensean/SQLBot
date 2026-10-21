"""OpenTelemetry spans for AgentCore Observability / Evaluations.

Emits spans following the OpenTelemetry GenAI conventions so that AgentCore
Evaluations can rebuild a Chat as a session (``session.id``) and each question
as one trace (``invoke_agent`` root span with ``execute_tool`` children).

Without an SDK TracerProvider (i.e. not started via ``opentelemetry-instrument``)
the API tracer is a no-op and nothing is serialized.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from opentelemetry import baggage, context, trace
from opentelemetry.trace import Span, Status, StatusCode

# AgentCore Evaluations only reads scopes under opentelemetry.instrumentation.* / openinference.instrumentation.*
SCOPE_NAME = "opentelemetry.instrumentation.sqlbot"
AGENT_NAME = "sqlbot"
MAX_CONTENT_CHARS = 4000
MAX_RESULT_ROWS = 20

_tracer = trace.get_tracer(SCOPE_NAME)


def _clip(value: Any) -> str:
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, default=str)
    )
    return text[:MAX_CONTENT_CHARS]


def session_id_for_chat(chat_id: Any) -> str:
    return f"sqlbot-{chat_id}"


class AgentTurn:
    """Collects what the turn produced so the root span can carry the agent response."""

    def __init__(self, span: Span):
        self.span = span
        self.sql: str | None = None
        self.result: str | None = None
        self.error: str | None = None

    def response(self) -> str | None:
        if self.error:
            return f"ERROR: {self.error}"
        parts = []
        if self.sql:
            parts.append(f"SQL:\n{self.sql}")
        if self.result:
            parts.append(f"Result:\n{self.result}")
        return "\n\n".join(parts) or None


_current_turn: ContextVar[AgentTurn | None] = ContextVar(
    "sqlbot_agent_turn", default=None
)


@contextmanager
def agent_turn(
    session_id: str, question: str, **attributes: Any
) -> Iterator[AgentTurn]:
    """Root span of one question; spans created inside inherit session.id through baggage."""
    ctx_token = context.attach(baggage.set_baggage("session.id", session_id))
    try:
        with _tracer.start_as_current_span(f"invoke_agent {AGENT_NAME}") as span:
            turn = AgentTurn(span)
            turn_token = _current_turn.set(turn)
            try:
                if span.is_recording():
                    span.set_attributes(
                        {
                            "gen_ai.operation.name": "invoke_agent",
                            "gen_ai.agent.name": AGENT_NAME,
                            "session.id": session_id,
                            "gen_ai.task.input": _clip(question),
                            "agentcore.invocation.user_prompt": _clip(question),
                            **{k: v for k, v in attributes.items() if v is not None},
                        }
                    )
                yield turn
            finally:
                _current_turn.reset(turn_token)
                if span.is_recording():
                    answer = turn.response()
                    if answer:
                        span.set_attribute("gen_ai.task.output", _clip(answer))
                        span.set_attribute(
                            "agentcore.invocation.agent_response", _clip(answer)
                        )
                    if turn.error:
                        span.set_status(Status(StatusCode.ERROR, turn.error[:256]))
    finally:
        context.detach(ctx_token)


@contextmanager
def tool_call(name: str, arguments: Any, **attributes: Any) -> Iterator[Span]:
    with _tracer.start_as_current_span(f"execute_tool {name}") as span:
        if span.is_recording():
            span.set_attributes(
                {
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": name,
                    "gen_ai.tool.call.arguments": _clip(arguments),
                    **{k: v for k, v in attributes.items() if v is not None},
                }
            )
            session_id = baggage.get_baggage("session.id")
            if session_id:
                span.set_attribute("session.id", str(session_id))
        yield span


def record_sql_result(span: Span, sql: str, result: dict | None) -> None:
    """Attach a truncated SQL result to the tool span and the current turn."""
    if not span.is_recording():
        return
    rows = (result or {}).get("data") or []
    summary = _clip({"row_count": len(rows), "rows": rows[:MAX_RESULT_ROWS]})
    span.set_attribute("gen_ai.tool.call.result", summary)
    turn = _current_turn.get()
    if turn is not None:
        turn.sql = sql
        turn.result = summary


def record_sql(sql: str | None) -> None:
    """Keep the generated SQL as the answer when the turn stops before execution."""
    turn = _current_turn.get()
    if turn is not None and sql and not turn.sql:
        turn.sql = sql


def record_error(message: str) -> None:
    turn = _current_turn.get()
    if turn is not None:
        turn.error = message
