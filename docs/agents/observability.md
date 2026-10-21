# AgentCore Observability / Evaluations 接入说明

SQLBot 部署在 AgentCore Runtime 之外，通过 ADOT（`aws-opentelemetry-distro`）把 trace 发到 CloudWatch，供 AgentCore Observability 展示、AgentCore Evaluations 评分。默认关闭；不设置 `SQLBOT_OTEL_ENABLED=true` 时没有任何导出，`opentelemetry-api` 的 tracer 为 no-op。

## Span 契约

源码入口：`backend/common/utils/agent_trace.py`，接入点在 `backend/apps/chat/task/llm.py` 的 `run_task_cache`、`execute_sql`、`save_error`。契约由 `backend/tests/test_agent_trace.py` 守护。

| SQLBot 概念 | Span | 关键属性 |
| --- | --- | --- |
| 会话（Chat） | — | `session.id = sqlbot-<chat_id>`，经 baggage 传给同线程内所有 span（含 ADOT 自动生成的 span） |
| 一次提问（ChatRecord） | `invoke_agent sqlbot`（根） | `gen_ai.task.input` / `agentcore.invocation.user_prompt` 为问题；`gen_ai.task.output` / `agentcore.invocation.agent_response` 为最终 SQL + 截断结果，失败时为 `ERROR: ...` 且 status=ERROR |
| 执行 SQL | `execute_tool execute_sql` | `gen_ai.tool.call.arguments` 为实际执行的 SQL，`gen_ai.tool.call.result` 为行数 + 前 20 行（最多 4000 字符） |
| 每次 LLM 调用 | `chat <model>` | ADOT 内置 `aws_langchain` 自动生成，含完整提示词（表结构、术语、SQL 示例） |

自定义 span 的 scope 为 `opentelemetry.instrumentation.sqlbot`：AgentCore Evaluations 只读取 `opentelemetry.instrumentation.*` / `openinference.instrumentation.*` 前缀，改名会导致不被评估。

当前未覆盖：分析/预测/推荐问题任务、MCP 之外的嵌入渠道差异、按工作空间控制上报。

## 数据外发

开启后问题、生成的 SQL、查询结果前 20 行和完整提示词会写入 CloudWatch。只在已确认数据用途的账号和测试数据源上开启；不需要提示词时禁用 `aws_langchain`。ADOT 还会自动启用数据库客户端插桩（会记录 SQL 语句）和 HTTP 插桩，demo 建议一并禁用：

```bash
OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=sqlalchemy,psycopg2,pymysql,asyncpg,sqlite3,redis,requests,urllib3,urllib,httpx,jinja2,system_metrics
```

## 一次性 AWS 准备

1. 开启 CloudWatch Transaction Search（trace segment 目的地为 CloudWatch Logs）。
2. 创建 log group，例如 `/aws/bedrock-agentcore/runtimes/sqlbot`，并添加允许 `xray.amazonaws.com` 对其 `logs:PutLogEvents` 的 resource policy。
3. 给 SQLBot 进程最小权限凭证（X-Ray 写入、CloudWatch Logs 写入）。

## 运行配置

`start.sh` 在 `SQLBOT_OTEL_ENABLED=true` 时用 `opentelemetry-instrument` 启动 8000 和 8001 两个 uvicorn 进程。

```bash
SQLBOT_OTEL_ENABLED=true
AGENT_OBSERVABILITY_ENABLED=true
AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true
OTEL_PYTHON_DISTRO=aws_distro
OTEL_PYTHON_CONFIGURATOR=aws_configurator
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_TRACES_EXPORTER=otlp
OTEL_RESOURCE_ATTRIBUTES=service.name=sqlbot,aws.log.group.names=/aws/bedrock-agentcore/runtimes/sqlbot
OTEL_EXPORTER_OTLP_LOGS_HEADERS=x-aws-log-group=/aws/bedrock-agentcore/runtimes/sqlbot,x-aws-log-stream=runtime-logs,x-aws-metric-namespace=bedrock-agentcore
OTEL_EXPORTER_OTLP_TRACES_HEADERS=x-aws-log-group=/aws/bedrock-agentcore/runtimes/sqlbot,x-aws-log-stream=spans
AWS_REGION=<region>
```

本地只看 span 结构、不连 AWS 时改为 `OTEL_TRACES_EXPORTER=console OTEL_METRICS_EXPORTER=none OTEL_LOGS_EXPORTER=none`。注意 `python -I` 会忽略 `opentelemetry-instrument` 注入的 `PYTHONPATH`，导致插桩不生效。

## 验证

```bash
cd backend
uv run pytest -q tests/test_agent_trace.py
```

接入 AWS 后：

1. CloudWatch → GenAI Observability 中查看 `sqlbot` 的会话、trace 和 span。
2. 按需评估：AgentCore Python SDK evaluation client 的 `run(evaluator_ids=[...], session_id="sqlbot-<chat_id>", log_group_name="/aws/bedrock-agentcore/runtimes/sqlbot")`。
3. 在线评估：`CreateOnlineEvaluationConfig`，数据源为上述 log group 与 `service.name=sqlbot`；`sessionTimeout` 按实际会话间隔设置（默认 15 分钟）。
