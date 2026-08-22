# 穿戴甲AIGC商品视觉生产管理系统软著核心源码筛选记录

软件简称：穿戴甲视觉生产系统  
版本号：V1.0

## 1. 筛选原则

本表以当前工作区真实文件为准。“推荐提交”表示适合进入完整软著候选源码，不代表最终必须全部出现在普通交存的前、后各 30 页中。候选优先保留能够说明同一业务调用链的首方后端代码；测试、探测脚本、文档、静态 UI 模板、部署配置和 Mock 降低优先级或排除。

敏感信息检查区分“敏感字段名”和“真实秘密值”。`api_key`、`token` 等字段名是正常程序逻辑，可以出现；真实凭证值不得出现。仓库存在被 `.gitignore` 排除的 `.env` 和 `data/`，本次没有读取，也不会纳入候选。

“是否主要来源于第三方代码”仅依据仓库中是否存在第三方许可证头、vendored 包结构或生成标记判断。未发现这些迹象不等于已经完成著作权归属证明，最终仍需开发者确认代码形成过程和历史来源。

## 2. 推荐进入完整候选源码的文件

| 文件路径 | 模块用途 | 行数 | 推荐提交 | 推荐原因 | 敏感信息 | 主要第三方代码 |
|---|---:|---:|---|---|---|---|
| `lunelle/server.py` | FastAPI 生命周期、API 路由、上传与管理页面入口 | 1070 | 是 | 业务入口完整，连接款式、素材、任务、审核、导出和发布 | 有 token/api_key 字段引用，无真实值 | 否；使用 FastAPI API |
| `lunelle/schemas.py` | StyleSpec 与 API 请求校验 | 277 | 是 | 体现商品款式、生成、矩阵、审核、配置等业务约束 | 有 api_key/token 字段定义，无真实值 | 否 |
| `lunelle/styles.py` | 款式解析、SKU 派生、LLM 回退 | 270 | 是 | 体现自主商品款式结构化逻辑 | 未发现 | 否 |
| `lunelle/vocab.py` | 甲型、长度、颜色、肤色等受控词表 | 212 | 是 | 为 StyleSpec 与解析提供业务词汇约束 | 未发现 | 否 |
| `lunelle/prompts.py` | Grid/Wearing/Hero/Matrix/Correction Prompt 构造 | 479 | 是 | 直接体现视觉生成业务规则和版本化 Prompt | 未发现凭证；Prompt 需人工审查商业敏感性 | 否 |
| `lunelle/tasks.py` | 款式与任务核心服务 | 2114 | 是 | 幂等、依赖、快照、谱系、状态流转、失败恢复的核心 | 未发现真实秘密值 | 否 |
| `lunelle/models.py` | 任务状态机、复核状态机、错误分类 | 211 | 是 | 状态和重试策略的权威定义 | 未发现 | 否 |
| `lunelle/policy.py` | Batch/Precision 生成策略 | 51 | 是 | 约束自动创意修复和模式差异 | 未发现 | 否 |
| `lunelle/budget.py` | 成本估算、预算断路、调用预留、谱系上限 | 534 | 是 | 体现付费异步任务并发控制和防失控逻辑 | 未发现 | 否 |
| `lunelle/worker.py` | 线程 Worker、执行、落盘、QA、自动后续任务 | 910 | 是 | AI 生成实际执行主链 | 未发现真实秘密值 | 否 |
| `lunelle/providers/base.py` | Provider 请求/响应抽象与错误模型 | 51 | 是 | 连接 Worker 与具体接口适配 | 未发现 | 否 |
| `lunelle/providers/openai_compat.py` | OpenAI 兼容图片请求封装 | 296 | 是 | 参数构造、参考图、响应解析、错误分类、下载保护 | 有 api_key 字段使用，无真实值 | 否 |
| `lunelle/providers/__init__.py` | Provider/Chat 构建与环境回退 | 82 | 是 | 展示生产 Provider 选择入口 | 有 api_key/token 变量，无真实值 | 否；引用但不提交 Mock 文件 |
| `lunelle/profiles.py` | 运行时图片/LLM 通道配置 | 236 | 是 | 通道 CRUD、启用、密钥脱敏和动态解析 | 有 api_key 字段，运行值来自 DB；源码无真实值 | 否 |
| `lunelle/llm.py` | 多模态 Chat 调用和结构化解析 | 437 | 是 | 按图建款、身份识别、建议性 QA 的接口与校验 | 有 api_key/token 变量，无真实值 | 否 |
| `lunelle/inputs.py` | 实际输入解析、请求清单与快照一致性比较 | 449 | 是 | 防止执行时输入漂移，体现可追溯性 | 仅包含“secret”排除字段名，无值 | 否 |
| `lunelle/snapshots.py` | 输入快照、执行记录、指纹查询 | 411 | 是 | 固化 Prompt、模型、素材及执行证据 | 显式排除 api_key/secret，无真实值 | 否 |
| `lunelle/assets.py` | SHA-256 内容寻址素材库 | 239 | 是 | 素材真实性、去重和不可变引用 | 未发现 | 否 |
| `lunelle/splits.py` | 2×5 款式图切分及版本审核 | 410 | 是 | 体现素材处理、十甲管理与人工校正 | 未发现 | 否 |
| `lunelle/nailslots.py` | 手模逐甲 mask 与生理身份映射 | 250 | 是 | 体现试戴视觉业务的专用几何逻辑 | 未发现 | 否 |
| `lunelle/planview.py` | 款式图检测、裁剪和视图计划 | 427 | 是 | 连接十甲设计素材与 Matrix 生成 | 未发现 | 否 |
| `lunelle/geometry.py` | 输出尺寸与比例适配 | 77 | 是 | 支撑矩阵单元保持手模比例 | 未发现 | 否 |
| `lunelle/qa.py` | 图片自动 QA 和持久化 | 365 | 是 | 生成结果检查、评分和人工复核前置 | 未发现 | 否 |
| `lunelle/gating.py` | 导出/发布默认拒绝门禁 | 99 | 是 | 防止未 QA/未人工批准结果流出 | 未发现 | 否 |
| `lunelle/config.py` | 环境变量配置和启动校验 | 314 | 是 | 支撑真实运行参数与安全默认值 | 大量敏感变量名，无硬编码真实值 | 否 |
| `lunelle/urlguard.py` | 出站 URL/SSRF 防护 | 188 | 是 | 保护可配置 AI/下载地址 | 有 password/api_key 查询参数名，无值 | 否 |
| `lunelle/logging_setup.py` | JSON 日志与脱敏 | 102 | 是 | 展示 API Key/Token/Secret 的日志防泄漏 | 仅脱敏模式名，无真实值 | 否 |
| `lunelle/build.py` | 构建身份、依赖指纹、运行时漂移 | 440 | 是 | 生成结果可追溯到实际加载代码 | 未发现 | 否 |
| `lunelle/errors.py` | 业务异常 | 21 | 是 | 补全服务和成本确认依赖 | 未发现 | 否 |
| `lunelle/stats.py` | 任务、失败、费用、QA、SKU 统计 | 90 | 是 | 体现后台看板数据来源 | 未发现 | 否 |
| `lunelle/export.py` | Shopify 素材包和报告导出 | 254 | 是 | 生成结果管理和交付闭环 | 无凭证；含客户项目历史部署标识 `Lunelle Nails`，需隔离审核 | 否 |
| `lunelle/publish.py` | R2/D1 版本化发布 | 404 | 是 | 体现结果发布、幂等、部分失败恢复 | 含公开域名/R2 key 名，无凭证 | 否 |
| `lunelle/cloudflare.py` | R2/D1 REST 客户端和配置脱敏 | 162 | 是 | 对象存储与元数据发布封装 | 有 token/R2 字段，运行值来自 DB；源码无真实值 | 否 |
| `lunelle/db.py` | SQLite 连接、事务、迁移、备份 | 197 | 是 | 数据持久层和原子任务队列基础 | 未发现连接串/密码 | 否 |

### 数据库迁移

| 文件路径 | 模块用途 | 行数 | 推荐提交 | 推荐原因 | 敏感信息 | 主要第三方代码 |
|---|---:|---:|---|---|---|---|
| `lunelle/migrations/0001_init.sql` | styles/batches/tasks/attempts/qa 基础表 | 90 | 是 | 核心业务数据模型 | 无 | 否 |
| `lunelle/migrations/0002_profiles_hero.sql` | API profile、Hero/Matrix 输出类型 | 71 | 是 | AI 通道与扩展任务模型 | 有 `api_key` 字段名，无值 | 否 |
| `lunelle/migrations/0003_llm_settings.sql` | LLM profile 与应用设置 | 10 | 是 | 配置持久化 | 无实际值 | 否 |
| `lunelle/migrations/0004_tryon_publish.sql` | 款式与试戴发布 ID | 7 | 是 | 发布关联 | 无 | 否 |
| `lunelle/migrations/0005_review_closure.sql` | QA/人工复核状态与审计表 | 68 | 是 | 审核闭环及默认拒绝历史修复 | 无 | 否 |
| `lunelle/migrations/0006_lineage_and_reservations.sql` | 谱系预算和调用预留 | 78 | 是 | 防无限自动任务及并发超支 | 无 | 否 |
| `lunelle/migrations/0007_snapshots_and_assets.sql` | 内容寻址资产、快照、执行表 | 62 | 是 | 生成可追溯性 | 无 | 否 |
| `lunelle/migrations/0008_lineage_and_dependencies.sql` | 父子任务与依赖失败 | 43 | 是 | 完整异步依赖图 | 无 | 否 |
| `lunelle/migrations/0009_publish_versions.sql` | 发布版本和进度账本 | 51 | 是 | R2/D1 部分失败可见性 | 无 | 否 |
| `lunelle/migrations/0010_nail_slots.sql` | 手模与逐甲槽位 | 68 | 是 | 试戴素材模型 | 无 | 否 |
| `lunelle/migrations/0011_identity_provenance.sql` | 逐甲身份来源与人工确认 | 27 | 是 | 防止模型猜测冒充事实 | 无 | 否 |
| `lunelle/migrations/0012_build_identity.sql` | 执行构建指纹 | 58 | 是 | 代码版本追溯 | 无 | 否 |
| `lunelle/migrations/0013_execution_inputs.sql` | 实际请求输入摘要 | 54 | 是 | 快照与实际调用一致性 | 仅说明“不记录 key” | 否 |
| `lunelle/migrations/0014_crop_revisions.sql` | 切图版本和逐甲裁剪 | 41 | 是 | 素材版本管理 | 无 | 否 |
| `lunelle/migrations/0015_candidate_selections.sql` | 候选结果选择 | 14 | 是 | 生成结果工作台状态 | 无 | 否 |

## 3. 降低优先级或排除的首方文件

| 文件路径 | 模块用途 | 行数 | 推荐提交 | 原因 | 敏感信息 | 主要第三方代码 |
|---|---:|---:|---|---|---|---|
| `lunelle/__init__.py` | 包版本 | 3 | 否 | 信息量很低 | 无 | 否 |
| `lunelle/cli.py` | 命令行运维入口 | 340 | 备选 | 可补充运行管理，但不如 Web 主链集中 | 无真实值 | 否 |
| `lunelle/providers/mock.py` | 离线图片 Mock Provider | 124 | 否 | 测试辅助，不代表生产 AI 调用 | 无 | 否 |
| `lunelle/web/index.html` | 管理首页与业务 UI | 1073 | 否 | 大量 HTML/CSS/视图模板；业务后端已足够 | localStorage token 变量名，无值 | 否 |
| `lunelle/web/settings.html` | 设置 UI | 398 | 否 | 大量 HTML/CSS/表单模板；存在密钥输入控件 | 仅输入字段/占位符，无值 | 否 |
| `lunelle/contracts/hero_pose_contract.json` | Hero 姿势契约 | 53 | 备选 | 自主规则但属于数据契约，非主要程序 | 无 | 否 |
| `lunelle/contracts/matrix_views.json` | Matrix 视图契约 | 101 | 备选 | 自主规则但属于数据契约，非主要程序 | 无 | 否 |
| `Dockerfile` | 容器构建 | 40 | 否 | 部署配置，不是核心业务源码 | 无 | 否 |
| `docker-compose.yml` | 单服务部署 | 26 | 否 | 部署编排，不是核心业务源码 | 引用 `.env`，无值 | 否 |
| `.env.example` | 环境变量样例 | 114 | 否 | 包含敏感变量名称和供应商示例，且非业务代码 | 有 Key/Token 占位项 | 否 |
| `scripts/*.sh` | 安装、启动、备份、测试 | 多文件 | 否 | 运维脚本，核心性低 | setup 引用 Key 变量名 | 否 |
| `scripts/*.py` | 探测、导入、检查脚本 | 多文件 | 否 | 部分会触发真实 API 或依赖运行数据，不适合提交 | 多处 api_key 运行读取 | 否 |
| `tests/**/*.py` | 单元/集成/e2e 测试 | 多文件 | 否 | 测试数据、Mock 和占位 secret 较多，不作为业务源码 | 大量测试 token/key 字面量 | 否；测试代码为首方但不提交 |
| `docs/**`、`README.md` | 项目说明 | 多文件 | 否 | 不是源程序 | 仅变量名和示例 | 否 |
| `.github/workflows/ci.yml` | CI | 当前仓库文件 | 否 | 自动化配置，不是源程序 | secret 引用名，无值 | 否 |

## 4. 明确排除的目录和数据

- `.env`：已确认存在，但未读取；必须始终排除。
- `data/`：已确认存在，可能含 SQLite、上传图、生成图、日志、导出和真实通道配置；未读取；必须排除。
- `.git/`、缓存、虚拟环境、`__pycache__`、测试缓存：排除。
- 任何生成图片、客户上传图片、日志、备份数据库、导出 CSV：排除。
- `node_modules`、`dist`、`build`：当前清单未见有效业务来源，即使后续存在也应排除。

## 5. 敏感关键字结论

对整个仓库进行关键字级扫描后，`API_KEY`、`TOKEN`、`SECRET`、`PASSWORD`、`COOKIE`、`CLOUDFLARE_`、`R2_`、`OPENAI_` 等主要出现在：

- 环境变量名和 `.env.example` 占位说明；
- API/数据库字段名；
- 脱敏与安全测试；
- 页面密码输入框；
- 测试用假值；
- Cloudflare、OpenAI 兼容接口参数。

候选文件中未发现硬编码真实 API Key、Token、Cookie、密码、私钥、数据库连接串或 AWS 凭证。由于真实 `.env` 与 `data/` 未读取，本结论只适用于候选文本，不表示运行环境没有秘密。

## 6. 第三方代码结论

仓库没有 `node_modules`、vendored SDK、压缩库源码或带第三方许可证头的候选文件。候选代码调用 FastAPI、Pydantic、Pillow、httpx 等开源依赖，但不会复制这些依赖的源码。仓库根目录未发现 LICENSE/COPYING/NOTICE 文件；申请前仍需开发者确认所有首方文件的真实创作/委托/雇佣关系，以及前身项目代码的权利归属。
