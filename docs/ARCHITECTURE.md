# 架构

## 总览

单服务进程 = FastAPI(API+管理页) + 内嵌 Worker 线程池 + SQLite(WAL)。
所有持久状态在 `LUNELLE_DATA_DIR`：`lunelle.db`、`outputs/`、`uploads/`、`exports/`、`logs/`。

```
┌────────────────────────── 进程 ──────────────────────────┐
│ FastAPI 路由 ──► TaskService ──► SQLite (WAL, 事务)      │
│      │               ▲                                   │
│  web/index.html      │ claim(原子) / transition(状态机)  │
│                 Worker ×N ──► providers.openai_compat ───┼──► 图片 API
│                      │                                   │
│                      ├──► outputs/ (原子写, 永不覆盖)     │
│                      └──► qa.py ──► qa_results           │
└──────────────────────────────────────────────────────────┘
```

## 分层规则

- 路由层（server.py）不写 SQL、不做业务判断；一切经 TaskService。
- 状态流转只能走 `models.ALLOWED_TRANSITIONS`，`UPDATE ... WHERE status=当前值`
  乐观锁防并发串改；非法流转抛 `IllegalTransition`。
- 提示词只在 `prompts.py` 生成，版本号 `pv-N` 存进每个任务，改词必须升版本。
- 供应商差异全部封在 `providers/`；上层只见 `GenerationRequest/Result/ProviderError`。
- 配置只在 `config.py` 读环境变量；业务代码不碰 `os.environ`。

## 关键机制

| 机制 | 实现 |
|---|---|
| 幂等 | `idempotency_key = sha256(style|type|pv|prompt_hash|nonce)` UNIQUE；重复请求复用/跳过；force 用 batch_id 作 nonce 开新版本 |
| 领取任务 | `BEGIN IMMEDIATE` + 单条 UPDATE，同库多进程安全（已在 Docker 双容器共卷验证） |
| 依赖 | wearing 任务 `wait_for_task_id` 指向同批 grid；grid 结束（无论成败）后才可领取；grid 失败则 wearing 无参考图纯文本生成 |
| 崩溃恢复 | 启动时把 `running` 任务判为 `interrupted`（可重试），进退避队列 |
| 重试 | 错误分类见 `models.py`；退避 `base × 2^n`；上限 `max_retries`；手动重试仅限 failed/cancelled |
| 文件 | `outputs/<sku>/<sku>-<type>-<task>-a<attempt>.png`，临时文件+rename 原子落盘，路径记录回 DB |
| 一致性 | wearing 请求携带 grid 成品图（Seedream `image` 参数或 OpenAI edits 多部件，`LUNELLE_REFERENCE_MODE` 控制） |

## 为什么没有 Redis/队列/微服务

每个任务的瓶颈是一次 10-30 秒的收费 API 调用，单机并发上限本来就要压到个位数
（供应商限流+成本控制），SQLite WAL + 线程池已覆盖需求且少两个故障点。
吞吐需求上来时的演进路径：Worker 独立进程 → 换 PostgreSQL → 多实例。
