# 穿戴甲视觉生产系统 — AI 穿戴甲视觉资产生产系统

面向 Shopify 穿戴甲品牌的生产级 AI 视觉资产系统：输入款式描述，自动产出同一款式的
**1:1 十枚甲片 2×5 排列商品图** 和 **真实手部佩戴效果图**，并完成任务管理、质检、
重试、成本统计与 Shopify 素材导出。

> 前身项目：`Lunelle_Nails_fork_polish-contract` 与 `Lunelle_AI_ImageGen`（脚本集合）。
> 本项目将其中验证过的提示词工程与 QA 规则产品化为可部署服务。

---

## 1. 业务问题

穿戴甲品牌上新需要为每个款式拍摄/制作两类主图（整套甲片商品图 + 手部佩戴图）。
人工拍摄成本高、周期长；直接用生图工具则存在款式漂移、两图不一致、无法批量管理、
无成本与质量追踪的问题。

## 2. 系统解决方案

```
款式输入(结构化字段/自然语言/参考图)
  → 结构化款式数据 StyleSpec（Schema 校验、默认值、可选 LLM 解析+校验兜底）
  → 提示词生成（grid/wearing/negative，版本化 pv-N）
  → 两个独立生成任务（幂等、状态机、成本预估）
  → Worker 调用真实图片 API（并发受控、超时、错误分类、退避重试）
  → wearing 任务以 grid 成品图为设计参考 → 两图风格一致
  → 图片原子落盘 + QA 自动检查（存在/可读/比例/分辨率/2×5 计数/色彩一致性）
  → 任务/批次/统计/日志全程可查 → Shopify 素材导出（webp + manifest + CSV）
```

## 3. 当前已实现并验证的功能

- 款式创建：结构化字段 + 中英文自然语言关键词解析 + 可选 LLM 结构化（输出强校验，失败自动回退）
- 双图生成：真实 API（Seedream/OpenAI 兼容端点）验证通过；佩戴图使用网格图作为参考图保证一致性
- 任务系统：pending/running/success/failed/retrying/cancelled 显式状态机；崩溃恢复；幂等去重；force 新版本
- 重试：错误分类（可重试/不可重试）、指数退避、上限控制、手动重试留痕、成功任务禁止误重跑
- QA：确定性检查 + 2×5 甲片投影检测（多阈值自适应）+ 双图色彩一致性；自动化永不替代人工终审
- 管理：Web 管理页（/）、REST API（/docs）、CLI；筛选、详情、图片预览、重试、统计、导出
- 导出：`nail-style-<sku>-grid.webp` / `-wearing.webp` + manifest.json + products.csv + generation-report.csv
- 运维：结构化 JSON 日志（含 task_id 全链路、密钥自动脱敏）、健康检查、SQLite 在线备份、Docker 部署

## 4. 技术栈

Python 3.12+ / FastAPI / SQLite(WAL) + 编号 SQL 迁移 / 线程 Worker / Pillow /
httpx / pydantic v2 / pytest + ruff + mypy / Docker。无 Redis、无消息队列 ——
单机吞吐（每任务一次收费 API 调用，秒级完成）用内置 worker 即可，避免无谓复杂度。

## 5. 目录结构

```
lunelle/            应用包
  config.py         全部配置（仅从环境变量读取）
  db.py             SQLite 连接/事务/迁移执行器/备份
  migrations/       编号 SQL 迁移（0001_init.sql ...）
  models.py         状态机、错误分类、ID 生成
  schemas.py        StyleSpec 与 API 模型（pydantic）
  vocab.py          甲型/长度/颜色/元素/质感受控词表
  styles.py         款式结构化（确定性解析 + LLM 可选）
  prompts.py        提示词生成（唯一出口，版本 pv-N）
  providers/        图片供应商适配层（openai_compat / mock仅测试）
  tasks.py          任务服务：创建/领取/状态流转/幂等/恢复
  worker.py         后台执行线程（并发上限=LUNELLE_MAX_CONCURRENCY）
  qa.py             自动质检
  export.py         Shopify 素材导出
  stats.py          统计（全部来自真实数据库）
  server.py         FastAPI 路由 + 管理页
  web/index.html    管理界面（无构建步骤）
  cli.py            命令行入口
tests/              unit / integration / e2e
scripts/            setup / start / dev / test / backup
Dockerfile, docker-compose.yml, .env.example
docs/               架构、部署、排障等文档
```

## 6. 环境要求

- 本地运行：Python ≥ 3.12（无其他系统依赖；Pillow 走 wheel）
- 或 Docker（任意近年版本；compose 可选）
- 一个 OpenAI 兼容图片生成端点的 API Key（见下）

## 7. 安装（本地）

```bash
git clone <repo> lunelle-studio && cd lunelle-studio
./scripts/setup.sh          # 建 venv、装锁定依赖、生成 .env、init 数据库
vi .env                     # 填入 LUNELLE_IMAGE_API_KEY 等
./scripts/setup.sh          # 再跑一次确认 configuration: OK
```

## 8. 环境变量

完整清单见 `.env.example`（每项均有注释）。必填：

| 变量 | 说明 |
|---|---|
| `LUNELLE_IMAGE_API_BASE_URL` | OpenAI 兼容端点，如 `https://ai.wisech.com/v1` 或 `https://api.openai.com/v1` |
| `LUNELLE_IMAGE_API_KEY` | 图片 API 密钥（启动时校验，占位值会被拒绝） |
| `LUNELLE_IMAGE_MODEL` | 如 `doubao-seedream-4-5-251128` 或 `gpt-image-1` |

常用选填：`LUNELLE_ADMIN_TOKEN`（生产必设）、`LUNELLE_MAX_CONCURRENCY`、
`LUNELLE_MAX_RETRIES`、`LUNELLE_TEXT_MODEL`（启用 LLM 款式解析）、
`LUNELLE_GRID_IMAGE_SIZE`（必须 1:1）、`LUNELLE_DISABLE_PROVIDER_WATERMARK`。

## 9. 启动

```bash
./scripts/start.sh                      # 生产模式（前台，配 systemd/supervisor 即为守护）
./scripts/dev.sh                        # 开发模式（代码热重载）
```

打开 `http://127.0.0.1:8300/` 使用管理界面；`/docs` 为 API 文档（生产环境自动关闭）。

## 10. Docker 部署（推荐生产方式）

```bash
cp .env.example .env && vi .env         # 填密钥
docker compose up -d                    # 或见 docs/DEPLOYMENT.md 的纯 docker 命令
docker compose logs -f lunelle
```

数据全部落在命名卷 `lunelle-data`（数据库/图片/日志），容器重建不丢数据。
若机器没有 compose：

```bash
docker build -t lunelle-studio .
docker run -d --name lunelle-studio --env-file .env \
  -e LUNELLE_DATA_DIR=/data -e LUNELLE_HOST=0.0.0.0 -e LUNELLE_ENV=production \
  -v lunelle-data:/data -p 127.0.0.1:8300:8300 lunelle-studio
```

## 11. 数据库初始化与迁移

首次启动自动完成（`init`/容器启动都会执行迁移）。新增迁移 = 在
`lunelle/migrations/` 加 `NNNN_name.sql`，重启或 `python -m lunelle.cli init` 即应用；
已应用记录在 `schema_migrations` 表。禁止手工改库。

## 12. 创建第一个任务

管理页表单，或：

```bash
curl -s -X POST http://127.0.0.1:8300/api/styles -H 'Content-Type: application/json' -d '{
  "name": "Pearl French",
  "description": "milky white almond nails with pearl, thin gold line, french tip, glossy translucent finish, medium length",
  "avoid": ["cartoon style", "oversized decoration"]
}'
# 记下返回的 style_id，然后：
curl -s -X POST http://127.0.0.1:8300/api/styles/<style_id>/generate \
  -H 'Content-Type: application/json' -d '{"output_types":["grid","wearing"]}'
```

CLI 等价：`python -m lunelle.cli create-style ... && python -m lunelle.cli generate --sku <sku> --wait 120`
（CLI generate 需要服务已运行，worker 在服务进程内）。

## 13. 查看结果

- 管理页任务列表 → 点任务号 → 看图/Prompt/QA/调用记录
- `GET /api/tasks?sku=&status=&output_type=&batch_id=`、`GET /api/tasks/{id}`、`GET /api/tasks/{id}/image`
- 文件位置：`<数据目录>/outputs/<sku>/<sku>-<type>-<task>-a<n>.png`（永不覆盖）

## 14. 日志

`<数据目录>/logs/lunelle.log`（JSON lines，20MB×5 轮转）+ 标准输出（Docker 用
`docker logs`）。每条含 task_id/batch_id/sku/stage/status/duration_ms；
API Key 永不入日志（自动脱敏）。按任务追踪：`grep tk_xxx data/logs/lunelle.log`。

## 15. 运行测试

```bash
WITH_DEV=1 ./scripts/setup.sh   # 安装 dev 依赖
./scripts/test.sh               # ruff + mypy + pytest（119 用例，全部离线）
RUN_REAL_E2E=1 .venv/bin/python -m pytest tests/e2e -m real_api   # 付费真实 API 冒烟（可选）
```

## 16. 健康检查

- `GET /health`：进程存活；`GET /ready`：数据库可用/目录可写/配置完整/worker 存活（503=降级）
- CLI：`python -m lunelle.cli health`
- Docker 自带 HEALTHCHECK（`docker ps` 可见 healthy）

## 17. 数据备份与恢复

```bash
./scripts/backup.sh                       # 在线一致性备份 → data/backups/lunelle-<ts>.db
# 恢复：停服务 → 用备份文件替换 LUNELLE_DB_PATH → 启动
# 图片目录 outputs/ 直接文件级备份（rsync/快照均可）
```

Docker 卷备份见 `docs/BACKUP_AND_RECOVERY.md`。

## 18. 停止服务

本地：Ctrl-C / `pkill -f lunelle.cli`；Docker：`docker compose down`（数据在卷中保留）。

## 19. 升级版本

```bash
git pull && ./scripts/setup.sh && 重启服务      # 迁移自动应用
# Docker: docker compose build && docker compose up -d
```

## 20. 常见错误

| 现象 | 处理 |
|---|---|
| 启动报 configuration invalid | 按提示补 .env 必填项 |
| 任务 `auth_invalid` | 密钥无效/过期，换密钥后手动重试任务 |
| 任务 `config_error` model not available | `LUNELLE_IMAGE_MODEL` 在该端点不存在 |
| 任务 `rate_limited`/`server_error` | 自动退避重试；持续失败调低 `LUNELLE_MAX_CONCURRENCY` |
| 任务 `interrupted` | 服务重启所致，自动转入重试 |
| 更多 | 见 `docs/TROUBLESHOOTING.md` |

## 21. 已知限制

- QA 无法自动识别水印文字/手部畸形/款式还原度 —— 这些列入 `manual_review_items`，
  自动化只做硬性检查并默认标记待人工复核；复核通过用管理页按钮或
  `POST /api/tasks/{id}/review {"approved":true}` 记录，导出时可用
  `include_unreviewed=false` 只导出已复核资产
- 甲型/长度对生成模型是软约束，可能漂移（两图之间一致性由参考图机制保证）
- 成本为估算值（OpenAI 兼容图片接口不回报实际扣费），单价可用 `LUNELLE_PRICING_JSON` 校准
- SQLite 单机部署；多实例共享数据库不受支持（WAL 支持同机多进程，已验证）

## 22. 安全注意事项

- 密钥只放 `.env`（已 gitignore）或环境变量；日志/接口/前端均不暴露
- 生产必设 `LUNELLE_ADMIN_TOKEN`（所有写操作需 `X-Admin-Token` 头）
- 生产 `LUNELLE_DEBUG=0`（强制校验）、`/docs` 自动关闭、错误响应不含堆栈
- 上传校验魔数+大小+格式，文件名由服务端生成（防路径穿越）
- 对外发布务必经反向代理 + HTTPS，见 `docs/DEPLOYMENT.md`

## 23. 更多文档

`docs/ARCHITECTURE.md`、`docs/DATA_FLOW.md`、`docs/DEPLOYMENT.md`、
`docs/PRODUCTION_CHECKLIST.md`、`docs/TROUBLESHOOTING.md`、
`docs/BACKUP_AND_RECOVERY.md`、`docs/SECURITY.md`、`docs/API.md`、
`docs/CURRENT_STATE.md`
