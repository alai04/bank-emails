# 银行/券商交易确认单邮件处理 Daemon — 产品设计文档

| 项目 | 内容 |
| --- | --- |
| 文档版本 | v0.1（草案） |
| 状态 | 待评审 |
| 日期 | 2026-09-12 |
| 关联仓库 | `bank-emails` |
| 关联文档 | [README.md](../README.md)、[AGENTS.md](../AGENTS.md) |

---

## 1. 概述

### 1.1 背景

券商与银行在每笔股票/证券交易完成后会通过邮件发送"交易确认单"（成交回报、月结单、交割单等）。这些邮件格式各异（纯文本、HTML、PDF 附件、扫描件图片、Excel/CSV 附件），人工逐封录入 Odoo 重复且易错。

本产品是一个常驻后台的 Python daemon：周期性地从 Microsoft 365 邮箱拉取邮件，用 LLM 判断是否为交易确认单并抽取结构化交易数据，再通过 `odoorpc` 写入指定的 Odoo 服务器；同时对外提供 REST API，用于查看运行状态、处理统计与单封邮件的处理结果。

### 1.2 目标

| 编号 | 目标 | 可量化验收指标 |
| --- | --- | --- |
| G1 | 无人值守地把交易确认单转为 Odoo 数据 | 连续运行 7×24 无人工干预；单封邮件端到端 P95 < 60s |
| G2 | 分类与抽取可靠 | 在 Golden Set（≥100 封真实样本）上：确认单分类准确率 ≥ 99%、关键字段（代码/方向/数量/价格/金额/日期）抽取准确率 ≥ 98% |
| G3 | 幂等、可追溯 | 同一封邮件重复处理不会在 Odoo 产生重复记录；每笔交易可回溯到原始邮件与原始附件 |
| G4 | 状态可观测 | 通过 REST API 可查运行状态、统计与任意邮件的完整处理轨迹（含失败原因） |
| G5 | 出错不丢数据 | LLM/Odoo/网络故障时进入重试或人工复核队列，不静默丢弃 |

### 1.3 非目标（本期不做）

- 不做 Odoo 端的会计凭证生成、对账与损益计算（仅推送原始交易确认数据）。
- 不做 Web 图形界面（交互仅通过 REST API / CLI 提供）。
- 不做多租户（本期是单实例、单邮箱、单 Odoo 库）。
- 不做订单下单/交易指令回传（严格只读邮件、只写 Odoo）。
- 不做历史全量邮件回溯（仅最近一段时间 + 增量水位线）。

---

## 2. 术语

| 术语 | 含义 |
| --- | --- |
| 确认单（Confirmation） | 券商/银行发出的、包含一笔或多笔已成交交易明细的邮件或附件 |
| 水位线（Watermark） | 上次成功拉取到的最大邮件接收时间，用于增量拉取 |
| 交易（Transaction） | 确认单中的一条成交记录；一封邮件可含 0..N 笔 |
| 抽取（Extraction） | LLM 从正文/附件中输出符合 JSON Schema 的结构化交易数据 |
| 推送（Push） | 通过 `odoorpc` 将交易写入 Odoo |
| 人工复核队列（Review Queue） | 低置信度或多次失败、需要人介入的记录集合 |
| Golden Set | 带人工标注的邮件样本集，用于回归评测准确率 |

---

## 3. 用户与使用场景

**主要用户**：部署并运维该 daemon 的个人投资者 / 小型团队运维者。

核心场景：

1. **常规处理**：用户收到券商成交回报邮件 → daemon 在下一轮轮询（默认 5 分钟）内识别并写入 Odoo。
2. **状态检查**：用户或监控系统调用 API 确认进程存活、最近一轮拉取时间、累计处理数量。
3. **异常排查**：用户发现某笔交易未入 Odoo，通过 API 查询该邮件的处理状态与错误原因，修复后触发重新处理。
4. **配置调整**：用户修改 `.env` 中的轮询间隔、回溯窗口、LLM 模型等，重启或热加载配置生效。
5. **误判纠正**：用户发现非确认单邮件被误抽取（或确认单被判为噪音），通过 API 覆写分类结果并重新处理。

---

## 4. 总体架构

```
                        ┌────────────────────────────────────────────────┐
                        │             daemon 进程（单进程）               │
                        │                                                │
  Microsoft 365         │  ┌────────────┐      ┌───────────────────┐      │
  (Graph API)  ────────▶│  │ Mail       │─────▶│ Pipeline          │      │
  邮件 / 附件           │  │ Fetcher    │      │ Orchestrator      │      │
                        │  │ (轮询/水位) │      │ (状态机/重试)      │      │
                        │  └────────────┘      └───┬───────────┬───┘      │
                        │                          │           │          │
                        │        ┌─────────────────┘           └───────┐  │
                        │        ▼                                     ▼  │
                        │  ┌────────────┐   ┌────────────┐   ┌────────────┐
                        │  │ Parser     │──▶│ LLM Client │──▶│ Odoo       │
                        │  │ 正文/附件   │   │ 分类+抽取   │   │ Pusher     │
                        │  └────────────┘   └────────────┘   └────────────┘
                        │        │              │                │
                        │        └──────────────┴────────────────┘
                        │                       ▼
                        │            ┌────────────────────┐
                        │            │ SQLite (WAL)       │
                        │            │ 邮件/交易/日志/统计 │
                        │            └────────────────────┘
                        │                       ▲
                        │            ┌──────────┴─────────┐
                        └────────────│ FastAPI (REST API) │─────────────────┘
                          HTTP/JSON  └────────────────────┘
                                      状态 / 统计 / 邮件 / 交易 / 重处理
```

### 4.1 模块职责

| 模块 | 职责 | 关键实现 |
| --- | --- | --- |
| Config | 加载与校验配置，敏感项脱敏 | `pydantic-settings` + `.env` |
| Mail Fetcher | 增量拉取邮件与附件，持久化原文 | `O365`（python-o365）或 Graph SDK |
| Parser | MIME 解析、HTML→文本、附件文本化 | `beautifulsoup4` / `lxml` / `pdfplumber` / `openpyxl` |
| LLM Client | 分类 + 结构化抽取，重试与成本统计 | OpenAI 兼容 HTTP API + JSON Schema |
| Validator | 业务规则与交叉校验，判定置信度 | 纯函数，易测试 |
| Odoo Pusher | 映射字段并 upsert 到 Odoo | `odoorpc`（同步，放线程池执行） |
| Store | 所有持久化（邮件、附件、交易、状态、统计） | SQLite（WAL）+ 轻量 DAO |
| Scheduler | 定时触发拉取与分析，互斥与优雅退出 | `asyncio` 循环 + 单实例文件锁 |
| REST API | 状态、统计、明细查询与人工干预 | `FastAPI` + `uvicorn` |
| Observability | 结构化日志、指标、告警事件 | `structlog` + 日志轮转 |

### 4.2 技术选型

| 层次 | 选型 | 说明 |
| --- | --- | --- |
| 运行时 | Python ≥ 3.10（仓库要求），包管理用 `uv` | 与 [pyproject.toml](../pyproject.toml) 一致 |
| 并发模型 | 单进程 `asyncio`；同步阻塞调用（odoorpc、PDF 解析）放 `asyncio.to_thread` | 避免多进程争抢 SQLite 与内存状态 |
| HTTP 框架 | `FastAPI` + `uvicorn` | 与主循环同进程、共享状态 |
| 数据库 | SQLite（WAL、`busy_timeout`） | 单机轻量；后续可平滑迁移到 PostgreSQL |
| 邮件 | `O365`（python-o365）或 `msgraph-sdk` | 二选一，以库当前版本文档为准 |
| 抽取 | OpenAI 兼容接口（可指向 Azure OpenAI / 自建网关） | 模型可配置，便于替换与降本 |
| Odoo | `odoorpc` | 通过 JSON-RPC 调用 `create`/`write`/`search_read` |

---

## 5. 功能需求

### FR-1 配置管理

从 `.env` 读取全部运行参数（清单见 §12）。启动时做完整校验，缺项或格式错误立即退出并打印明确原因。日志中禁止出现明文凭据。

**验收**：删去必填项启动 → 退出码非 0，且错误信息指明具体变量名。

### FR-2 邮件增量拉取

- 首次启动回溯 `MAIL_LOOKBACK_DAYS` 天；之后基于水位线增量拉取。
- 每轮拉取使用重叠窗口（`receivedDateTime >= watermark - 5min`）避免边界丢信，靠唯一键去重。
- 支持限定邮件文件夹（如收件箱下的"交易确认单"子目录）与发件人白名单/黑名单。
- 拉取邮件正文、`internetMessageId`、发件人、接收时间，以及全部附件元数据与内容。

**验收**：同一封邮件被重复拉取两次，数据库只有一条记录，且不产生重复 Odoo 交易。

### FR-3 附件与正文解析

- 正文：优先 `text/plain`；仅有 HTML 时转纯文本并保留表格结构（表格还原为对齐文本或 Markdown，避免列错位）。
- 支持附件类型：PDF（含文本层）、图片（PNG/JPEG，走 LLM 视觉）、XLSX/CSV、DOCX。
- 单个附件超过 `MAX_ATTACHMENT_MB` 时跳过并记录原因，不阻塞整封邮件。

**验收**：对样本集中每种格式各 ≥10 个文件，解析输出无异常、无乱码。

### FR-4 是否交易确认单的分类

LLM 输出 `is_confirmation`（bool）、`issuer_type`（券商/银行/其他）、`issuer_name`、`confidence`、`reason`。非确认单直接标记为 `SKIPPED` 并结束，不调用抽取、不写 Odoo。

**验收**：Golden Set 中广告、账单、验证码、营销邮件等噪音全部被正确跳过（目标 ≥ 99%）。

### FR-5 交易数据抽取

对判定为确认单的邮件，按 §9 的字段清单与 §8.3 的 JSON Schema 抽取 1..N 笔交易。多笔交易、多币种、同日多笔需正确拆分。

**验收**：Golden Set 关键字段准确率 ≥ 98%；无法确定的字段必须返回 `null` 而不是猜测。

### FR-6 校验与置信度

执行 §9.3 的规则校验。校验通过且 `confidence ≥ LLM_CONFIDENCE_THRESHOLD` → 进入推送；否则进入人工复核队列（`NEEDS_REVIEW`），并保留失败原因。

**验收**：构造"数量×价格≠金额"的样本，系统必定拦截并进入复核，而不是推送脏数据。

### FR-7 推送到 Odoo

- 目标模型与字段映射由配置文件（`mapping.yaml`）驱动，不硬编码。
- 以 `external_ref`（见 §10.2）做幂等键：先 `search_read`，命中则 `write`，未命中则 `create`。
- 每笔交易独立事务与独立状态；部分成功不影响其余交易。

**验收**：同一笔交易连续推送 3 次，Odoo 中始终只有 1 条记录。

### FR-8 失败重试与人工复核

- 可重试错误（网络超时、HTTP 429/5xx、Odoo 临时不可用）按指数退避重试，上限 `MAX_RETRY`。
- 不可重试错误（LLM 多次输出不符合 Schema、必填字段缺失、Odoo 拒绝的字段错误）直接进入 `NEEDS_REVIEW`。
- 复核队列可通过 API 列出，并支持"修正字段后重新推送"。

**验收**：LLM 端点断开时，邮件停留在待重试状态；恢复后自动继续，无需重启进程。

### FR-9 REST API 状态与统计

见 §11。至少包含：存活/就绪探针、运行状态、处理统计、邮件与交易明细、重新处理与重试入口。

### FR-10 运行控制

- 单实例保护：启动时获取锁文件，重复启动直接退出并提示。
- 优雅关闭：收到 `SIGTERM`/`SIGINT` 后停止拉取、等待进行中的任务（超时则记录并退出）。
- 支持手动触发一次拉取（API 或 CLI），与定时任务互斥。

### FR-11 可观测性

- 结构化 JSON 日志，字段含 `trace_id`、`mail_id`、`stage`、`duration_ms`。
- 关键指标：最近成功拉取时间、队列深度、各状态计数、LLM token 与费用、Odoo 成功率。
- 连续失败（如连续 3 轮拉取失败、或单封邮件重试耗尽）产生明确告警日志。

### FR-12 数据留存

默认保留邮件正文与附件解析文本，便于回溯与重跑；可通过 `RETAIN_RAW_DAYS` 控制清理。可选：不落盘原文，仅保存哈希与抽取结果。

---

## 6. 处理流程与状态机

### 6.1 主流程

```
启动 → 载入配置 → 初始化 DB → 获取单实例锁 → 启动 API → 进入轮询循环

每轮：
  1. 拉取增量邮件（水位线 → 新邮件列表）
  2. 新邮件入库（唯一键冲突则跳过）
  3. 取待处理队列（PENDING / 重试到期）
  4. 逐封处理：
       解析 → 分类 → 非确认单 → SKIPPED 结束
                    └ 确认单 → 抽取 → 校验 → 不通过 → NEEDS_REVIEW
                                        └ 通过 → 生成交易记录 → 推送 Odoo
  5. 更新水位线与统计
  6. 睡眠 MAIL_POLL_INTERVAL
```

### 6.2 邮件状态机

```
PENDING ──▶ FETCHED ──▶ PARSED ──▶ CLASSIFIED ──┬─▶ SKIPPED（非确认单，终态）
                                                │
                                                └─▶ EXTRACTED ──▶ VALIDATED ──┬─▶ PUSHED（终态）
                                                                              ├─▶ FAILED（可重试）
                                                                              └─▶ NEEDS_REVIEW（需人工）
```

任何阶段出现可重试错误 → 停留在该阶段并记录 `retry_count` 与 `next_retry_at`；超过上限 → `NEEDS_REVIEW`。状态变更全部写入 `email_events` 表，形成完整轨迹。

### 6.3 交易状态机

`PENDING → VALIDATED → PUSHED | FAILED → NEEDS_REVIEW`，与邮件状态独立，支持一封邮件部分成功。

### 6.4 幂等与并发

- 邮件唯一键：`mailbox + internetMessageId`（理由见 §8.6）。
- 交易唯一键：`external_ref`（§10.2）。
- 工作队列单消费者串行处理，天然避免同一封邮件被并发处理；Odoo 写入前再做一次 `search` 兜底。

---

## 7. 数据模型（SQLite）

```sql
-- 邮件主表
CREATE TABLE emails (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    mailbox             TEXT    NOT NULL,            -- 邮箱标识
    internet_message_id TEXT    NOT NULL,            -- Graph internetMessageId
    graph_id            TEXT,                        -- Graph 消息 id（可变，仅调试用）
    subject             TEXT,
    sender_address      TEXT,
    sender_name         TEXT,
    received_at         TEXT    NOT NULL,            -- ISO8601 UTC
    folder              TEXT,
    has_attachments     INTEGER NOT NULL DEFAULT 0,
    body_text           TEXT,                        -- 解析后的纯文本（可裁剪）
    body_sha256         TEXT,
    status              TEXT    NOT NULL DEFAULT 'PENDING',
    retry_count         INTEGER NOT NULL DEFAULT 0,
    next_retry_at       TEXT,
    last_error          TEXT,
    is_confirmation     INTEGER,                     -- 分类结果：1/0/NULL
    issuer_name         TEXT,
    issuer_type         TEXT,                        -- broker | bank | other
    llm_confidence      REAL,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    UNIQUE (mailbox, internet_message_id)
);

-- 附件
CREATE TABLE attachments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id       INTEGER NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    filename       TEXT,
    content_type   TEXT,
    size_bytes     INTEGER,
    sha256         TEXT,
    storage_path   TEXT,                             -- 落盘路径，可为空
    extracted_text TEXT,                             -- 解析后的可读文本
    parse_status   TEXT NOT NULL DEFAULT 'PENDING',
    parse_error    TEXT
);

-- 交易明细（一封邮件可多笔）
CREATE TABLE transactions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id          INTEGER NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    seq               INTEGER NOT NULL,              -- 邮件内序号，从 1 开始
    trade_date        TEXT,
    settle_date       TEXT,
    account_no        TEXT,
    symbol            TEXT,
    symbol_name       TEXT,
    market            TEXT,
    side              TEXT,                          -- BUY | SELL | OTHER
    quantity          REAL,
    price             REAL,
    gross_amount      REAL,
    fee               REAL,
    tax               REAL,
    net_amount        REAL,
    currency          TEXT,
    order_no          TEXT,
    source_type       TEXT,                          -- body | attachment
    source_ref        TEXT,                          -- 附件名或正文片段定位
    confidence        REAL,
    valid             INTEGER NOT NULL DEFAULT 0,
    validation_errors TEXT,                          -- JSON 数组
    external_ref      TEXT NOT NULL,
    odoo_model        TEXT,
    odoo_id           INTEGER,
    push_status       TEXT NOT NULL DEFAULT 'PENDING',
    push_error        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (email_id, seq)
);

CREATE UNIQUE INDEX idx_tx_external_ref ON transactions(external_ref);

-- 状态轨迹
CREATE TABLE email_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id    INTEGER NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    stage       TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT,
    message     TEXT,
    created_at  TEXT NOT NULL
);

-- LLM 调用审计与成本
CREATE TABLE llm_calls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id          INTEGER REFERENCES emails(id) ON DELETE SET NULL,
    purpose           TEXT NOT NULL,                 -- classify | extract
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    latency_ms        INTEGER,
    success           INTEGER NOT NULL,
    error             TEXT,
    created_at        TEXT NOT NULL
);

-- 运行态与水位线
CREATE TABLE runtime_state (
    key        TEXT PRIMARY KEY,                     -- e.g. watermark, last_run_at
    value      TEXT,
    updated_at TEXT NOT NULL
);
```

索引建议：`emails(status, next_retry_at)`、`emails(received_at)`、`transactions(push_status)`、`email_events(email_id)`。SQLite 开启 `journal_mode=WAL`、`synchronous=NORMAL`、`busy_timeout=5000`。

---

## 8. LLM 设计

### 8.1 两阶段调用

1. **分类阶段**：输入发件人 + 主题 + 正文（截断）+ 附件名列表（必要时附首张图片），输出是否为交易确认单及机构信息。成本低，可用小模型。
2. **抽取阶段**：仅对确认单执行，输入正文与附件文本（长文按段落或按附件拆分），输出交易数组。

两阶段分离的好处：噪音邮件不触发昂贵的抽取；分类与抽取可用不同模型（`LLM_MODEL_CLASSIFY` / `LLM_MODEL_EXTRACT`）。

### 8.2 提示词管理

提示词以文件形式存放（`prompts/classify.md`、`prompts/extract.md`），带版本号并在 `llm_calls` 中记录版本，便于 A/B 与回归。提示词需明确：

- 角色设定：金融单据信息抽取助手。
- 只依据给定内容作答，缺失字段填 `null`，禁止猜测。
- 明确币种、数量、价格的语义（区分成交价/均价、应付金额/成交金额）。
- 输出必须严格符合 JSON Schema，不得输出解释性文字。
- 附少量 few-shot 示例（覆盖券商/银行、买卖、含费、多笔成交）。

### 8.3 输出 JSON Schema（抽取阶段）

```json
{
  "type": "object",
  "required": ["transactions"],
  "properties": {
    "issuer_name": { "type": ["string", "null"] },
    "issuer_type": { "type": ["string", "null"], "enum": ["broker", "bank", "other", null] },
    "account_no": { "type": ["string", "null"] },
    "transactions": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": ["symbol", "side", "quantity", "price", "trade_date", "currency", "confidence"],
        "properties": {
          "trade_date":   { "type": ["string", "null"], "description": "YYYY-MM-DD" },
          "settle_date":  { "type": ["string", "null"] },
          "symbol":       { "type": ["string", "null"] },
          "symbol_name":  { "type": ["string", "null"] },
          "market":       { "type": ["string", "null"] },
          "side":         { "type": "string", "enum": ["BUY", "SELL", "OTHER"] },
          "quantity":     { "type": ["number", "null"] },
          "price":        { "type": ["number", "null"] },
          "gross_amount": { "type": ["number", "null"] },
          "fee":          { "type": ["number", "null"] },
          "tax":          { "type": ["number", "null"] },
          "net_amount":   { "type": ["number", "null"] },
          "currency":     { "type": ["string", "null"] },
          "order_no":     { "type": ["string", "null"] },
          "confidence":   { "type": "number", "minimum": 0, "maximum": 1 }
        }
      }
    }
  }
}
```

调用时优先使用供应商的结构化输出能力（如 `response_format: json_schema` 或 function calling）；若供应商不支持，则退化为"JSON 模式 + 本地 Schema 校验 + 一次修复重试"。

### 8.4 附件处理策略

| 类型 | 处理方式 |
| --- | --- |
| PDF（含文本层） | `pdfplumber` 抽取文本与表格；表格按行列还原为文本 |
| PDF（扫描件） | 渲染为图片后走 LLM 视觉输入（需多模态模型） |
| 图片 | 直接作为多模态输入；必要时先做裁剪/去噪 |
| XLSX/CSV | `openpyxl` / `csv` 读取为表格文本 |
| DOCX | `python-docx` 提取段落与表格 |
| 其他/加密/超大 | 记录原因并跳过；邮件标记为 `NEEDS_REVIEW`（避免静默丢单） |

正文与多附件的合并顺序：正文优先，其次按附件名自然排序。若同一笔交易在正文与附件重复出现，抽取阶段要求去重，并在校验阶段按 `symbol + trade_date + side + quantity + price` 做二次去重。

### 8.5 可靠性与成本

- `temperature = 0`（或供应商等价的最确定性设置）。
- 超时（默认 60s）+ 重试（默认 2 次，指数退避）。
- 上下文长度守卫：超长正文按段落截断并在提示中标注"内容已截断"，同时记录告警。
- 逐封邮件记录 token 消耗与估算费用，纳入统计 API。

### 8.6 邮件唯一标识的取舍

Microsoft Graph 的消息 `id` 在邮件被移动/复制后可能变化，不适合做长期幂等键。因此：

- 首选 `internetMessageId`（RFC 5322 头，跨服务器稳定）与邮箱地址组合；或请求 Graph 返回 **immutable id**（`Prefer: IdType="ImmutableId"`）。
- 若确认单本身带唯一参考号（如成交编号/合同号），将该编号加入交易级幂等键，进一步防止"同一笔交易由两封邮件分别通知"造成重复入账。

---

## 9. 交易数据字典与校验规则

### 9.1 字段清单

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| trade_date | date | 是 | 成交日期 |
| settle_date | date | 否 | 交割日期 |
| account_no | string | 否 | 证券/银行账号（**脱敏存储与展示**） |
| symbol | string | 是 | 股票代码 |
| symbol_name | string | 否 | 股票名称 |
| market | string | 否 | 交易所/市场（如 HKEX、NYSE、SSE） |
| side | enum | 是 | BUY / SELL / OTHER |
| quantity | number | 是 | 成交数量 |
| price | number | 是 | 成交价 |
| gross_amount | number | 否 | 成交金额（未扣费） |
| fee | number | 否 | 佣金等费用合计 |
| tax | number | 否 | 税费（如印花税） |
| net_amount | number | 否 | 结算金额（含费） |
| currency | string | 是 | ISO 4217，如 HKD/USD/CNY |
| order_no | string | 否 | 订单号/成交编号/参考号 |
| issuer_name | string | 否 | 券商或银行名称 |
| confidence | number | 是 | 模型自评置信度 0–1 |

> 说明：README 第 5 条列出的"结果中要包含的数据项"目前为空，本表为基于业务惯例的**建议默认集**，需与最终 Odoo 目标模型对齐后冻结（见 §18 Q1/Q2）。

### 9.2 规范化规则

- 股票代码统一大写、去空格；保留市场后缀（如 `0700.HK`）。
- 日期统一为 ISO `YYYY-MM-DD`（无年份时结合邮件接收时间补全）。
- 数字去千分位与货币符号；负数与括号表示统一转为带符号数。
- 币种映射为 ISO 代码（`HK$→HKD`、`US$→USD`、`RMB/￥→CNY`）。
- 方向映射：`B/买/买入/Buy/Purchase→BUY`，`S/卖/卖出/Sell/Sale→SELL`，其余 `OTHER`。

### 9.3 校验规则

| 编号 | 规则 | 失败处理 |
| --- | --- | --- |
| V1 | `quantity > 0`、`price >= 0` | 复核 |
| V2 | 三者齐全时 `abs(quantity*price - gross_amount) <= max(0.01, 0.5% * gross_amount)` | 复核 |
| V3 | `net_amount` 齐全时 `abs(gross_amount ± fee ± tax - net_amount)` 在容差内 | 复核 |
| V4 | `trade_date` 不早于邮件接收时间 90 天、不晚于接收时间 +1 天 | 复核 |
| V5 | `currency` 在配置的白名单内 | 复核 |
| V6 | `symbol` 非空且符合可配置的代码正则 | 复核 |
| V7 | `side != OTHER` | 复核 |
| V8 | 邮件内与跨附件的重复交易去重后仅保留一笔 | 自动 |
| V9 | `confidence < LLM_CONFIDENCE_THRESHOLD`（默认 0.8） | 复核 |

所有校验实现为无副作用纯函数，便于单元测试，也便于 API 提供"试算/预校验"能力。

---

## 10. Odoo 集成

### 10.1 连接

- 使用 `odoorpc.ODOO(host, port, protocol, timeout)` 登录，凭据来自 `.env`。
- 同步阻塞调用统一放线程池执行（`asyncio.to_thread`），设置连接超时与请求超时。
- 会话失效（`SessionExpiredError` 等）自动重新登录一次，再失败则进入重试。

### 10.2 幂等键

```
external_ref = sha256(f"{issuer_name}|{order_no or ''}|{symbol}|{trade_date}|{side}|{quantity}|{price}")[:32]
```

优先使用券商/银行提供的成交编号（`order_no`）；缺失时退化为业务字段组合哈希。`external_ref` 写入 Odoo 目标的唯一字段（如 `x_external_ref`），推送前先 `search_read` 判定存在性。若同一笔交易由两封不同邮件通知（例如"成交回报" + "月结单"），业务字段一致时会被识别为同一笔，只更新不新增。

### 10.3 字段映射（配置驱动）

`mapping.yaml` 示例：

```yaml
model: x_trade_confirmation
unique_field: x_external_ref
fields:
  x_external_ref: "{{ external_ref }}"
  x_trade_date:   "{{ trade_date }}"
  x_symbol:       "{{ symbol }}"
  x_side:         "{{ side }}"
  x_quantity:     "{{ quantity }}"
  x_price:        "{{ price }}"
  x_net_amount:   "{{ net_amount }}"
  x_currency:     "{{ currency }}"
  x_order_no:     "{{ order_no }}"
  x_source_email: "{{ internet_message_id }}"
defaults:
  x_state: "draft"
```

映射文件可替换，因此从自定义模型切到 `account.move`、`stock.move` 或任何内部模型都不需要改代码。

### 10.4 错误处理

| Odoo 错误 | 处理 |
| --- | --- |
| 连接/超时/5xx | 可重试，指数退避 |
| 权限不足 | 不可重试，`NEEDS_REVIEW` + 告警 |
| 字段校验失败（ValidationError） | 不可重试，记录原始响应，`NEEDS_REVIEW` |
| 唯一约束冲突 | 视为"已存在"，转为 `write` 更新，标记成功 |

---

## 11. REST API

基础路径 `/api/v1`，JSON 响应；鉴权用静态 API Key（`Authorization: Bearer <API_TOKEN>`），`/healthz` 与 `/readyz` 免鉴权。默认仅监听回环地址，可由 `API_HOST` 改成内网地址。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 存活探针，进程可响应即 200 |
| GET | `/readyz` | 就绪探针：DB 可写、配置已加载、最近一轮未超时 |
| GET | `/api/v1/status` | 运行状态：启动时间、下次运行时间、最近成功/失败时间、连续失败次数、队列深度、版本 |
| GET | `/api/v1/stats` | 统计：`from`/`to` 区间内处理总数、按状态分布、按机构分布、成功率、平均耗时、LLM token 与费用 |
| GET | `/api/v1/emails` | 邮件列表，支持 `status`、`sender`、`from`、`to`、`page`、`page_size` |
| GET | `/api/v1/emails/{id}` | 单封邮件详情：元数据、处理轨迹、附件清单、抽取结果、错误信息 |
| GET | `/api/v1/emails/{id}/attachments/{aid}` | 附件元数据与解析文本（可选返回原文） |
| POST | `/api/v1/emails/{id}/reprocess` | 重新处理，可覆写分类：`{"is_confirmation": true}` |
| GET | `/api/v1/transactions` | 交易列表，支持 `push_status`、`symbol`、日期区间、分页 |
| GET | `/api/v1/transactions/{id}` | 单笔交易详情，含校验结果与 Odoo 记录 id |
| POST | `/api/v1/transactions/{id}/push` | 手动重试推送 |
| GET | `/api/v1/review` | 人工复核队列 |
| POST | `/api/v1/review/{id}` | 提交人工修正并触发推送 |
| POST | `/api/v1/jobs/fetch` | 手动触发一次拉取（与定时任务互斥，重复调用返回 409） |
| GET | `/api/v1/config` | 当前生效配置（**脱敏**：密钥仅显示是否已设置） |

`GET /api/v1/status` 响应示例：

```json
{
  "status": "running",
  "started_at": "2026-09-12T01:00:00Z",
  "last_success_at": "2026-09-12T02:35:10Z",
  "last_error": null,
  "consecutive_failures": 0,
  "watermark": "2026-09-12T02:30:00Z",
  "queue": { "pending": 2, "retry": 0, "needs_review": 1 },
  "totals": { "emails": 1280, "confirmations": 342, "transactions": 505, "pushed": 501 }
}
```

约定：统一错误体 `{"error": {"code": "...", "message": "...", "trace_id": "..."}}`；列表接口统一返回 `{"items": [...], "page": 1, "page_size": 50, "total": 123}`；写接口幂等（重复调用返回当前状态而非报错）。

---

## 12. 配置项

| 变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `M365_TENANT_ID` | 是 | — | Azure AD 租户 ID |
| `M365_CLIENT_ID` | 是 | — | 应用（客户端）ID |
| `M365_CLIENT_SECRET` | 二选一 | — | 应用认证方式（daemon 推荐） |
| `M365_REFRESH_TOKEN` | 二选一 | — | 委派认证方式的刷新令牌 |
| `M365_MAILBOX` | 是 | — | 要读取的邮箱地址 |
| `M365_FOLDER` | 否 | `Inbox` | 目标邮件文件夹 |
| `MAIL_LOOKBACK_DAYS` | 否 | `7` | 首次启动回溯天数 |
| `MAIL_POLL_INTERVAL` | 否 | `300` | 轮询间隔（秒） |
| `MAIL_SENDER_FILTER` | 否 | 空 | 发件人域名白名单，逗号分隔 |
| `MAX_ATTACHMENT_MB` | 否 | `20` | 单个附件大小上限 |
| `LLM_BASE_URL` | 是 | — | OpenAI 兼容端点 |
| `LLM_API_KEY` | 是 | — | LLM 密钥 |
| `LLM_MODEL_CLASSIFY` | 否 | `gpt-4o-mini` | 分类模型 |
| `LLM_MODEL_EXTRACT` | 否 | `gpt-4o` | 抽取模型 |
| `LLM_CONFIDENCE_THRESHOLD` | 否 | `0.8` | 低于该值进入复核 |
| `LLM_TIMEOUT` | 否 | `60` | 单次调用超时（秒） |
| `ODOO_URL` / `ODOO_DB` / `ODOO_USER` / `ODOO_PASSWORD` | 是 | — | Odoo 连接信息（密码建议使用 API Key） |
| `ODOO_MAPPING_FILE` | 否 | `mapping.yaml` | 字段映射文件路径 |
| `API_HOST` / `API_PORT` | 否 | `127.0.0.1` / `8080` | REST API 监听地址 |
| `API_TOKEN` | 是 | — | API 鉴权令牌 |
| `DB_PATH` | 否 | `./data/bank_emails.db` | SQLite 路径 |
| `DATA_DIR` | 否 | `./data` | 附件与临时文件目录 |
| `LOG_LEVEL` / `LOG_DIR` | 否 | `INFO` / `./logs` | 日志级别与目录 |
| `TIMEZONE` | 否 | `Asia/Shanghai` | 本地时间展示与 Odoo 时区转换 |
| `MAX_RETRY` | 否 | `3` | 单阶段最大重试次数 |
| `RETAIN_RAW_DAYS` | 否 | `90` | 原文保留天数，`0` 表示永久 |

敏感值禁止出现在日志与 API 响应中；`.env` 必须加入 `.gitignore`（现有文件只包含 `.venv`，需补充 `.env`）。

---

## 13. 非功能需求

| 类别 | 要求 |
| --- | --- |
| 性能 | 单轮 100 封邮件处理 < 5 分钟（并发度可配）；空闲内存 < 300MB |
| 可靠性 | 崩溃后重启自动恢复未完成队列；任何单封邮件故障不影响整体循环 |
| 幂等 | 邮件级与交易级双重幂等，重复执行结果一致 |
| 安全 | TLS 访问 Graph/LLM/Odoo；凭据仅存 `.env`（建议 `chmod 600`）；账号等 PII 在日志与 API 中脱敏；API 强制鉴权 |
| 隐私 | 邮件内容会发送给 LLM 供应商，需在部署文档中明确提示；可选"发送前脱敏账号/证件号"开关 |
| 可维护 | 提示词、字段映射、校验阈值全部配置化，不写死在代码里 |
| 可移植 | 支持 launchd / systemd / Docker 三种部署方式 |
| 合规 | 仅读取与保存交易相关信息；保留期可配置；支持"不落盘原文"模式 |

---

## 14. 部署与运维

- **macOS（当前开发环境）**：`launchd` plist，`KeepAlive=true`，标准输出重定向到日志目录。
- **Linux**：`systemd` unit，`Restart=always`、`RestartSec=10`，`EnvironmentFile=.env`。
- **Docker**：单容器，`data/` 与 `logs/` 挂载为卷；健康检查调用 `/healthz`。

启动方式（示意）：

```bash
uv sync
uv run bank-emails        # 入口由 pyproject 的 [project.scripts] 提供
```

运维要点：日志按天轮转并保留 30 天；数据库定期备份（`VACUUM INTO`）；升级前先停进程；时间同步（NTP）以免水位线错乱。

---

## 15. 可观测性与告警

- **日志**：JSON 行格式，含 `trace_id`（一次处理链）、`stage`、`mail_id`、`duration_ms`、`error_code`。
- **指标**（`/api/v1/stats` 与可选 Prometheus `/metrics`）：`emails_fetched_total`、`emails_skipped_total`、`transactions_extracted_total`、`odoo_push_success_total`、`odoo_push_failure_total`、`llm_tokens_total`、`pipeline_duration_seconds`。
- **告警条件**：连续 3 轮拉取失败；任一邮件重试耗尽；Odoo 连续 5 次推送失败；LLM 费用日环比超阈值；复核队列深度超阈值。

---

## 16. 测试策略

按 [AGENTS.md](../AGENTS.md) 的约定，每次改动都必须带测试且全绿。测试分层：

1. **单元测试**：解析器（HTML→文本、PDF/XLSX 表格）、规范化与校验纯函数、映射模板渲染、幂等键计算、退避策略。
2. **契约测试**：LLM 客户端（Mock HTTP，覆盖合法 JSON、非法 JSON、超时、429）、Odoo 客户端（Mock `odoorpc`，覆盖 create/write/唯一冲突/权限错误）。
3. **集成测试**：SQLite + 状态机 + FastAPI（`TestClient`），覆盖"新邮件→跳过""确认单→推送""校验失败→复核""重试后成功"等路径。
4. **端到端测试**：用固定 fixtures 邮件（`.eml` 样本，含 PDF/图片/CSV 附件）走完整流水线，LLM 与 Odoo 使用录制回放（VCR 式）。
5. **评测回归**：Golden Set 批量跑分类与抽取，输出准确率报告并与基线比较，准确率下降则测试失败。
6. **运行验证**：真实邮箱的只读冒烟测试（手动触发，不上线），确认认证、分页、限流、水位线行为。

工具：`pytest` + `pytest-asyncio` + `respx`/`responses`（HTTP mock）+ `freezegun`（时间相关）；覆盖率目标 ≥ 85%（核心模块 ≥ 90%）。

---

## 17. 里程碑

| 阶段 | 内容 | 交付物 |
| --- | --- | --- |
| M0 骨架 | 项目结构、配置加载、日志、SQLite 建表与 DAO、单实例锁 | 可启动的空壳进程 + 单元测试 |
| M1 邮件接入 | M365 认证、增量拉取、附件下载、解析器 | 邮件与附件落库，可通过 API 查询 |
| M2 LLM 流水线 | 分类 + 抽取、提示词、Schema 校验、置信度 | 确认单转为结构化交易记录（暂不推送） |
| M3 Odoo 推送 | odoorpc 客户端、映射、幂等 upsert、重试 | 交易自动写入 Odoo，重复推送不产生重复记录 |
| M4 REST API | 状态、统计、明细、重处理、鉴权 | 完整 API + OpenAPI 文档 |
| M5 加固与上线 | 部署脚本、告警、Golden Set 评测、运维文档 | 7×24 稳定运行，指标达标 |

---

## 18. 风险与开放问题

### 18.1 风险

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| LLM 幻觉导致金额/数量错误 | 财务数据错误，影响最大 | 强制 Schema、交叉校验（§9.3）、置信度阈值、人工复核、Golden Set 回归 |
| 邮件格式高度多样（各券商不同、多语言、扫描件） | 抽取准确率波动 | 提示词加入机构专属示例；按发件人维护模板优先级；持续补充 Golden Set |
| Graph API 限流（429） | 拉取延迟 | 遵守 `Retry-After`、分页 `$top=50`、批量请求、退避重试 |
| Odoo 目标模型未确定 | 开发返工 | 映射配置化，先落自定义模型，后续切换不改代码 |
| 网络/进程中断导致漏信或重复 | 数据缺失/重复 | 水位线重叠窗口 + 双重幂等键 + 崩溃恢复队列 |
| 邮件内容隐私 | 合规风险 | 部署文档明示、可选脱敏、最小化留存 |
| LLM 成本上升 | 运行成本 | 分类用便宜模型、附件文本剪裁、成本统计与告警 |

### 18.2 开放问题（需用户决策）

| 编号 | 问题 | 影响 |
| --- | --- | --- |
| Q1 | README 第 5 条的"数据项清单"为空，最终要抽取哪些字段？ | 决定 JSON Schema 与 Odoo 映射 |
| Q2 | 交易数据写入 Odoo 的哪个模型与字段（自定义 `x_*` 模型还是 `account.move` 等）？ | 决定映射配置 |
| Q3 | M365 认证采用应用认证（client credentials）还是委派认证（refresh token）？ | 决定权限配置与令牌存储方式 |
| Q4 | LLM 供应商与模型（OpenAI / Azure OpenAI / 自建网关）？是否允许邮件内容出境？ | 决定客户端实现与合规评估 |
| Q5 | 是否需要留存邮件原文与附件？留存多久？ | 决定存储与清理策略 |
| Q6 | 低置信度记录人工复核的方式：仅 API，还是需要 CLI/简易页面？ | 决定 M4 范围 |
| Q7 | 是否需要"同一笔交易被多封邮件通知"的去重（成交回报 + 月结单）？ | 影响幂等键设计 |
| Q8 | 目标 Odoo 版本与账号权限（是否允许 create/write）？ | 影响集成可行性 |

---

## 附录 A：核心流程伪代码

```python
async def poll_once() -> None:
    emails = await fetcher.fetch_incremental(since=watermark - OVERLAP)
    for raw in emails:
        await store.upsert_email(raw)          # 唯一键冲突即跳过

    while mail := await store.next_pending():
        try:
            parsed = parser.parse(mail)                     # 正文 + 附件
            verdict = await llm.classify(parsed)
            if not verdict.is_confirmation:
                await store.mark(mail, "SKIPPED")
                continue
            payload = await llm.extract(parsed)             # JSON Schema 结构化输出
            result = validator.validate(payload)            # 规范化 + V1..V9
            if not result.ok:
                await store.mark(mail, "NEEDS_REVIEW", reason=result.errors)
                continue
            for tx in result.items:
                record = await store.save_transaction(mail, tx)
                await odoo.upsert(record)                   # 幂等写入
            await store.mark(mail, "PUSHED")
        except RetryableError as err:
            await store.schedule_retry(mail, err)
        except Exception as err:                            # 不可重试
            await store.mark(mail, "NEEDS_REVIEW", reason=str(err))

    await store.set_watermark(max(m.received_at for m in emails))
```

## 附录 B：枚举定义

```text
email.status:            PENDING | FETCHED | PARSED | CLASSIFIED | EXTRACTED
                         | VALIDATED | SKIPPED | PUSHED | FAILED | NEEDS_REVIEW
side:                    BUY | SELL | OTHER
issuer_type:             broker | bank | other
push_status:             PENDING | PUSHED | FAILED
attachment.parse_status: PENDING | OK | SKIPPED | FAILED
```
