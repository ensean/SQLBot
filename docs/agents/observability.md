# AgentCore Observability / Evaluations 接入说明

SQLBot 部署在 AgentCore Runtime 之外，通过 ADOT（`aws-opentelemetry-distro`）把 trace 发到 CloudWatch，供 AgentCore Observability 展示、AgentCore Evaluations 评分。默认关闭；不设置 `SQLBOT_OTEL_ENABLED=true` 时没有任何导出，`opentelemetry-api` 的 tracer 为 no-op。完整部署步骤见 `docs/agentcore-deployment.md`。

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

不要开启 OTel 日志导出（`OTEL_LOGS_EXPORTER=none`）：SQLBot 根 logger 为 DEBUG，ADOT 的日志处理器会把应用日志（含完整提示词）以及导出请求自身的 botocore 签名日志一起发到 CloudWatch。`setup_logging` 已把 `botocore`、`urllib3` 限制在 INFO；Evaluations 依赖的内容已在 span 上（`AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true`），不需要日志。

## 一次性 AWS 准备

1. 开启 CloudWatch Transaction Search（trace segment 目的地为 CloudWatch Logs）。
2. 创建 log group，例如 `/aws/bedrock-agentcore/runtimes/sqlbot`，作为在线评估的数据源标识。span 默认写入 Transaction Search 的 `aws/spans`，无需给该 log group 额外的 X-Ray resource policy。
3. 给 SQLBot 进程最小权限凭证：`xray:PutSpans`、`xray:PutTraceSegments` 等写入权限（EC2 上用实例角色，容器需 IMDS hop limit ≥ 2）。
4. 在线评估需要可被 `bedrock-agentcore.amazonaws.com` 扮演的执行角色：对 `aws/spans` 与上述 log group 的 `logs:StartQuery`/`logs:GetQueryResults`，对 `/aws/bedrock-agentcore/evaluations/*` 的写入，以及 `Bedrock-AgentCore/Evaluations` 命名空间的 `cloudwatch:PutMetricData`。

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
OTEL_LOGS_EXPORTER=none
OTEL_RESOURCE_ATTRIBUTES=service.name=sqlbot,aws.log.group.names=/aws/bedrock-agentcore/runtimes/sqlbot
OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=sqlalchemy,psycopg2,pymysql,asyncpg,sqlite3,redis,requests,urllib3,urllib,httpx,jinja2,system_metrics
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
2. 按需评估：AgentCore Python SDK（`bedrock-agentcore`）`EvaluationClient.run(evaluator_ids=[...], session_id="sqlbot-<chat_id>", log_group_name="/aws/bedrock-agentcore/runtimes/sqlbot")`。可用评估器以 `list_evaluators()` 为准；回答属性是 SQL + 结果 JSON，`Builtin.Helpfulness` 会因缺少文字结论偏低。
3. 在线评估：`CreateOnlineEvaluationConfig`，数据源为上述 log group 与 `serviceNames=["sqlbot"]`，结果写入 `/aws/bedrock-agentcore/evaluations/results/<config-id>`；`sessionTimeoutMinutes` 按实际会话间隔设置（默认 15）。
4. 模型拒绝的问题（如删改数据）在非流式 MCP 接口返回 500，属既有行为；根 span 记为 ERROR，回答为拒绝原因。
