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

## 费用控制

`POST /api/styles/{style_id}/matrix/estimate` — body 同 `/matrix`；**免费、不排队**。
返回 `image_count`（只算真正会新建的格）、`estimated_usd`、`worst_case_usd`（含重试与
自动返修的最坏情况）、`cells_already_succeeded`、`cells_in_progress`、
`requires_confirmation`、`budget`。

`POST /api/styles/{style_id}/matrix` — 估算金额 ≥ `LUNELLE_CONFIRM_COST_USD` 时必须带
`confirm_estimated_usd`，且必须与当前估算一致：

- 409 `cost_confirmation_required` — 未确认；响应含 `estimate` 供前端展示
- 409 `cost_confirmation_mismatch` — 确认金额与当前报价不符（预览后价格变了）
- 429 `budget_exceeded` — 会超出滚动 24h 预算；响应含 `budget` 快照。**整批拒绝，
  不会部分排队**

`GET /api/budget` — 滚动窗口开销快照（`realized_usd` 已实现、`committed_usd` 在途、
`remaining_usd`、`limit_usd`、`enabled`）。需要管理员：开销数字反映业务量。

预算熔断在 worker 调用 provider **之前**再检查一次，因此触发的代价是 0 次付费调用，
任务以非重试的 `budget_exceeded` 失败，窗口滚动或上调上限后可手动重试。

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

## 通道 Profile 与设置

`GET /api/profiles?kind=image|llm` — 列出通道；`api_key` 只返回指纹与末 4 位。
`POST /api/profiles` / `PUT /api/profiles/{id}` / `DELETE /api/profiles/{id}`
`POST /api/profiles/{id}/activate` / `POST /api/profiles/deactivate`
`POST /api/profiles/{id}/test` — 只读连通性探测（`GET {base_url}/models`），不产生图片费用。

**`base_url` 的出网限制**（`lunelle/urlguard.py`，422 `unsafe_url`）：

- 必须 https（http 一律拒绝，即使开了下面的开关）
- 不得内嵌凭证（`https://user:pass@…`）——会泄进日志与错误串
- 主机不得解析到 loopback / 私有 / link-local / reserved 空间，其中
  `169.254.169.254` 是把 SSRF 变成凭证窃取的关键载荷
- `LUNELLE_ALLOW_PRIVATE_API_HOSTS=1` 可放行私有地址（自建模型场景），**生产环境拒绝**
- 写入时校验，每次外发请求前再校验一次；无法解析的域名**不拦**（连不上即非通道，
  且 DNS 故障应归类为可重试的传输错误）

`GET /api/settings/cloudflare` — **需要管理员**：token 已指纹化，但 account /
database / bucket ID 本身就指向生产基础设施。
`POST /api/settings/cloudflare` — 部分更新，**留空字段表示保持原值**（因此保存无法撤销）。
`DELETE /api/settings/cloudflare` — 真正清除全部凭证，返回被删除的键名。
`POST /api/settings/cloudflare/test` — 只读探测 D1 与 R2 绑定。

`GET /api/settings/hand-models` / `POST /api/settings/hand-models/{tone}/{view}`
— 手模底图（4 肤色 × 4 视角）。缺失时矩阵任务以 `dependency_missing` 阻塞，
不会静默降级。

## CLI 对照

`python -m lunelle.cli <cmd>`：`init` `serve` `health` `create-style` `generate`
`tasks` `show` `retry` `stats` `export` `backup`（`--help` 看参数）。
