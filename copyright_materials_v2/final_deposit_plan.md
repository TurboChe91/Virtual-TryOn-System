# 穿戴甲AIGC商品视觉生产管理系统最终普通交存计划

软件全称：穿戴甲AIGC商品视觉生产管理系统  
软件简称：穿戴甲视觉生产系统  
版本号：V1.0

## 1. 最终候选说明

最终候选文件：`source_submission_candidate.txt`  
候选 SHA-256：`2dbf4c9c0f3bc8f7d94d8c0a2e6cf23907f4f31232cd7dc2c60204e61b3345b0`  
真实来源文件：49 个首方文件  
候选文件内容行数：12,327 行  
含文件路径标记和分隔空行的物理行数：12,475 行  
按每页 50 个物理行估算：约 250 页。

相较第一轮 12,871 行原始候选，提交净化副本减少 544 行纯叙述性注释、叙述性文档字符串、历史故障/前身/客户说明及相邻空行。没有修改函数逻辑、变量名、类名、API 逻辑、SQL 语句、业务规则，也没有改写或删除参与业务计算、API 请求或 SQL 的字符串常量；客户部署适配的可执行代码仍真实保留在候选中部。逐文件复核确认，净化后的 Python 可执行行和 SQL 非注释语句与真实源码顺序一致。

## 2. 完整候选源码顺序

“候选起止行”覆盖该文件净化后的内容，不含文件路径标记。删减仅造成注释行数变化，所以本表行数不等同于真实源文件原始行号。

| 顺序 | 真实文件 | 净化后内容行数 | 候选起止行 | 安排理由 |
|---:|---|---:|---:|---|
| 1 | `lunelle/models.py` | 211 | 3–213 | 状态机、错误分类、ID |
| 2 | `lunelle/schemas.py` | 277 | 217–493 | API 与业务数据模型 |
| 3 | `lunelle/styles.py` | 270 | 497–766 | Style/SKU 结构化 |
| 4 | `lunelle/vocab.py` | 207 | 770–976 | 穿戴甲受控词表；前身描述已删 |
| 5 | `lunelle/policy.py` | 51 | 980–1030 | 生成策略 |
| 6 | `lunelle/tasks.py` | 2104 | 1034–3137 | 任务创建、幂等、依赖、队列与重试 |
| 7 | `lunelle/prompts.py` | 443 | 3141–3583 | Prompt 构造；前身叙述已删 |
| 8 | `lunelle/budget.py` | 534 | 3587–4120 | 成本预算和调用预留 |
| 9 | `lunelle/worker.py` | 900 | 4124–5023 | Worker 执行主链 |
| 10 | `lunelle/providers/base.py` | 51 | 5027–5077 | Provider 抽象 |
| 11 | `lunelle/providers/openai_compat.py` | 296 | 5081–5376 | AI API 调用适配 |
| 12 | `lunelle/providers/__init__.py` | 82 | 5380–5461 | Provider/Chat 构建 |
| 13 | `lunelle/profiles.py` | 236 | 5465–5700 | AI 通道配置与脱敏 |
| 14 | `lunelle/llm.py` | 437 | 5704–6140 | 多模态识别与建议性 QA |
| 15 | `lunelle/config.py` | 314 | 6144–6457 | 配置和启动校验 |
| 16 | `lunelle/urlguard.py` | 188 | 6461–6648 | 出站 URL 安全 |
| 17 | `lunelle/logging_setup.py` | 101 | 6652–6752 | 日志和敏感字段脱敏 |
| 18 | `lunelle/build.py` | 378 | 6756–7133 | 构建追溯；历史故障叙述已删 |
| 19 | `lunelle/errors.py` | 21 | 7137–7157 | 业务异常 |
| 20 | `lunelle/stats.py` | 90 | 7161–7250 | 后台统计 |
| 21 | `lunelle/export.py` | 254 | 7254–7507 | 客户导出适配置于中部 |
| 22 | `lunelle/publish.py` | 349 | 7511–7859 | 客户发布适配置于中部 |
| 23 | `lunelle/cloudflare.py` | 153 | 7863–8015 | R2/D1 客户部署适配置于中部 |
| 24 | `lunelle/server.py` | 1061 | 8019–9079 | 避免历史页面标题出现在前段 |
| 25 | `lunelle/migrations/0001_init.sql` | 88 | 9083–9170 | 基础数据结构 |
| 26 | `lunelle/migrations/0002_profiles_hero.sql` | 63 | 9174–9236 | Profile/Hero 扩展 |
| 27 | `lunelle/migrations/0003_llm_settings.sql` | 8 | 9240–9247 | LLM 设置 |
| 28 | `lunelle/migrations/0004_tryon_publish.sql` | 5 | 9251–9255 | 客户部署说明已删，SQL 保留且位于中部 |
| 29 | `lunelle/migrations/0005_review_closure.sql` | 50 | 9259–9308 | QA/人工复核状态 |
| 30 | `lunelle/migrations/0006_lineage_and_reservations.sql` | 60 | 9312–9371 | 谱系和预算预留 |
| 31 | `lunelle/migrations/0007_snapshots_and_assets.sql` | 37 | 9375–9411 | 快照、素材和执行表 |
| 32 | `lunelle/migrations/0008_lineage_and_dependencies.sql` | 24 | 9415–9438 | 任务父子与依赖 |
| 33 | `lunelle/migrations/0009_publish_versions.sql` | 34 | 9442–9475 | 发布版本账本 |
| 34 | `lunelle/migrations/0010_nail_slots.sql` | 68 | 9479–9546 | 手模逐甲槽位 |
| 35 | `lunelle/migrations/0011_identity_provenance.sql` | 27 | 9550–9576 | 逐甲身份来源 |
| 36 | `lunelle/migrations/0012_build_identity.sql` | 33 | 9580–9612 | 构建身份；历史事件叙述已删 |
| 37 | `lunelle/migrations/0013_execution_inputs.sql` | 36 | 9616–9651 | 实际执行输入 |
| 38 | `lunelle/migrations/0014_crop_revisions.sql` | 41 | 9655–9695 | 切图版本 |
| 39 | `lunelle/migrations/0015_candidate_selections.sql` | 14 | 9699–9712 | 候选结果选择 |
| 40 | `lunelle/splits.py` | 410 | 9716–10125 | 2×5 款式图切分 |
| 41 | `lunelle/nailslots.py` | 220 | 10129–10348 | 逐甲槽位和 mask |
| 42 | `lunelle/planview.py` | 399 | 10352–10750 | 视图计划合成 |
| 43 | `lunelle/inputs.py` | 398 | 10754–11151 | 实际输入解析与调用边界 |
| 44 | `lunelle/snapshots.py` | 380 | 11155–11534 | 不可变快照和执行记录 |
| 45 | `lunelle/assets.py` | 222 | 11538–11759 | 内容寻址素材 |
| 46 | `lunelle/geometry.py` | 58 | 11763–11820 | 穿戴甲图像尺寸/比例处理 |
| 47 | `lunelle/qa.py` | 350 | 11824–12173 | 自动 QA |
| 48 | `lunelle/gating.py` | 99 | 12177–12275 | 人工复核发布门禁 |
| 49 | `lunelle/db.py` | 195 | 12279–12473 | SQLite 事务、迁移和备份；作为程序结尾 |

## 3. 前 30 页计划

范围：候选第 1–1500 行。

| 文件 | 使用范围 | 内容 |
|---|---|---|
| `models.py` | 全部 | 任务状态机、人工复核状态机、错误重试分类 |
| `schemas.py` | 全部 | StyleSpec 和 API Schema |
| `styles.py` | 全部 | 款式/SKU 结构化、确定性/LLM 解析 |
| `vocab.py` | 全部净化版 | 穿戴甲受控词表 |
| `policy.py` | 全部 | Batch/Precision 策略 |
| `tasks.py` | 候选第 1032–1500 行 | 款式 CRUD、Prompt/通道衔接、输入快照、幂等键及 `create_generation` 起始部分 |

第 1500 行位于 `TaskService.create_generation` 的 `metadata_intent` 字典表达式内部，因此**前 30 页发生函数中间截断**。这是严格保持完整文件顺序并以 1,500 个物理行作为页界的结果；没有从函数内部抽取或拼接代码。最终排版若希望页末落在完整语句处，需要由人工微调每页实际行数并重新执行品牌扫描。

受 30 页容量限制，预算、SQLite 原子领取和 Worker 文件不能与完整 Schema/Style/Task 创建同时全部装入前 1,500 行；它们紧随 TaskService 位于完整候选后续主体中。当前方案优先保证数据模型、API Schema、Style/SKU、状态机、幂等和任务创建的连续性。

## 4. 后 30 页计划

范围：候选第 10976–12475 行。

| 文件 | 使用范围 | 内容 |
|---|---|---|
| `inputs.py` | 从候选第 10976 行 `def load_all(...)` 至文件末尾 | 冻结输入解析、通道指纹、请求清单和快照比较 |
| `snapshots.py` | 全部 | 不可变任务快照、执行输入与指纹 |
| `assets.py` | 全部 | SHA-256 内容寻址素材存储 |
| `geometry.py` | 全部 | 穿戴甲视觉尺寸/比例处理 |
| `qa.py` | 全部 | 自动图片 QA 和结果持久化 |
| `gating.py` | 全部 | 人工审核和发布默认拒绝门禁 |
| `db.py` | 全部 | SQLite WAL、事务、迁移和备份 |

后 30 页从一个完整函数定义开始，结束于 `db.py` 文件末尾，因此**没有函数中间截断**。`splits.py`、`nailslots.py` 和 `planview.py` 被连续安排在该区段之前（候选第 9716–10750 行），完整候选的结束链仍是穿戴甲切图/槽位/视图计划 → 实际输入 → 快照 → 素材 → 图像几何 → QA → 人工门禁 → SQLite；受严格 1,500 行限制，前三个较大的穿戴甲模块不进入最终后 30 页。

## 5. 前后 30 页品牌扫描

| 标识 | 前 30 页 | 后 30 页 | 结论 |
|---|---:|---:|---|
| `FINGLOW` | 0 | 0 | 达标 |
| `finglow` | 0 | 0 | 达标 |
| `api.finglow.cn` | 0 | 0 | 达标 |
| `Lunelle Studio` | 0 | 0 | 达标 |
| `Lunelle Nails` | 0 | 0 | 达标 |
| `lunelle`（大小写不敏感） | 8 | 11 | 包名、路径、环境变量或迁移标记；未改写 |

准确行号和上下文见 `brand_scan.md`。客户发布/导出适配字符串分别位于候选第 7476、7858、8135 行附近，均在中部。

## 6. 前后 30 页敏感信息扫描

### 6.1 真实凭证格式

| 特征 | 前 30 页 | 后 30 页 |
|---|---:|---:|
| OpenAI 风格长 Key | 0 | 0 |
| AWS Access Key | 0 | 0 |
| Google API Key | 0 | 0 |
| JWT | 0 | 0 |
| PEM Private Key | 0 | 0 |
| 带用户名密码的数据库 URL | 0 | 0 |
| 长格式硬编码 Bearer Token | 0 | 0 |

### 6.2 敏感字段名

按用户指定关键词进行大小写不敏感扫描：

| 关键词 | 前 30 页命中数 | 后 30 页命中数 |
|---|---:|---:|
| `API_KEY` | 2 | 4 |
| `SECRET` | 0 | 2 |
| `PASSWORD` | 0 | 0 |
| `TOKEN` | 1 | 0 |
| `COOKIE` | 0 | 0 |
| `PRIVATE_KEY` | 0 | 0 |
| `AWS_` | 0 | 0 |
| `CLOUDFLARE_` | 0 | 0 |
| `R2_` | 1 | 0 |
| `DATABASE_URL` | 0 | 0 |
| `OPENAI_` | 0 | 0 |
| `ANTHROPIC_` | 0 | 0 |

前 30 页仅出现字段/变量名：

- `token`：第 423 行。
- `r2_bucket`：第 425 行。
- `api_key`：第 467、480 行。

后 30 页仅出现防泄漏和指纹逻辑中的字段名：

- `secret`：第 10992、11273 行。
- `api_key`：第 11276 行 3 次、第 11285 行 1 次。

上述均不是凭证值。前后 30 页未发现 Cookie、密码、私钥、数据库连接密码、客户数据、客户域名、真实 IP 或个人信息。

## 7. 提交前人工动作

1. 以本文件规定的固定行范围制作源程序页，不再把中部客户适配文件移到页首或页尾。
2. 若排版改变了每页物理行数或删除任何额外注释，重新计算前后边界并重跑品牌/敏感扫描。
3. 页眉统一使用“穿戴甲AIGC商品视觉生产管理系统 V1.0”，不要使用客户项目名称。
4. 不附带 `.env`、`data/`、数据库、客户素材、生成图、日志或浏览器存储。
5. 最终打印前核对候选 SHA-256；若内容发生任何变化，生成新哈希并更新本计划。
