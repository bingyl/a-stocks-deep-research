# A股深研 API

基于 **FastAPI** 的个股深研工作台：股票池检索、财务/行情查询、Deep Agents + LangGraph 六维投研流水线（SSE）、报告持久化、追问 RAG。

用于信息整合与可解释分析，不提供买卖建议，也不是量化交易系统。

---

## 目录

- [架构总览](#架构总览)
- [请求链路](#请求链路)
- [六维深研流水线](#六维深研流水线)
- [追问 RAG 与去重](#追问-rag-与去重)
- [持久化分层](#持久化分层)
- [项目结构](#项目结构)
- [主要 API](#主要-api)
- [快速开始](#快速开始)
- [配置要点](#配置要点)
- [设计说明](#设计说明)

---

## 架构总览

下图按「浏览器 → API → Agent → 工具/RAG → 存储」分层，标出运行时职责与数据落点。

![项目架构图](docs/architecture.png)

<details>
<summary>Mermaid 源码（可编辑）</summary>

```mermaid
flowchart TB
  subgraph Client["客户端 static/"]
    UI["深研页 / 历史页 / 追问面板"]
    SSE_C["EventSource / fetch SSE"]
  end

  subgraph Gateway["FastAPI app/app.py"]
    R_SEARCH["routers/search<br/>联想 · 股票池"]
    R_STOCK["routers/stock<br/>概览 · 财务 · 行情"]
    R_AGENT["routers/agent<br/>analyze / analyze/stream"]
    R_REPORT["routers/reports<br/>报告 · 取消 · 追问 SSE"]
    LIFE["lifespan<br/>建表 · checkpointer · 孤儿任务回收 · 定时同步"]
  end

  subgraph AgentLayer["Agent 层 app/agent/"]
    ANALYZER["analyzer.py<br/>深研 SSE 编排"]
    DETACH["sse_bridge.iter_detached<br/>分析 Task 与 SSE 解耦"]
    GRAPH["graph.py<br/>Deep Agents 主编排"]
    PIPE["pipeline.py<br/>research_pipeline 六维 StateGraph"]
    FOLLOW["followup.py<br/>追问 Agent"]
    FTOOLS["followup_tools.py<br/>KB 短路 + singleflight"]
    TOOLS["tools.py / rag_tools.py<br/>业务工具 · rag_search"]
    MW["middleware.py<br/>模型/工具日志 · SSE 旁路推送"]
  end

  subgraph Domain["领域服务 app/services/"]
    STOCK["stock / market / boards"]
    REPORTS["reports<br/>任务状态机 · 同 code 互斥"]
    JOBS["analysis_jobs<br/>RunControl · 取消 drain"]
    CHAT["chat<br/>追问消息 DocStore"]
    UNIVERSE["universe / sync_stocks"]
  end

  subgraph RAG["RAG app/rag/"]
    INGEST["ingest<br/>切片 · embedding · upsert"]
    KB["kb_lookup<br/>同参未过期父文档"]
    RETR["retriever<br/>子向量召回 → 父文档组装"]
    FRESH["freshness<br/>stale_hours"]
  end

  subgraph Persist["持久化 app/persistence/"]
    DB["db/<br/>业务库 DATABASE_URL<br/>stocks · research_reports · sync_*"]
    DOC["docstore/<br/>LangGraph Store<br/>父文档 · 追问历史"]
    CP["checkpointer/<br/>分析短时记忆 thread_id"]
    VS["vectorstore/<br/>Chroma / Milvus<br/>子块向量"]
  end

  subgraph External["外部依赖"]
    LLM["LLM<br/>DeepSeek 等 OpenAI 兼容"]
    EMB["Embedding<br/>DashScope text-embedding"]
    BOCHA["博查搜索"]
    MKT["行情/财务源<br/>akshare · 东财等"]
  end

  UI --> R_SEARCH & R_STOCK & R_AGENT & R_REPORT
  SSE_C --> R_AGENT & R_REPORT
  R_AGENT --> DETACH --> ANALYZER
  ANALYZER --> GRAPH --> PIPE
  ANALYZER --> JOBS & REPORTS
  R_REPORT --> FOLLOW --> FTOOLS --> TOOLS
  FOLLOW --> RETR
  GRAPH --> TOOLS
  PIPE --> TOOLS
  TOOLS --> STOCK & BOCHA & MKT
  TOOLS -.->|工具结果| INGEST
  FTOOLS --> KB
  INGEST --> EMB & VS & DOC
  RETR --> VS & DOC
  CHAT --> DOC
  ANALYZER --> CP
  REPORTS --> DB
  STOCK --> DB
  UNIVERSE --> DB
  GRAPH --> LLM
  FOLLOW --> LLM
  MW --> SSE_C
```

</details>

### 模块职责一览

| 层级 | 职责 |
|------|------|
| **routers** | HTTP/SSE 边界、参数校验、409 冲突 |
| **agent** | 主编排、六维子图、追问、工具、流式进度 |
| **services** | 股票/板块业务、报告状态、取消控制、聊天 |
| **rag** | 入库、新鲜度、短路查询、召回 |
| **persistence** | 业务库 / DocStore / Checkpointer / 向量库可插拔 |
| **integrations** | LLM Embedding、博查等外部 SDK 封装 |

---

## 请求链路

### 深研分析（可多股票并行）

分析在独立 asyncio Task 中执行；浏览器断开或切换股票只停止 **SSE 收流**，不会误杀后台任务。显式取消走 `POST /api/reports/{id}/cancel` → `RunControl.drain`。

```mermaid
sequenceDiagram
  autonumber
  participant U as 浏览器
  participant API as POST /analyze/stream
  participant Det as iter_detached
  participant A as analyzer
  participant R as reports / analysis_jobs
  participant G as Deep Agent + pipeline
  participant T as tools
  participant RAG as ingest
  participant S as DB / DocStore / Vector / Checkpoint

  U->>API: code=601003
  API->>API: 同 code 已有 pending/running → 409
  API->>Det: 启动消费端 SSE
  Det->>A: create_task 生产端
  A->>R: create/reactivate report + analysis_run_id
  A->>R: register RunControl
  A-->>U: status / stage / tool_*（经队列）
  A->>G: astream_events(thread_id=analysis:r{id}:{run})
  G->>T: 调度工具
  T-->>RAG: schedule_ingest（异步）
  RAG->>S: parent upsert + child vectors
  G->>S: checkpointer 写入
  alt 用户点取消
    U->>R: POST /cancel
    R->>A: request_drain
    A-->>U: cancelled
  else 用户关掉 SSE / 换股票
    U--xDet: abort 消费端
    Note over A,S: 生产端继续跑到 final
  end
  A->>R: 落库 analysis + tool_trace
  A-->>U: final（若 SSE 仍连接）
```

### 同股票互斥 vs 多股票并行

```mermaid
flowchart LR
  A["开始 002709"] --> OK1["创建 report #1 running"]
  B["再开 601003"] --> OK2["创建 report #2 running"]
  C["再开 002709"] --> Conflict["409 ActiveAnalysisExists"]
  OK1 -.->|并行| OK2
```

---

## 六维深研流水线

主编排（Deep Agents）调度子智能体 `research_pipeline`（`CompiledSubAgent` + LangGraph `StateGraph`）：

```mermaid
flowchart TB
  START([用户提问 / 默认全面分析]) --> ORCH["主编排 Agent<br/>graph.py"]
  ORCH -->|task tool| PIPE["research_pipeline"]
  PIPE --> P0["0 解析任务"]
  P0 --> P1["1 基本面画像"]
  P1 --> P2["2 股性与估值框架"]
  P2 --> P3["3 板块联动与同业对比"]
  P3 --> P4["4 技术面辅助"]
  P4 --> P5["5 情报补缺<br/>公告/新闻/宏观"]
  P5 --> P6["6 固定大纲综合报告"]
  P6 --> OUT([Markdown 报告入库])

  P1 & P2 & P3 & P4 & P5 -.->|工具结果| RAGIN["rag.ingest → 向量知识库"]
```

阶段事件通过 SSE `stage` / `tool_start` / `tool_end` 推到前端进度条。

---

## 追问 RAG 与去重

追问目标：**优先复用本报告知识库**，避免同参工具重复拉数、重复 embedding。

```mermaid
flowchart TB
  Q["用户追问"] --> FA["followup Agent"]
  FA --> RS{"需要结构化数据？"}
  RS -->|是| WRAP["followup_tools 包装"]
  WRAP --> KB{"kb_lookup<br/>report_id + tool + arg_hash<br/>存在未过期父文档？"}
  KB -->|命中| SKIP["返回 skipped=true<br/>提示改用 rag_search"]
  KB -->|未命中| FLIGHT{"同参 inflight？"}
  FLIGHT -->|是| WAIT["等待第一次结果"]
  FLIGHT -->|否| CALL["真实调用 tools"]
  CALL --> ING["ingest：切片 / embedding / upsert"]
  ING --> UNC{"parents_content_unchanged？"}
  UNC -->|是| NOWRITE["跳过向量写入"]
  UNC -->|否| WRITE["写入 DocStore + Vector"]
  SKIP --> RAGS["rag_search 召回"]
  RS -->|直接问答| RAGS
  RAGS --> ANS["组织回答 + 写入 chat DocStore"]
```

要点：

- 父文档 id 前缀：`{report_id}:{tool}:{arg_key}:`（参数不同不会误短路）
- 新鲜度：`RAG_STALE_HOURS`（默认 24h）
- 短路工具集见 `FOLLOWUP_DEDUP_TOOLS`（财务/概览/K 线/同业等；联网搜索默认不短路）
- 召回：子向量检索 → 按 `parent_id` 去重组装父文档再喂模型

---

## 持久化分层

`DATABASE_URL` 决定方言（业务库为 **AsyncEngine + AsyncSession**）；SQLite 下把高频写库拆成旁路文件，降低锁竞争。

```mermaid
flowchart TB
  URL["DATABASE_URL"] --> DIALECT{"方言"}
  DIALECT -->|postgresql| PG["同一 Postgres<br/>业务表 + PostgresStore + PostgresSaver"]
  DIALECT -->|sqlite| SPLIT["拆分旁路"]

  SPLIT --> BIZ["app_local_sqlite.db<br/>stocks / research_reports / sync_*"]
  SPLIT --> DS["app_local_sqlite.docstore.db<br/>父文档 · 追问消息"]
  SPLIT --> CK["app_local_sqlite.checkpoints.db<br/>LangGraph checkpoints"]
  SPLIT --> VEC["data/vector_chroma 或 Milvus<br/>子块 embedding"]
```

| 存储 | 内容 | 选型 |
|------|------|------|
| 业务库 | 股票池、深研报告元数据与正文、同步元信息 | SQLAlchemy · SQLite / Postgres |
| DocStore | RAG 父文档、追问 chat 消息 | LangGraph Store（SqliteDocStore / PostgresStore） |
| Checkpointer | 分析线程短时状态 | AsyncSqliteSaver / AsyncPostgresSaver |
| Vector | 子块向量 | Chroma（本地）/ Milvus（生产） |

`thread_id` 形如：`analysis:r{report_id}:{analysis_run_id}`，保证重跑与多任务隔离。

---

## 项目结构

```
app/
  app.py                 # FastAPI 工厂与 lifespan
  core/                  # config / logging / scheduler
  routers/               # search · stock · agent · reports
  models/                # Pydantic 请求/响应
  services/              # 领域逻辑
  agent/                 # Deep Agents + LangGraph + SSE
  rag/                   # 分块 · 入库 · 召回 · 新鲜度 · KB 短路
  persistence/
    db/                  # SQLAlchemy 业务库
    docstore/            # LangGraph Store + 父文档仓库
    checkpointer/        # 分析 checkpointer
    vectorstore/         # Chroma / Milvus 后端
  integrations/          # 博查 · Embedding
  extensions/stocks/     # 东财板块等 CLI 扩展
static/                  # 前端工作台
data/                    # 本地库与向量数据（gitignore）
logs/                    # app.log / app_debug.log（gitignore）
```

---

## 主要 API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/search/suggest` | 股票联想 |
| GET/POST | `/api/search/universe/*` | 股票池状态 / 同步 |
| GET | `/api/stock/{code}/overview` 等 | 概览 / 财务 / 行情 |
| POST | `/api/agent/analyze` | 深研（一次性） |
| POST | `/api/agent/analyze/stream` | 深研（SSE） |
| GET | `/api/reports` · `/api/reports/{id}` | 报告列表 / 详情 |
| POST | `/api/reports/{id}/cancel` | 取消进行中任务 |
| POST | `/api/reports/{id}/chat/stream` | 追问（SSE） |
| GET/DELETE | `/api/reports/{id}/messages` | 追问历史 / 清空 |

深研 SSE 事件：`status` · `stage` · `tool_start` · `tool_end` · `final` · `error` · `cancelled`。

---

## 快速开始

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
pip install -r requirements.txt
# 如需显式安装向量可选依赖：
# pip install -r requirements-vector.txt

copy .env.example .env
# 编辑 .env：填入 LLM_API_KEY / EMBEDDING_API_KEY / BOCHA_API_KEY 等

start.bat
# 或：python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

浏览器打开：`http://127.0.0.1:8000`

### 板块扩展 CLI

```bash
python -m app.extensions.stocks.lookup_stock_boards 600519 --json
python -m app.extensions.stocks.fetch_concept_boards --help
```

---

## 配置要点

见 `.env.example`。常用项：

| 变量 | 含义 |
|------|------|
| `DATABASE_URL` | 业务库（`sqlite+aiosqlite` / `postgresql+psycopg` 异步）；SQLite 时自动旁路 docstore / checkpoints |
| `VECTOR_BACKEND` | `chroma` / `milvus` |
| `RAG_ENABLED` · `RAG_FOLLOWUP_TOOL` | 入库 / 追问短路开关 |
| `RAG_STALE_HOURS` | 知识库结果过期时间 |
| `AGENT_MAX_TOOL_ROUNDS` | 主编排工具轮次预算 |
| `FOLLOWUP_*_LIMIT` | 追问工具/模型硬上限 |
| `DEBUG` | `true` 时写 `logs/app_debug.log` |

日志：`logs/app.log`。

---

## 设计说明

1. **SSE 与分析解耦**：`iter_detached` 让消费端 abort 不影响生产端，支持多股票并行；取消必须走显式 cancel API。  
2. **同 code 互斥、异 code 并行**：避免同一标的双写报告，同时允许工作台并排深研。  
3. **SQLite 旁路三库**：业务 / DocStore / Checkpointer 分离写锁，缓解 `database is locked`。  
4. **父子文档 RAG**：子块向量召回、父块喂模型；追问用 `report_id+tool+args` 短路减少重复成本。  
5. **RunControl.drain**：协作式取消，避免硬杀半写入状态；`analysis_run_id` 防止旧轮次覆盖新一轮。

---

## 声明

行情与财务数据来自第三方公开接口，可能延迟或失败；输出不构成投资建议。
