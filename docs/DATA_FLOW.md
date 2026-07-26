# 数据流

## 1. 款式创建

```
POST /api/styles {sku?, name?, description?, base_colors[]?, elements[]?, ...}
  → schemas.StyleCreateRequest 校验（长度/数量/去重/黑名单字段拒绝）
  → styles.build_style_spec:
      derive_sku（显式校验 或 从名称 slug 化 + 唯一化）
      确定性解析（vocab 词表，中英文关键词）⟂ 可选 LLM 解析（JSON 强校验，失败回退并记 warning）
  → StyleSpec（pydantic，enum 校验，缺省值）
  → styles 表：spec_json + source_input_json（原始输入留档，可复现）
  ← 201 {style, warnings, prompts_preview}
```

## 2. 生成计划

```
POST /api/styles/{id}/generate {output_types, force}
  → prompts.build_prompt_bundle(spec)  # grid/wearing/negative + quality_requirements, pv-N
  → 事务内逐类型:
      算 idempotency_key
      已有任务? active→复用 | success→跳过(需 force) | failed/cancelled→原任务重新入队
      否则 INSERT 任务(pending, 预估成本, wearing 带 wait_for=grid)
  ← 202 {batch_id, created[], reused[], skipped[]}
```

## 3. 执行（Worker 线程）

```
claim_next(): BEGIN IMMEDIATE → 选出 due 任务(pending/retrying, 依赖已了结) → 置 running
  → 解析参考图（wearing: 最近成功 grid 的 output_path，存在才用）
  → attempts 表插入 attempt(started)
  → providers.generate():
       POST {base}/images/generations (JSON, size, b64_json, seedream 时 watermark:false, image[]=参考图)
       或 POST {base}/images/edits (multipart, OpenAI 风格参考图)
       超时=LUNELLE_REQUEST_TIMEOUT_S；HTTP/网络错误 → ProviderError(code, retryable)
  → 成功: 原子写 outputs/... → attempts(success) → 任务 success(记 external_request_id, sha256)
          → qa.run_qa() → qa_results（QA 崩溃不影响任务结果）
  → 失败: attempts(error) → retryable 且有预算 → retrying(next_attempt_at=now+base*2^n)
                        否则 → failed(error_code, error_message[:2000])
```

## 4. 查询与导出

```
GET /api/tasks?sku&status&output_type&batch_id     列表（索引字段过滤）
GET /api/tasks/{id}                                 详情+attempts+最新 qa
GET /api/tasks/{id}/image                           校验路径在 output_dir 内后回文件
GET /api/stats                                      全部聚合自 tasks/attempts/qa_results
POST /api/export {skus?}                            每 SKU 取最新成功 grid+wearing
   → exports/export-<ts>/images/nail-style-<sku>-<type>.webp (q92)
   → manifest.json（来源任务、sha256、QA、prompt 版本 全量溯源）
   → products.csv（Shopify 导入骨架） + generation-report.csv
```

## 5. 数据库表

- `styles`：款式与原始输入
- `batches`：一次生成请求
- `tasks`：任务全字段（见 migrations/0001_init.sql，含成本/重试/幂等键/元数据）
- `attempts`：每次真实 API 调用（外部请求 ID、http 状态、耗时、错误、指纹）
- `qa_results`：每次质检（checks 全量 JSON、issues、needs_human_review）
- `schema_migrations`：迁移记录
