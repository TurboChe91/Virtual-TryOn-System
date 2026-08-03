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
`POST /api/tasks/{task_id}/qa` — 对成功任务重跑质检。**重跑会重开人工复核**
（`review_state` 回到 `waiting_human_review`），因为原批准是针对旧判定给出的。
`POST /api/tasks/{task_id}/review` — 记录人工复核结论，body
`{approved: bool, note?, reviewer?}`。返回 `{qa_state, review_state, needs_human_review}`。
409 `qa_not_ready`=自动质检尚未完成（不是错误，稍后重试即可）；
409 `no QA result exists`=质检未产生结果，需先重跑 `/qa`。

### 状态字段（v5 起）

每个任务有两个正交状态，`GET /api/tasks` 与 `/api/tasks/{id}` 均返回，且可作为
`?qa_state=` / `?review_state=` 过滤：

- `qa_state`：`pending|running|done|error|skipped` — 机器质检走到哪一步。
  与 `status=success` **同事务写入**，因此不存在「已成功但质检状态未知」的窗口。
- `review_state`：`generated|waiting_human_review|approved|rejected|publish_ready|published`
  — 人工审核生命周期。`publish_ready` 是系统派生态，人工只能 approve/reject。

`/api/tasks/{id}` 另外返回 `qa`（**heuristic** 判定，闸门只认这一条）与
`qa_advisory`（LLM 判定，仅供参考，永不放行）。

## 批次 / 统计 / 导出

`GET /api/batches` — 批次汇总（任务数/成功/失败）。
`GET /api/stats` — 总量、按状态、成功率、重试总数、失败原因分布、成本（实记+估算）、
QA 概况、按 SKU 完成度。全部实时聚合自数据库。
`POST /api/export` — body `{skus:[]}`；返回导出目录、manifest 与 `skipped`（含逐个
款式被withhold 的原因）；422=没有任何款式可导出（detail 里列出具体原因）。

**导出与发布默认拒绝（v5 起，破坏性变更）**：只有同时满足以下全部条件的素材
才会出厂 —— `status=success` 且 `qa_state=done` 且存在 heuristic 质检行且该行
`passed=1` 且 `needs_human_review=0` 且 `review_state ∈ {approved, publish_ready,
published}`。任一条缺失即视为拒绝（`NULL` 一律判为拒绝）。

`include_unreviewed` 参数**已移除**：默认拒绝下它没有合法语义。旧客户端继续发送
会收到 422（schema 为 `extra="forbid"`），而不是静默得到与预期相反的结果。

## CLI 对照

`python -m lunelle.cli <cmd>`：`init` `serve` `health` `create-style` `generate`
`tasks` `show` `retry` `stats` `export` `backup`（`--help` 看参数）。
