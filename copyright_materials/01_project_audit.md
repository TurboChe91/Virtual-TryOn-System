# 穿戴甲AIGC商品视觉生产管理系统项目审计

软件简称：穿戴甲视觉生产系统  
版本号：V1.0

审计日期：2026-08-22  
审计对象：当前工作区的实际文件状态（包括原有未提交修改）  
审计边界：仅阅读仓库源码、配置样例和项目文档；未读取 `.env` 内容，未读取 `data/` 运行数据，未启动服务，未连接数据库，未调用 AI API，未执行发布或部署。

## 1. 审计结论摘要

该仓库实现的是一套面向穿戴甲商品视觉内容生产的单体 Web 服务。核心链路是：款式/SKU 建档与素材录入，生成结构化款式数据和版本化 Prompt，创建可追溯的图片生成任务，由进程内 Worker 从 SQLite 队列原子领取任务，调用 OpenAI 兼容图片接口，保存生成结果，执行自动 QA 与人工复核，再导出 Shopify 素材包或将通过门禁的试戴资产发布到 Cloudflare R2/D1。

代码中可以确认存在商品款式、SKU、素材、AI 生成任务、状态机、重试、输入快照、成本预算、QA、人工复核、候选版本、统计、导出和发布模块。代码中不存在独立注册/登录/会员/角色系统，不存在 Redis、RabbitMQ、Kafka、Celery 等外部消息队列，也不存在独立的前端构建框架。

需要特别说明：仓库包名、页面标题和大量内部技术标识仍为 `lunelle` / `Lunelle Studio`。源码中的 `FINGLOW` 与 `api.finglow.cn` 属于客户项目历史部署标识，不是本软件名称。拟登记名称统一为“穿戴甲AIGC商品视觉生产管理系统”，正式交存的前后 30 页应避开客户项目历史部署标识，同时保持源码审计事实真实可追溯。

## 2. 软件总体架构

软件采用单进程单体架构：

```text
浏览器（原生 HTML/CSS/JavaScript）
        │ REST / Multipart
        ▼
FastAPI 路由与管理接口（lunelle/server.py）
        │
        ├─ 款式、SKU、素材、切图、配置、统计、导出、发布
        │
        ▼
TaskService（lunelle/tasks.py）
        │ 事务、幂等、状态机、依赖、快照、预算
        ▼
SQLite WAL（编号 SQL 迁移） ◄──── 进程内 Worker 线程池轮询并原子领取
                                     │
                                     ├─ OpenAI 兼容图片/多模态接口
                                     ├─ 本地内容寻址素材库与结果目录
                                     ├─ 自动 QA / 可选视觉 LLM QA
                                     └─ 失败分类、退避重试、恢复

通过人工复核门禁的结果
        ├─ 本地 Shopify 素材包导出
        └─ Cloudflare R2 图片对象 + Cloudflare D1 元数据发布
```

服务启动时由 FastAPI lifespan 完成配置校验、目录初始化、数据库迁移、服务对象创建和 Worker 启动。Worker 与 HTTP API 共用同一 SQLite 数据库，但每个线程取得独立连接。事务使用 `BEGIN IMMEDIATE` 串行化关键写入。

## 3. 技术栈

| 类别 | 代码事实 |
|---|---|
| 主要编程语言 | Python 3.12+ |
| 其他语言 | SQL、HTML、CSS、原生 JavaScript、Shell、YAML、TOML、JSON |
| 前端框架 | 无；`lunelle/web/index.html` 与 `settings.html` 为服务端直接返回的原生页面 |
| 后端框架 | FastAPI 0.140.0、Starlette；Uvicorn 运行 |
| 数据校验 | Pydantic v2 |
| HTTP 客户端 | httpx |
| 图像处理 | Pillow |
| 本地数据库 | SQLite，WAL 模式，外键开启，编号 SQL 迁移 |
| 本地对象/文件存储 | `uploads/`、`outputs/`、`exports/` 及 SHA-256 内容寻址 `assets/` |
| 远端对象存储 | Cloudflare R2，仅用于已审核试戴资产发布 |
| 远端发布数据库 | Cloudflare D1，通过 Cloudflare REST API 更新试戴元数据 |
| AI 图片接口 | OpenAI 兼容 `/images/generations`、`/images/edits` 语义；支持兼容网关和参考图模式 |
| AI 文本/视觉接口 | OpenAI 兼容 Chat Completions，用于可选款式解析、按图识别、逐甲身份识别和建议性 QA |
| 异步机制 | 进程内 Python daemon 线程 + SQLite 持久任务队列；不是 asyncio 作业队列 |
| 部署 | Docker 单容器或本机 Python 进程；持久数据目录挂载；反向代理/HTTPS 由部署方提供 |
| 测试与质量工具 | pytest、ruff、mypy；测试代码不属于本次软著候选源码 |

AI 供应商并未被代码固定为某一家。配置和页面允许 OpenAI 兼容端点；代码及文档出现 GPT Image、Seedream 等兼容模式/示例，但不能据此认定生产环境实际使用的具体供应商或模型。

## 4. 目录结构

```text
lunelle/
  server.py                 FastAPI 生命周期、REST API、静态管理页
  schemas.py                API 输入模型与 StyleSpec 校验
  styles.py                 款式解析、SKU 派生、可选 LLM 解析
  vocab.py                  甲型、长度、肤色等受控词表
  prompts.py                Grid/Wearing/Hero/Matrix/Correction Prompt
  tasks.py                  款式与任务核心服务、幂等、依赖、状态流转
  models.py                 任务与人工复核状态机、错误分类、ID
  policy.py                 Batch/Precision 生成策略
  budget.py                 日预算、调用预留、谱系自动任务预算
  worker.py                 Worker 线程、执行、落盘、QA、自动修正
  providers/                AI 提供方抽象、OpenAI 兼容适配、测试 Mock
  profiles.py               图片/LLM 通道配置、激活和脱敏展示
  llm.py                    多模态 Chat 调用和结构化结果解析
  inputs.py                 实际发送输入解析、请求清单、快照对比
  snapshots.py              不可变任务输入快照和执行记录
  assets.py                 SHA-256 内容寻址素材存储
  splits.py                 2×5 款式图自动/人工切分及版本管理
  nailslots.py              手模逐甲槽位与掩膜数据结构
  planview.py               款式图检测、裁剪、视图计划合成
  geometry.py               输出尺寸与比例计算
  qa.py                     图像自动 QA 与结果持久化
  gating.py                 导出/发布默认拒绝门禁
  export.py                 WebP、manifest、Shopify CSV、生成报告导出
  publish.py                经审核资产的 R2/D1 版本化发布
  cloudflare.py             Cloudflare R2/D1 REST 客户端
  stats.py                  任务、成功率、失败、成本、QA、SKU 统计
  db.py                     SQLite 连接、事务、迁移和在线备份
  config.py                 环境变量配置与服务启动校验
  urlguard.py               出站 URL/SSRF 防护
  logging_setup.py          JSON 日志与敏感字段脱敏
  build.py                  运行时代码与依赖指纹、漂移检测
  migrations/               0001 至 0015 数据库迁移
  contracts/                Hero 与 Matrix 视图契约 JSON
  web/                      原生管理页面
tests/                      单元、集成、真实 API 可选测试；不进入候选源码
scripts/                    安装、启动、备份、探测脚本；不进入候选源码
docs/                       项目文档；不进入候选源码
Dockerfile                  Python 3.12 单容器镜像
docker-compose.yml          单服务、单持久卷、localhost 端口映射
```

## 5. 核心业务模块

### 5.1 商品/款式与 SKU 管理

- `styles` 表保存款式 ID、唯一 SKU、名称、结构化 `spec_json`、原始输入和素材路径。
- `StyleCreateRequest` 支持描述、名称、颜色、元素、纹理、甲型、长度、视觉风格、避免项等字段。
- `styles.py` 负责 SKU 清理/去重、确定性关键词解析以及可选 LLM 解析；无论来源如何，最终都必须通过 `StyleSpec` 校验。
- 当前模型是“款式/SKU”，没有库存、价格、订单、供应链等完整电商商品域模型。

### 5.2 素材管理

- 支持上传参考图和 2×5 款式图，校验文件大小、图片魔数及 PNG/JPEG/WEBP 格式，并使用服务端文件名。
- `assets.py` 将重要输入和衍生素材按完整 SHA-256 内容寻址保存，记录 MIME、大小、路径和种类。
- 2×5 款式图可自动切分为十甲；自动结果有置信度和门禁，也可创建人工框选版本，历史版本不覆盖。
- 手模底图与逐甲 mask 通过 `hand_models`、`hand_model_slots` 管理；管理页提供 4 肤色 × 4 视角配置入口。

### 5.3 AI 生成任务

- 输出类型包含 `grid`、`wearing`、`hero`、`matrix_cell`、`repair`；普通生成接口当前接受前三类。
- 创建任务时生成批次、Prompt、负面 Prompt、版本号、成本估算、幂等键、依赖任务和不可变输入快照。
- Wearing 任务可依赖同轮 Grid 成功结果；矩阵任务按肤色和视角扩展，并要求设计权威和手模素材。
- Correction 创建新任务，不覆盖成功任务；候选谱系保留父子关系、版本和人工选中的当前候选。

### 5.4 状态、重试和恢复

- 任务状态：`pending`、`running`、`success`、`failed`、`retrying`、`cancelled`。
- 所有状态变更受 `ALLOWED_TRANSITIONS` 约束；成功任务不能直接重跑，重新生成会创建新任务。
- 可重试错误包括超时、网络、DNS、限流、服务端错误、下载失败和进程中断。
- 不可重试错误包括认证失败、请求错误、内容策略、非法响应、写盘失败、配置错误、不安全 URL、依赖缺失、预算超限、依赖失败和快照不匹配。
- 自动重试使用指数退避：`base × 2^retry_count`，受最大重试次数限制。
- 启动恢复把崩溃遗留的 `running` 任务按 `interrupted` 处理，并重新完成未结束的本地 QA。

### 5.5 结果、QA、审核与发布

- Provider 返回的图像会先经 Pillow 完整解码并统一为 PNG，再通过临时文件、`fsync`、`os.replace` 原子落盘。
- 成功仅表示图片存在；QA 状态与任务状态正交，包含 `pending/running/done/error/skipped`。
- 自动 QA 检查图片存在性、尺寸/比例、Grid 结构、颜色一致性等；可选视觉 LLM QA 仅提供建议，不能代替人工批准。
- 人工复核状态包含 generated、waiting、approved、rejected、publish_ready、published；导出/发布采用默认拒绝门禁。
- 本地导出生成 WebP、manifest.json、products.csv 和 generation-report.csv。
- 线上发布先写 R2，再以 UPSERT 更新 D1；本地 `publish_versions` 记录计划、进度、错误和版本，降低跨系统部分失败风险。

### 5.6 数据统计/工作台

- 首页加载款式数量和任务指标，任务队列按状态、输出类型筛选并定时刷新。
- `stats.py` 从数据库实时统计状态分布、成功率、重试次数、失败原因、记录成本、尝试次数、QA 结果和 SKU 完成度。
- 代码中没有单独的数据仓库或 BI 系统，也没有独立“统计中心”页面。

### 5.7 用户与权限

- 代码明确注明“尚无用户系统”。
- 可选的 `LUNELLE_ADMIN_TOKEN` 用于保护写接口，浏览器把令牌保存在当前浏览器 `localStorage`，请求时作为 `X-Admin-Token` 发送。
- 服务端仅记录令牌指纹或客户端主机作为审核者标识，不保存该令牌到审核记录。
- 不存在用户注册、账号密码、找回密码、组织、角色或细粒度权限模型。

## 6. 数据流

1. 操作员从描述或图片创建款式。
2. 输入经 Pydantic 和受控词表校验；可选 LLM 输出仍需二次结构校验，失败时回退确定性解析。
3. 款式和唯一 SKU 写入 SQLite；上传图片写本地素材目录，关键输入进入内容寻址资产表。
4. Prompt 模块依据 StyleSpec、输出类型、身份文本和视图契约生成版本化 Prompt。
5. TaskService 计算幂等键、生成策略、成本、任务依赖和输入快照，在事务内写入 batch、task、lineage。
6. Worker 原子领取到期且依赖成功的任务；依赖失败的下游任务不会静默降级执行。
7. Worker 从快照解析输入，在调用边界重新哈希实际文件和 Prompt，并与快照比较；不一致则停止调用。
8. 预算模块在付费调用前原子预留金额；调用失败释放，成功后结算。
9. Provider 调用兼容图片接口；返回数据经校验、转 PNG、原子落盘，attempt 和 execution 记录保存请求、费用、输入摘要和运行时构建标识。
10. 本地 QA 和可选 LLM QA 执行，随后进入人工复核。
11. 审核通过的结果可本地导出，矩阵结果可经 R2/D1 发布。

## 7. 任务执行流程

```text
create_generation / create_matrix_generation / create_correction
  → 校验款式、输出类型、成本确认和依赖素材
  → 构造 Prompt、幂等键、输入快照、任务谱系
  → INSERT pending task
  → Worker.claim_next（BEGIN IMMEDIATE，依赖必须 success）
  → running + attempt started + 预算预留
  → 解析并比对实际请求与快照
  → Provider.generate
      ├─ 可重试失败 → retrying + next_attempt_at
      ├─ 不可重试/达到上限 → failed + 传播依赖失败
      └─ 成功 → PNG 原子落盘 → success
  → heuristic QA
  → 可选 LLM 建议性 QA
  → waiting_human_review
  → approved/rejected
  → export 或 publish
```

## 8. AI 生成工作流

### Grid/Wearing/Hero

- Grid：根据完整十甲身份和商品图规则生成 1:1 的 2×5 商品图，可附款式参考图。
- Wearing：生成手部佩戴图；启用参考模式时可等待并使用 Grid 成品作为设计参考。
- Hero：依据 Hero 视图契约、逐甲身份和参考素材生成上架主图。

### Matrix

- 按请求展开肤色 × 视角组合。
- 从获准的款式图切分版本取得逐甲裁剪，通过 `planview.py` 合成与该视角对应的视图计划。
- 同时冻结设计权威、手模底图、逐甲槽位等输入；缺失关键输入时记录为 unresolved 并阻止付费调用。
- 每个矩阵单元是独立任务，可单独 QA、人工复核、修正和发布。

### Correction 与自动工作

- 修正以某个成功候选为父任务，可附明确修正文本、目标甲位和补充图片；产生新版本，不覆盖原图。
- 硬性 QA 失败可触发自动重生成，视觉 LLM 给出可执行修正时可触发自动修正。
- 所有自动后代共享同一 root lineage 的次数和金额上限，避免重生成与修正交替形成无限循环。

## 9. 部署结构

- Dockerfile 基于 `python:3.12-slim`，以非 root 用户运行。
- 容器启动执行数据库初始化/迁移后启动 FastAPI/Uvicorn。
- Docker Compose 仅定义一个应用服务和一个 `lunelle-data` 持久卷，端口映射为 `127.0.0.1:8300:8300`。
- 数据库、输出、上传、导出和日志均位于持久数据目录；代码文档明确不支持多实例共享 SQLite。
- 对公网服务时需要外部反向代理和 HTTPS；仓库没有包含特定反向代理配置。
- Cloudflare R2/D1 是可选发布目标，不是应用运行所必需的主数据库。

## 10. API 结构

| API 分组 | 已实现路由 |
|---|---|
| 健康 | `GET /health`、`GET /ready` |
| 款式/SKU | `POST/GET /api/styles`、`GET /api/styles/{style_id}`、`POST /api/styles/from-image` |
| 素材/识别 | `POST /api/styles/{id}/reference-image`、`POST /identify`、`PUT /identity`、`GET /api/assets/{digest}` |
| 切图 | 获取、自动分析、人工切图、审核切图版本 |
| 生成 | 普通生成、矩阵估价、矩阵生成、任务修正 |
| 任务 | 列表、详情、图片、快照、谱系、候选选择、重试、取消、重跑 QA、人工复核 |
| 预算/批次/统计 | `/api/budget`、`/api/batches`、`/api/stats` |
| 通道配置 | 图片/LLM profile CRUD、启停、连通测试 |
| 手模配置 | 手模矩阵列表、上传、图片读取 |
| 导出 | `POST /api/export` |
| Cloudflare 发布 | 发布就绪检查、发布、历史、未完成发布、Cloudflare 配置与测试 |
| 管理页面 | `GET /`、`GET /settings` |

完整逐路由清单可直接从 `lunelle/server.py` 的 FastAPI 装饰器核对，共 54 个 HTTP 路由（含两个管理页面路由）。

## 11. 核心模块依赖关系

| 上游模块 | 主要下游依赖 | 作用 |
|---|---|---|
| `server.py` | schemas、styles、tasks、splits、profiles、stats、export、publish | 把 HTTP 请求转换为业务服务调用 |
| `styles.py` | schemas、vocab、可选 LLM chat | 形成可信 StyleSpec 和 SKU |
| `prompts.py` | schemas、contracts、models | 将款式、身份与视图规则转换为生成请求文本 |
| `tasks.py` | db、models、prompts、snapshots、inputs、assets、budget、splits、policy | 创建并维护完整任务生命周期 |
| `worker.py` | tasks、profiles/providers、inputs、snapshots、budget、qa、build | 从持久队列执行真实生成任务 |
| `providers/openai_compat.py` | httpx、urlguard、logging redaction | 构造和发送兼容 API 请求并分类错误 |
| `splits.py`/`planview.py`/`nailslots.py` | assets、Pillow、db | 管理十甲设计与手模几何素材 |
| `qa.py`/`gating.py` | db、Pillow、models | 机器检查及发布默认拒绝规则 |
| `publish.py` | gating、tasks、cloudflare、db | 将审核通过资产版本化写入 R2/D1 |
| `export.py` | gating、db、Pillow | 输出本地 Shopify 素材包和报告 |
| `stats.py` | db | 汇总后台看板数据 |

## 12. 未发现或不能确认的能力

- 未发现独立用户注册登录、组织/租户、角色权限、订单、库存、支付或客户 CRM。
- 未发现 Redis/Celery/RQ/Kafka/RabbitMQ；队列完全由 SQLite tasks 表和线程 Worker 实现。
- 未发现 S3/AWS SDK；对象发布代码是 Cloudflare R2 REST 调用。
- 未发现前端 React/Vue/Angular/Svelte 或 Node 构建链。
- 不能从代码判断实际生产使用的 AI 供应商、模型、Cloudflare 账户、域名权属、部署服务器规格和实际浏览器版本。
