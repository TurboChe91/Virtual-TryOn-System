# Nail Slot System — 数据模型、迁移方案与分阶段实施计划

本文档是 Nail Slot System 引入前的设计基线。第一阶段（P0 审核闭环）已按本文
实施，其余阶段按顺序推进。

## 0. 资产勘查结论（实测，非推断）

新底图目录 `新底图mask/{light,medium,tan,deep}/*.png` 共 16 张，实测：

| 事实 | 证据 |
|---|---|
| 16 张全部是 **slot_preview** 语义（彩色覆盖在甲片上） | 10 个预览色以**精确 RGB 值**存在，非近似 |
| 每个 Nail 区域是**单一连通块** | P2 light 实测 blobs=1（nail-09 有 1px 噪点） |
| **无文字、无箭头、无杂色墨迹** | 饱和杂色像素数 = 0 |
| 可见性与生理手指一致 | P2/P4 = 10 甲；P3 = nail-06..10；P5 = nail-01..05 |
| 分辨率 | P2 = 1672×941；P3/P4/P5 = 1448×1086 |
| **干净 hand_base 已存在** | `Lunelle_Nails_fork_polish-contract/shopify-theme-pc-fix/assets/*.webp` |
| hand_base 与 preview **像素级对齐** | 覆盖区之外 mean diff = **0.00**，>60 差异像素 = **0.00%** |

结论：`slot_labels.png`、`mask_nail_01..10.png`、`view_manifest.json` **全部可由现有
两组文件确定性推导**，无需人工描边。缺失项只有一个：predecessor 的 webp 需要作为
`hand_base` 正式纳管（当前 Studio 里手模是管理员手工上传的单张图，没有 base/label 之分）。

## 1. 「废弃画面横坐标编号」的实测依据

现有 `matrix_views.json` 的 `mapping_note` 用「SCREEN LEFT-to-RIGHT」表达身份。实测
同一批 nail ID 在不同视角下的屏幕顺序**完全相反**：

- P2 左手屏幕左→右：`nail-05, 04, 03, 02`（拇指 nail-01 在下方中央）
- P4 左手屏幕左→右：`nail-02, 03, 04, 05`（拇指 nail-01 在左下）

因此横坐标顺序只是**每个视角的渲染结果**，不可能是身份定义。新规则（nail ID ↔ 生理
手指永久绑定，视角只影响可见性与位置）与现有 contract 的**数据**并不冲突——现有
mapping 逐条核对后与生理绑定一致；冲突的是**表述方式**（"Do not infer order from
anatomy"）。Mask 落地后，屏幕顺序文本整体作废，由 Mask 直接决定位置。

注意区分：`llm.py` 的 identity 提示词里「top row left-to-right = nail-01..05」指的是
**2×5 plan grid 的格位顺序**（格 1 = 左拇指），不是手部视角的屏幕顺序。两者不可混用，
但前者本身正确，无需修改。

## 2. 数据模型

### 2.1 第一阶段：审核闭环（已实施，迁移 0005）

不新建表承载审核状态，而是在 `tasks` 上加两个**正交**的显式状态列，并新增一张审计表。
理由：一个 task 恰好产出一张图，审核对象就是 task 的产物；把状态放在 task 上让所有
查询（导出、发布、列表、统计）都能直接过滤，无需 JOIN 派生。

```
tasks.qa_state       pending | running | done | error | skipped     -- 质检管线阶段
tasks.review_state   generated | waiting_human_review | approved
                     | rejected | publish_ready | published          -- 审核生命周期
tasks.reviewed_at    TEXT NULL
tasks.reviewed_by    TEXT NULL

asset_reviews(review_id, task_id, qa_id, decision, note, reviewer, created_at)
qa_results.source    heuristic | llm | manual                        -- 默认 heuristic
```

两列正交的含义：`qa_state` 描述机器质检走到哪一步，`review_state` 描述人工审核走到
哪一步。用户要求的五态是二者的组合视图：

| review_state | 含义 | 谁写 |
|---|---|---|
| `generated` | 图已产出，QA 未完成 | worker（随 success 原子写入） |
| `waiting_human_review` | QA 已落库，等人看 | worker（随 QA 落库原子写入） |
| `approved` | 人工明确通过 | `/review` 端点 |
| `rejected` | 人工明确否决 | `/review` 端点 |
| `publish_ready` | 人工通过 **且** 机器闸门同时成立 | 系统计算，不可手工设置 |
| `published` | 已成功写入线上 D1/R2 | publish 流程 |

`publish_ready` 刻意设计为**系统派生态**而非人工可设态：人工只能表达 approved/rejected，
「可发布」必须由 approved + 闸门共同成立，避免人工绕过闸门。

**自动 QA 通过 ≠ 人工通过**：LLM 判定一律写 `source='llm'` 且 `needs_human_review=1`，
永不推进 `review_state`。它只是给人的建议，不是闸门的一部分。

### 2.2 默认拒绝闸门（唯一实现，导出与发布共用）

集中在 `lunelle/gating.py`，同时提供 SQL 片段与 Python 谓词，并用单元测试互相校验，
防止两条路径漂移：

```sql
t.status = 'success'
AND t.qa_state = 'done'
AND q.qa_id IS NOT NULL                      -- 必须存在质检行
AND COALESCE(q.passed, 0) = 1                -- NULL 视为不通过
AND COALESCE(q.needs_human_review, 1) = 0    -- NULL 视为需复核
AND t.review_state IN ('approved','publish_ready','published')
```

`COALESCE` 是关键：LEFT JOIN 无质检行时旧代码得到 NULL，而 `NULL` 为假，反而让
「只要已复核」的过滤器放行了最该拦的资产（原审查问题 2）。已实测验证闸门对无质检行
的资产返回空集。

### 2.3 第四阶段：Nail Slot 资产（迁移 0007 规划）

```
hand_models(hand_model_id, tone, view, revision,
            hand_base_path, hand_base_sha256,
            slot_preview_path, slot_labels_path, slot_labels_sha256,
            width, height, manifest_json, source_note,
            created_at, retired_at)
    UNIQUE(tone, view, revision)

hand_model_slots(hand_model_id, nail_id, hand, finger,
                 mask_path, mask_sha256,
                 bbox_x, bbox_y, bbox_w, bbox_h, area_px)
    PRIMARY KEY(hand_model_id, nail_id)

slot_qa_results(slot_qa_id, task_id, nail_id, passed, score,
                checks_json, issues_json, created_at)

repair_targets(task_id, nail_id)          -- 本次返修允许修改的 Slot
preservation_checks(task_id, outside_mask_mean_diff,
                    outside_mask_max_diff, changed_px_ratio, passed)
```

`revision` 从一开始就在，因为第三阶段要求资产版本冻结：任务快照引用
`hand_model_id`（含 revision），底图换版不会改写历史任务的输入。`hand` 与 `finger`
两列把生理绑定写进数据库，而不是只存在于提示词文本里。

## 3. 迁移方案

| 迁移 | 阶段 | 内容 | 是否需要重建表 |
|---|---|---|---|
| 0005 | 一 | `qa_state`/`review_state`/`reviewed_at`/`reviewed_by`、`asset_reviews`、`qa_results.source`、索引 | 否（纯 ADD COLUMN） |
| 0006 | 三 | 输入快照与 `input_fingerprint`、lineage、发布版本化 | 待定 |
| 0007 | 四 | `hand_models`/`hand_model_slots`/`slot_qa_results`/`repair_targets`/`preservation_checks` | 否（全新表） |

已实测确认 SQLite 接受 `ALTER TABLE ... ADD COLUMN ... NOT NULL DEFAULT ... CHECK(...)`
并在其后强制该 CHECK，因此 0005 无需 0002 那样的整表重建。

**0005 回填策略（保守、默认拒绝）**：所有 `status='success'` 的历史任务回填为
`qa_state='done'`（若有质检行）或 `'error'`（若无），`review_state` 一律回填为
`waiting_human_review`——**不因历史 `needs_human_review=0` 就判定为人工已通过**。
原因：现有 LLM 闸门会把 `needs_human_review` 写成 0，无法与人工清除区分，按用户
「自动 QA 通过 ≠ 人工通过」的规则必须重新走人工。代价是历史资产需重新审一次，
这是正确方向上的一次性成本。

## 4. P0 修复与 Nail Slot 结构的冲突判定

逐条核对，结论：**无结构性冲突，且第一阶段是第四阶段的前置条件。**

| P0 修复 | 与 Nail Slot 的关系 | 判定 |
|---|---|---|
| 无质检行不可导出/发布 | Slot 级 QA 是**追加**一张 `slot_qa_results`，闸门只需再 AND 一个条件 | 兼容，闸门函数需可扩展 |
| `needs_human_review` 闸门 | 与 Slot 无关，作用于整图审核 | 兼容 |
| 自动 QA ≠ 人工通过 | Slot 级自动 QA 同样不得自动放行，规则一致 | 兼容且必需 |
| 只有 approved 进 publish_ready | 发布对象未来是 (tone, view) cell，仍是 task 粒度 | 兼容 |
| 消除 success/QA 竞态 | `qa_state` 机制对 Slot 级 QA 同样适用（多写几行 slot 结果） | 兼容且必需 |
| 缺手模必须 blocked | 第四阶段手模变成强依赖（base+labels+masks 缺一不可），检查点相同 | 兼容且必需 |

唯一需要现在就做对的设计决定：闸门必须是**单一函数**，第四阶段给它加 Slot 条件时
不改调用点。已按此实现（`lunelle/gating.py`）。

一处**表述**需在第四阶段作废（非冲突）：`prompts.py` 的 `_hero_view_contract()` 与
`matrix_views.json` 的 `mapping_note` 用屏幕顺序描述身份。Mask 落地后这些文本由
Manifest + Mask 取代。第一阶段不动它们——现有数据经核对是正确的，此刻改动只会
在没有 Mask 的情况下降低出图质量。

## 4.1 第一阶段实施结果（实测）

| 要求 | 实现 | 验证 |
|---|---|---|
| 无 QA 行不可导出/发布 | `gating.py` 用 `COALESCE` 把 NULL 判为拒绝 | `test_gating.py` 540 组合 SQL/Python 一致；`test_cell_with_no_qa_row_is_excluded` |
| 需人工复核不可导出/发布 | 闸门同时要求 `needs_human_review=0` 与 `review_state` 已批准 | `test_unreviewed_cell_is_excluded`、`test_unreviewed_is_not_exportable` |
| 自动 QA ≠ 人工通过 | LLM 判定写 `source='llm'` 且 `needs_human_review` 恒为 1；闸门只看 heuristic 行 | `test_llm_verdict_never_satisfies_the_gate` |
| 只有 approved 进 publish_ready | `publish_ready` 为系统派生态，人工只能 approve/reject | `test_publish_ready_cannot_be_set_by_a_human` |
| 消除 success/QA 竞态 | `qa_state` 与 `status` 同事务写入；`/review` 返回 `qa_not_ready` | 竞态测试 **100/100**，原 flaky 测试独立进程 **100/100** |
| 缺手模必须 blocked | `DependencyMissing` 在 provider 调用**之前**抛出 | `test_matrix_cell_blocks_without_hand_model`（`provider.calls == 0`） |

**顺带发现并修复的第三个缺陷**：导出路径**从未校验 `qa_passed`**。所有集成测试里的
mock 图都只有 ~3KB，低于 QA 的 30KB 下限（该下限用于识别被截断的真实下载），
即 `qa_passed=0`，但旧导出照样放行。现已让 mock 产出带确定性颗粒噪声的图
（振幅 8 < QA 前景阈值 24，不影响数甲与配色检查），并由闸门真正校验。

迁移在**真实旧库副本**（13 任务 / 11 成功 / 13 QA 行）上验证：无行丢失、
11 个成功任务全部回到 `waiting_human_review`、迁移后可发布数 **0**、
CHECK 生效、重复执行为空操作。

## 5. 分阶段实施计划与预计修改文件

### 第一阶段 P0 审核闭环（本轮，独立提交）
- 新增 `lunelle/gating.py`、`lunelle/migrations/0005_review_closure.sql`
- 改 `lunelle/models.py`（状态常量与转换表）、`lunelle/tasks.py`（原子写入 qa_state、
  review_state 推进、依赖检查）、`lunelle/worker.py`（QA 阶段状态机、缺手模 fail-fast）、
  `lunelle/qa.py`（source 列）、`lunelle/export.py` 与 `lunelle/publish.py`（改用闸门）、
  `lunelle/server.py`（/review 语义、状态暴露、qa_not_ready）、`lunelle/schemas.py`
- 测试：新增 `tests/integration/test_review_closure.py`、`tests/unit/test_gating.py`，
  竞态测试重复 ≥100 次

### 第二阶段 成本与安全（已实施）

| 项 | 实现 | 验证 |
|---|---|---|
| a. 费用预览 | `POST /{id}/matrix/estimate` 免费、不排队，只报增量格数 | `test_estimate_queues_nothing_and_prices_the_batch`、`test_estimate_prices_only_the_incremental_cells` |
| a. 二次确认 | 超过 `LUNELLE_CONFIRM_COST_USD` 必须回传 `confirm_estimated_usd`，金额变动即拒 | `test_expensive_batch_needs_confirmation`、`test_stale_confirmation_is_refused` |
| a. 预算熔断 | 排队时（含在途）与 worker 调用前（仅已实现开销）双重检查 | `test_queue_time_breaker_refuses_and_queues_nothing`、`test_worker_breaker_spends_nothing_when_tripped`（`provider.calls == 0`） |
| b. HTTPS | 原已存在于 `profiles.py`（我上一轮的审查结论有误，已更正） | `test_urlghard` 全组 scheme 用例 |
| b. SSRF | 新 `urlguard.py`：拒 loopback/private/link-local/reserved、IPv4-mapped、URL 内嵌凭证 | 17 组实测全部符合预期；含 `169.254.169.254` |
| c. CF 接口鉴权 | `GET /api/settings/cloudflare` 加 `require_admin`；新增 `DELETE` 清除凭证 | `test_reading_settings_requires_admin`、`test_credentials_can_be_cleared` |
| 附带：.env 权限 | `setup.sh` umask+chmod 600；`cli init` 检测并告警 | 实测 644→600，告警文案已验证 |

两个熔断点的口径**故意不同**（`budget.py` 模块注释有完整说明）：排队时把在途任务
计入，防止多批次累加超支；worker 只计已实现开销加当前任务，否则任何大于剩余预算的
批次会被自己的队列卡死。

SSRF 的一个**已知边界**（写在 `urlguard.py` 里而不是掩盖）：DNS 在校验时与请求时
之间可能变化（DNS rebinding），完整防护需要在 HTTP 客户端里做连接期 IP 钉定。
当前做法是在每次外发请求前重新校验，把窗口压到最小。

另一个刻意的判断：**无法解析的域名不视为不安全**。它连不上，因此不是 SSRF 通道；
而把 DNS 故障当成永久配置错误会误判——provider 层本来就把 DNS 失败归类为可重试的
`dns` 错误，那才是正确行为。

### 第三阶段 快照与版本冻结
迁移 0006、`tasks.py`（input_fingerprint、lineage、依赖失败传播）、`publish.py`
（原子化 + 版本化 key）、`export.py`

### 第四阶段 Nail Slot System
新增 `lunelle/nailslots.py`（Manifest/Label/Mask 派生与校验）、
`lunelle/handmodels.py`（纳管与预览）、迁移 0007、`scripts/import_hand_models.py`、
`qa.py`（按 Slot 裁切）、`worker.py`（Mask inpainting）、
`prompts.py`（作废屏幕顺序表述）、`web/settings.html`（手模管理与预览）

