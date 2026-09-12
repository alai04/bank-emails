# bank-emails

后台常驻的 Python daemon：定期从 Microsoft 365 邮箱读取邮件，用 LLM 判断是否为券商/银行的
股票交易确认单并抽取交易数据，再通过 `odoorpc` 写入 Odoo；同时提供 REST API 供查询运行状态、
统计与单封邮件的处理结果。

设计与字段口径见 [docs/product-design.md](docs/product-design.md)。

当前版本 v0.1.0（里程碑 M0）。

## 当前进度

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| M0 | 配置管理、数据库表结构、REST API 骨架 | 已完成 |
| M1 | M365 认证、增量拉取、附件下载与解析 | 待开发 |
| M2 | LLM 分类与抽取、校验、置信度 | 待开发 |
| M3 | Odoo 推送（幂等 upsert） | 待开发 |
| M4 | 人工复核、告警、部署脚本 | 待开发 |

因此当前版本的 REST API 读写能力如下：查询类接口可用；`重新处理 / 推送 / 手动拉取` 等
依赖后台流水线的接口已固定路径，但返回 `501 NOT_IMPLEMENTED`。

## 环境要求

- Python ≥ 3.10（仓库用 [uv](https://docs.astral.sh/uv/) 管理虚拟环境与依赖）
- 一个可读取的 Microsoft 365 邮箱（应用认证或委派认证）
- 一个 OpenAI 兼容的 LLM 端点
- 一个可写入的 Odoo 服务

## 快速开始

```bash
# 1. 安装依赖（会创建 .venv 并安装项目本身）
uv sync

# 2. 生成配置文件并填写真实值（.env 不会入库）
cp .env.example .env
$EDITOR .env

# 3. 部署前自检：校验配置 + 初始化数据库，成功退出码 0
uv run bank-emails --check-config

# 4. 启动服务（前台运行，Ctrl+C 停止）
uv run bank-emails
```

常用变体：

```bash
uv run bank-emails --env-file /etc/bank-emails/.env   # 指定配置文件
uv run bank-emails --check-config                     # 只做配置与建库自检
```

配置缺失或写错时进程会以退出码 2 结束，并逐条打印出错的环境变量名，例如：

```text
配置错误：
配置校验失败：
- LLM_API_KEY: Field required
- API_PORT: Input should be less than or equal to 65535
```

## 配置

全部变量、取值范围与含义见 [.env.example](.env.example)（可复制模板）与
[docs/product-design.md §12](docs/product-design.md)。几点需要注意：

- `M365_CLIENT_SECRET` 与 `M365_REFRESH_TOKEN` **只能设置一个**，daemon 默认使用应用认证。
- `MAIL_POLL_INTERVAL` 最小 30 秒；`MAIL_LOOKBACK_DAYS` 只影响首次启动的回溯窗口。
- `API_TOKEN` 是 REST API 的 Bearer 令牌，请使用足够随机的字符串。
- `DB_PATH`、`DATA_DIR`、`LOG_DIR` 会在启动时自动创建。
- 环境变量优先于 `.env` 文件，便于容器部署时覆盖。

## REST API

除探针外，所有接口都需要在请求头带上 `Authorization: Bearer <API_TOKEN>`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 存活探针，进程可响应即 200，免鉴权 |
| GET | `/readyz` | 就绪探针：数据库可查、可写、表结构齐全，免鉴权 |
| GET | `/api/v1/status` | 运行状态：水位线、最近成功/失败时间、连续失败次数、队列深度、各状态分布 |
| GET | `/api/v1/stats` | 区间统计（`from`/`to`）：邮件与确认单数量、状态分布、按机构分布、净额合计、LLM token |
| GET | `/api/v1/config` | 当前生效配置，密钥仅显示 `***` |
| GET | `/api/v1/emails` | 邮件列表，支持 `status`、`sender`、`from`、`to`、`page`、`page_size` |
| GET | `/api/v1/emails/{email_id}` | 邮件详情：处理轨迹、附件清单、抽取出的交易 |
| GET | `/api/v1/emails/{email_id}/attachments/{attachment_id}` | 单个附件的信息与解析文本 |
| POST | `/api/v1/emails/{email_id}/reprocess` | 重新处理某封邮件（501，待流水线接入） |
| GET | `/api/v1/transactions` | 交易列表，支持 `push_status`、`symbol`、`issuer_name`、`email_id`、`from`、`to` |
| GET | `/api/v1/transactions/{transaction_id}` | 单笔交易详情 |
| POST | `/api/v1/transactions/{transaction_id}/push` | 手动重试推送（501，待 Odoo 客户端接入） |
| GET | `/api/v1/review` | 人工复核队列（邮件级失败 + 交易级推送失败） |
| POST | `/api/v1/review/{item_id}` | 提交人工修正（501，待流水线接入） |
| POST | `/api/v1/jobs/fetch` | 手动触发一次拉取（501，待邮件采集接入） |

调用示例：

```bash
curl -s http://127.0.0.1:8080/healthz
curl -s -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8080/api/v1/status
curl -s -H "Authorization: Bearer $API_TOKEN" \
  "http://127.0.0.1:8080/api/v1/transactions?push_status=FAILED&page_size=20"
```

响应约定：

- 列表接口统一返回 `{"items": [...], "page": 1, "page_size": 50, "total": 123}`。
- 出错统一返回 `{"error": {"code": "EMAIL_NOT_FOUND", "message": "...", "trace_id": "..."}}`。
- 每个响应都带 `X-Trace-Id` 头，排查问题时可直接与日志对齐。
- 金额字段为字符串化的数字（`REAL` 列），币种以设计文档 §9 的口径处理。

## 数据库

SQLite（开启 WAL，支持 API 与后台流水线并发读写），默认路径 `./data/bank_emails.db`。

| 表 | 用途 |
| --- | --- |
| `emails` | 邮件主表，含处理状态与水位线所需的接收时间、唯一键 `(mailbox, internet_message_id)` |
| `attachments` | 附件元数据与解析后的文本 |
| `transactions` | 交易明细，18 个交易字段 + 单据属性 + 推送状态 |
| `email_events` | 状态轨迹，可回溯每封邮件经过了哪些阶段 |
| `llm_calls` | LLM 调用审计与 token 消耗 |
| `runtime_state` | 水位线、最近成功时间等运行态键值 |

表结构由 `bank_emails.db.init_db()` 在启动时创建，可重复执行；表结构与设计文档 §7 的
差异由测试 `tests/test_db.py` 逐列把关。

## 测试

```bash
uv run pytest              # 全部测试
uv run pytest tests/test_api.py -v
```

测试不依赖网络与真实凭据：配置从环境变量注入，数据库使用临时目录，邮件/交易数据由夹具直接写入。
`tests/test_db.py` 还会解析设计文档里的 SQL 代码块，确保代码与文档的表结构一致。

## 目录结构

```text
src/bank_emails/
  __init__.py   入口：加载配置 → 建库 → 启动 API（含 --check-config）
  config.py     配置模型、校验与脱敏
  db.py         建表 SQL、SQLite 连接与 PRAGMA
  store.py      数据访问层（SQL 只出现在这里）
  api.py        FastAPI 应用与路由
tests/          pytest 测试
docs/           产品设计文档与样本（docs/examples 为真实单据，不入库）
```

## 文档

- [docs/product-design.md](docs/product-design.md)：产品设计文档（架构、字段口径、校验规则、里程碑）
- [AGENTS.md](AGENTS.md)：本仓库的协作约定
