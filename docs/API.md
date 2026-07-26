# API 参考

Base URL: `http://<host>:8300`。写操作在设置 `LUNELLE_ADMIN_TOKEN` 后需请求头
`X-Admin-Token: <token>`。错误统一为 `{"error": "..."}`（400/404/409/422）或
`{"error":"internal server error","error_id":"..."}`（500）。
交互式文档：开发环境访问 `/docs`。

## 健康

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /health | 进程存活 `{status, version}` |
| GET | /ready | 深度检查（db/目录可写/配置/worker），degraded 时 503 |

## 款式

`POST /api/styles` — 创建款式。字段（全部可选，但至少给 name 或 description 或结构化字段）：
`sku`(kebab-case)、`name`、`description`(≤2000)、`base_colors[]`、`accent_colors[]`、
`elements[]`、`texture[]`、`shape`(almond|coffin|square|tapered-square|squoval|oval|round|stiletto)、
`length`(short|medium|long|extra-long)、`visual_style`、`avoid[]`、
`skin_tone`(light|medium|tan|deep)、`notes`、`use_llm`(bool)。
返回 201：`{style, warnings, parser, prompts_preview}`。409=SKU 冲突，422=输入非法。

`GET /api/styles?limit&offset`、`GET /api/styles/{style_id}`（含完整 prompts）。

`POST /api/styles/{style_id}/reference-image` — multipart 上传参考图（PNG/JPEG/WEBP，
默认≤10MB）。413=过大，422=非图片。

## 生成与任务

`POST /api/styles/{style_id}/generate` — body `{output_types:["grid","wearing"], force:false, note:""}`。
返回 202：`{batch_id, created[], reused[], skipped[]}`（幂等：进行中复用、已成功跳过、
失败任务自动重新入队；force=true 开新版本）。

`GET /api/tasks?sku&status&output_type&batch_id&limit&offset` — 任务列表。

`GET /api/tasks/{task_id}` — 全量详情：prompt/negative_prompt/prompt_version/状态/错误/
重试数/成本/`attempts[]`（每次 API 调用含 external_request_id、http_status、耗时）/
`qa`（最新质检）。

`GET /api/tasks/{task_id}/image` — 成品图（PNG）。

`POST /api/tasks/{task_id}/retry` — 手动重试，仅 failed/cancelled，409 其他状态。
`POST /api/tasks/{task_id}/cancel` — 取消，仅 pending/retrying。
`POST /api/tasks/{task_id}/qa` — 对成功任务重跑质检。

## 批次 / 统计 / 导出

`GET /api/batches` — 批次汇总（任务数/成功/失败）。
`GET /api/stats` — 总量、按状态、成功率、重试总数、失败原因分布、成本（实记+估算）、
QA 概况、按 SKU 完成度。全部实时聚合自数据库。
`POST /api/export` — body `{skus:[], include_unreviewed:true}`；返回导出目录与 manifest；
422=没有可导出的完整款式。

## CLI 对照

`python -m lunelle.cli <cmd>`：`init` `serve` `health` `create-style` `generate`
`tasks` `show` `retry` `stats` `export` `backup`（`--help` 看参数）。
