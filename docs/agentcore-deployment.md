# SQLBot 接入 AgentCore Observability / Evaluations 部署指引

本文说明如何把 SQLBot 部署在 AgentCore Runtime 之外（以单台 EC2 + Docker 为例），并接入 Amazon Bedrock AgentCore Observability 与 Evaluations。埋点约定、属性含义和数据外发说明见 `docs/agents/observability.md`。

已验证环境：SQLBot 1.10.2、`aws-opentelemetry-distro` 0.21.0、区域 `ap-northeast-1`、模型 Bedrock `qwen.qwen3-235b-a22b-2507-v1:0`（OpenAI 兼容接口）。

## 架构

```
用户 / MCP 客户端
      │  (SSM 端口转发，实例无入站规则)
      ▼
EC2 ── Docker: SQLBot (opentelemetry-instrument + ADOT)
      │   ├─ LLM 调用 ──────────────► Bedrock (OpenAI 兼容接口)
      │   └─ OTLP spans (SigV4) ────► X-Ray ─► CloudWatch Logs: aws/spans (Transaction Search)
      ▼
AgentCore Evaluations ── 读取 aws/spans（按 service.name + session.id 组装会话）
      └─ 结果写入 /aws/bedrock-agentcore/evaluations/results/<config-id> 与 Bedrock-AgentCore/Evaluations 指标
```

每次提问产生一个 trace：`invoke_agent sqlbot`（根）下包含 ADOT 自动生成的 `chat <model>` span 和 SQLBot 的 `execute_tool execute_sql` span；同一会话（Chat）的所有 span 带 `session.id = sqlbot-<chat_id>`。

## 1. 前置条件

- 一个测试用 AWS 账号，具备创建 IAM 角色、EC2、CloudWatch Logs 资源的权限；本地安装 AWS CLI 和 Session Manager 插件。
- 目标区域已开通所用 Bedrock 模型，且 AgentCore Evaluations 可用。
- 只使用测试数据源：问题、生成的 SQL、查询结果前 20 行和完整提示词（表结构、术语、SQL 示例）都会写入 CloudWatch。

下文用 `<REGION>`、`<ACCOUNT_ID>` 作占位符。

## 2. 一次性 AWS 准备

### 2.1 开启 CloudWatch Transaction Search

控制台：CloudWatch → Application Signals (APM) → Transaction search → Enable，勾选 ingest spans as structured logs。控制台会自动创建 X-Ray 写入 `aws/spans` 所需的 Logs resource policy。

```bash
aws xray get-trace-segment-destination --region <REGION>   # Destination 应为 CloudWatchLogs，Status 从 PENDING 变为 ACTIVE
```

### 2.2 创建 SQLBot log group

```bash
aws logs create-log-group --log-group-name /aws/bedrock-agentcore/runtimes/sqlbot --region <REGION>
aws logs put-retention-policy --log-group-name /aws/bedrock-agentcore/runtimes/sqlbot --retention-in-days 7 --region <REGION>
```

该 log group 作为在线评估的数据源标识，并写进 `aws.log.group.names` 资源属性；span 本身写在 `aws/spans`。

### 2.3 EC2 实例角色

信任 `ec2.amazonaws.com`，附加 `AmazonSSMManagedInstanceCore`，以及以下内联策略：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow",
     "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:PutSpans", "xray:PutSpansForIndexing",
                "xray:GetSamplingRules", "xray:GetSamplingTargets"],
     "Resource": "*"},
    {"Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*",
     "Condition": {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}},
    {"Effect": "Allow",
     "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:CallWithBearerToken"],
     "Resource": "*"}
  ]
}
```

正式环境把 Bedrock 的 `Resource` 收窄到所用模型。

### 2.4 在线评估执行角色

信任策略（限定本账号、本区域）：

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
    "Action": "sts:AssumeRole",
    "Condition": {
      "StringEquals": {"aws:SourceAccount": "<ACCOUNT_ID>"},
      "ArnLike": {"aws:SourceArn": "arn:aws:bedrock-agentcore:<REGION>:<ACCOUNT_ID>:*"}
    }
  }]
}
```

权限策略：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": "logs:DescribeLogGroups", "Resource": "*"},
    {"Effect": "Allow",
     "Action": ["logs:StartQuery", "logs:GetQueryResults", "logs:FilterLogEvents", "logs:GetLogEvents"],
     "Resource": ["arn:aws:logs:<REGION>:<ACCOUNT_ID>:log-group:aws/spans",
                  "arn:aws:logs:<REGION>:<ACCOUNT_ID>:log-group:aws/spans:*",
                  "arn:aws:logs:<REGION>:<ACCOUNT_ID>:log-group:/aws/bedrock-agentcore/runtimes/sqlbot",
                  "arn:aws:logs:<REGION>:<ACCOUNT_ID>:log-group:/aws/bedrock-agentcore/runtimes/sqlbot:*"]},
    {"Effect": "Allow", "Action": ["logs:GetQueryResults", "logs:StopQuery"], "Resource": "*"},
    {"Effect": "Allow",
     "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"],
     "Resource": "arn:aws:logs:<REGION>:<ACCOUNT_ID>:log-group:/aws/bedrock-agentcore/evaluations/*"},
    {"Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*",
     "Condition": {"StringEquals": {"cloudwatch:namespace": "Bedrock-AgentCore/Evaluations"}}}
  ]
}
```

> 在 zsh 中用 heredoc 拼 ARN 时，`$ACCOUNT_ID:log-group` 里的 `:l` 会被当作小写修饰符吞掉字符，请写成 `${ACCOUNT_ID}:log-group` 或直接用文件。

## 3. 启动 EC2

- AMI：Amazon Linux 2023（x86_64）；实例类型 `m7i.xlarge`（4 vCPU / 16 GiB，内置 embedding 模型需要内存）；根卷 60 GiB gp3。
- 安全组不加任何入站规则，通过 SSM 访问。
- IMDSv2 必需，**hop limit 设为 2**，否则容器内取不到实例角色凭证。

```bash
aws ec2 run-instances --region <REGION> \
  --image-id $(aws ssm get-parameter --region <REGION> --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 --query Parameter.Value --output text) \
  --instance-type m7i.xlarge --subnet-id <SUBNET_ID> --security-group-ids <SG_ID> \
  --iam-instance-profile Name=<EC2_INSTANCE_PROFILE> \
  --metadata-options HttpTokens=required,HttpPutResponseHopLimit=2 \
  --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=60,VolumeType=gp3}' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=sqlbot-agentcore}]'
```

实例上安装 Docker 和 git：

```bash
sudo dnf install -y docker git && sudo systemctl enable --now docker
```

## 4. 构建镜像

根目录 `Dockerfile` 的基础镜像位于 `registry.cn-qingdao.aliyuncs.com`，海外区域通常无法访问。可改用 Docker Hub 上的官方镜像作为基础，叠加本仓库代码和 ADOT（官方镜像版本需与代码版本一致）：

```bash
git clone --branch <BRANCH> <REPO_URL> sqlbot && cd sqlbot
cat > Dockerfile.agentcore <<'EOF'
FROM dataease/sqlbot:latest
COPY backend/ /opt/sqlbot/app/
COPY start.sh /opt/sqlbot/app/start.sh
RUN cd /opt/sqlbot/app && uv pip install --python .venv/bin/python "aws-opentelemetry-distro>=0.18.0,<1.0.0"
EOF
sudo docker build -f Dockerfile.agentcore -t sqlbot:agentcore .
```

能访问原基础镜像时，直接 `docker build .` 即可（`pyproject.toml` 已包含 ADOT 依赖）。

## 5. 运行

环境变量文件 `sqlbot-agentcore.env`：

```bash
SECRET_KEY=<随机字符串>
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
AWS_REGION=<REGION>
AWS_DEFAULT_REGION=<REGION>
```

- `OTEL_LOGS_EXPORTER=none`：不导出应用日志（日志含完整提示词）；评估所需内容已在 span 上。
- 禁用数据库/HTTP 客户端插桩，避免 SQL 语句和 HTTP 细节进入 span。

```bash
mkdir -p data/postgresql data/sqlbot/{excel,file,images,logs}
sudo docker run -d --name sqlbot --restart always --env-file sqlbot-agentcore.env \
  -p 127.0.0.1:8000:8000 -p 127.0.0.1:8001:8001 \
  -v $PWD/data/postgresql:/var/lib/postgresql/data \
  -v $PWD/data/sqlbot/excel:/opt/sqlbot/data/excel \
  -v $PWD/data/sqlbot/file:/opt/sqlbot/data/file \
  -v $PWD/data/sqlbot/images:/opt/sqlbot/images \
  -v $PWD/data/sqlbot/logs:/opt/sqlbot/app/logs \
  sqlbot:agentcore
```

PostgreSQL 数据目录是镜像声明的 volume，务必挂载到宿主机：否则重建容器（包括 `docker commit` 后重跑）会得到空库，chat ID 从 1 重新计数，与历史 `session.id` 冲突。

本地访问 UI（默认账号 `admin` / `SQLBot@123456`，首次登录后修改）：

```bash
aws ssm start-session --region <REGION> --target <INSTANCE_ID> \
  --document-name AWS-StartPortForwardingSession --parameters portNumber=8000,localPortNumber=8000
# 浏览器打开 http://localhost:8000
```

## 6. 配置模型和数据源

在 SQLBot「系统管理 → AI 模型」新增模型：

| 字段 | 值 |
| --- | --- |
| 供应商 | 通用 OpenAI |
| 基础模型 | `qwen.qwen3-235b-a22b-2507-v1:0`（或区域内其他支持 Chat Completions 的模型） |
| API 域名 | `https://bedrock-runtime.<REGION>.amazonaws.com/openai/v1` |
| API Key | Bedrock API key |

Bedrock API key 可在实例上用实例角色生成短期 key（最长 12 小时，过期后需更新模型配置）：

```bash
sudo docker exec sqlbot sh -c 'cd /opt/sqlbot/app && uv pip install -q --python .venv/bin/python aws-bedrock-token-generator'
sudo docker exec sqlbot /opt/sqlbot/app/.venv/bin/python -c \
  "from aws_bedrock_token_generator import provide_token; print(provide_token(region='<REGION>'))"
```

长期使用请改用 Bedrock 长期 API key 并存放在 Secrets Manager，或等待 SQLBot 原生 Bedrock 模型支持。

然后在「数据源」中添加测试数据源并在会话中提问。

## 7. 验证 Observability

CloudWatch → GenAI Observability 中查看服务 `sqlbot` 的会话与 trace；或用 Logs Insights 查询 `aws/spans`：

```
fields @timestamp, name, `attributes.session.id`, traceId
| filter `resource.attributes.service.name` = "sqlbot" and ispresent(`attributes.gen_ai.operation.name`)
| sort startTimeUnixNano asc
| limit 100
```

每次提问应看到 1 个 `invoke_agent sqlbot`、若干 `chat <model>` 和 1 个 `execute_tool execute_sql`（模型拒绝生成 SQL 时没有 `execute_sql`，根 span 状态为 ERROR）。

## 8. 评估

### 按需评估

```bash
pip install bedrock-agentcore
```

```python
from bedrock_agentcore.evaluation import EvaluationClient

client = EvaluationClient(region_name="<REGION>")
results = client.run(
    evaluator_ids=["Builtin.Correctness", "Builtin.Helpfulness",
                   "Builtin.GoalSuccessRate", "Builtin.ToolParameterAccuracy"],
    session_id="sqlbot-<chat_id>",
    log_group_name="/aws/bedrock-agentcore/runtimes/sqlbot",
)
for r in results:
    print(r["evaluatorId"], r.get("value"), r.get("label"))
```

可用评估器以 `client.list_evaluators()` 为准。SQLBot 的回答属性是「SQL + 结果 JSON」，`Builtin.Helpfulness` 会因缺少文字结论偏低（约 0.67）。

### 在线评估

```python
import boto3

cp = boto3.client("bedrock-agentcore-control", region_name="<REGION>")
cp.create_online_evaluation_config(
    onlineEvaluationConfigName="sqlbot_online",
    rule={"samplingConfig": {"samplingPercentage": 10}, "sessionConfig": {"sessionTimeoutMinutes": 15}},
    dataSourceConfig={"cloudWatchLogs": {"logGroupNames": ["/aws/bedrock-agentcore/runtimes/sqlbot"],
                                         "serviceNames": ["sqlbot"]}},
    evaluators=[{"evaluatorId": e} for e in ("Builtin.Correctness", "Builtin.Helpfulness",
                                              "Builtin.GoalSuccessRate", "Builtin.ToolParameterAccuracy")],
    evaluationExecutionRoleArn="arn:aws:iam::<ACCOUNT_ID>:role/<EVAL_ROLE>",
    enableOnCreate=True,
)
```

会话最后一个 span 之后超过 `sessionTimeoutMinutes` 才会被评估，结果出现在 `/aws/bedrock-agentcore/evaluations/results/<config-id>`（验证时约在会话结束后 10 分钟内），分数同时写入 `Bedrock-AgentCore/Evaluations` 指标，可配置告警。演示可把采样设为 100%、超时设为 5 分钟；采样越高，评审模型调用费用越高。

## 9. 已知限制

- `session.id` 取自数据库自增 chat ID，多套部署共用同一账号或数据库被重置时会冲突；共用评估数据源前需加区分前缀。
- 只覆盖问数主流程；分析、预测、推荐问题等异步任务未埋点。
- 尚无按工作空间/数据源控制是否上报的开关。

## 10. 清理

按创建的反序删除：在线评估配置 → EC2 实例 → 安全组 → 实例配置文件与两个 IAM 角色 → `/aws/bedrock-agentcore/runtimes/sqlbot` 与评估结果 log group。Transaction Search 是账号级设置，确认无其他服务使用后再关闭（`aws xray update-trace-segment-destination --destination XRay`）。
