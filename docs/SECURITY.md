# 安全说明

## 密钥管理

- 密钥仅从环境变量/.env 读取（`config.py` 是唯一入口）；`.env` 在 `.gitignore`。
- 日志层全局脱敏：`sk-...` 与 `Bearer ...` 模式自动替换为 `<redacted>`（logging_setup.redact，含异常堆栈）。
- 日志/接口/统计中只出现密钥指纹（sha256 前 8 位），不可逆。
- 启动校验会拒绝 `.env.example` 的占位密钥。
- **历史事项**：前身项目 `Lunelle_Nails_fork_polish-contract/scripts/run_seedance_video.py`
  曾硬编码一枚 wisech API key（已于 2026-07-26 从源码移除；该目录非 git 仓库，无历史泄露面，
  但该 key 曾长期存在于本地磁盘，建议轮换）。本仓库从未包含真实密钥。

## 接口安全

- 写操作（创建款式/生成/重试/取消/导出/上传/重跑 QA）在设置 `LUNELLE_ADMIN_TOKEN`
  后要求 `X-Admin-Token` 头，恒定时间比较（hmac.compare_digest）。生产必设。
- 读接口**大部分**无敏感数据（无密钥、无用户隐私），但以下读接口已改为需要管理员：
  - `GET /api/settings/cloudflare` — token 已指纹化，但 account/database/bucket ID
    本身指向生产基础设施
  - `GET /api/budget` — 开销数字反映业务量
- 凭证可撤销：`DELETE /api/settings/cloudflare`。保存接口把留空字段当作「保持原值」
  （便于部分更新时不必重发 token），因此保存本身无法撤销，撤销必须是独立操作。

## 出网请求安全（SSRF）

API Profile 由运营者在运行时编辑，而服务端会向其发起请求，其中
`POST /api/profiles/{id}/test` 会回显响应体前 200 字节 —— 「攻击者可控 URL + 响应回显」
即构成可读 SSRF。`lunelle/urlguard.py` 在写入时与每次外发请求前校验：

- 强制 https；`http`/`file`/`gopher` 等一律拒绝
- 拒绝 URL 内嵌凭证（会泄进日志、错误串、Referer）
- 拒绝解析到 loopback / 私有 / link-local / reserved / multicast 的主机，
  包含 IPv4-mapped IPv6（`::ffff:127.0.0.1`）与 6to4 形式；
  `169.254.169.254`（云元数据）是把 SSRF 升级为凭证窃取的关键载荷
- 所有解析结果都必须合规：一个域名同时解析到公网与内网地址即拒绝
- `httpx` 全程 `follow_redirects=False`：302 跳转到内网地址会绕过首个 URL 的校验
- `LUNELLE_ALLOW_PRIVATE_API_HOSTS=1` 为自建模型场景放行私有地址，
  **生产环境启动校验直接拒绝**

**已知边界（未掩盖）**：校验时与请求时之间 DNS 可能变化（DNS rebinding）。
完整防护需在 HTTP 客户端做连接期 IP 钉定；当前通过「每次请求前重新校验」把窗口压到最小。
另：无法解析的域名**不拦**——它连不上，因此不是 SSRF 通道，且把 DNS 故障当成永久配置
错误会误判（provider 层将 DNS 失败归类为可重试的 `dns` 错误，那才是正确行为）。

## 成本安全

- 滚动 24h 预算熔断 `LUNELLE_DAILY_BUDGET_USD`（生产必须 > 0）。三处生效：队列授权在
  写事务内、调用前在事务内**预占**、调用后结算。触发代价为 0 次付费调用。
- 预占（`spend_reservations`）是多 Worker 下预算真正成立的原因：检查与预占同事务，
  并发线程会序列化。纯「先读后花」不行——两个线程可以都读到未超预算然后都花钱。
  实测 20 线程抢 5 个额度得到恰好 5 个。
- 超过 `LUNELLE_CONFIRM_COST_USD` 的批次必须回传**最坏情况上限** `confirm_max_usd`；
  上限变动即拒绝。授权预计值而承受最坏情况不叫知情同意。
- 自动重生与自动修正共用**同一份** per-root 额度
  （`LUNELLE_MAX_LINEAGE_DESCENDANTS`）。此前两条路径各记各的计数器且子任务不继承对方
  的，可以无限交替；已用复刻旧逻辑的脚本证实在 40 跳内从未终止。
- 一次点击最多产生 16 张付费图（试戴矩阵），现已必经预览与确认。
- 无默认账号/密码、无测试后门、无无保护删除接口（系统根本没有删除接口 —— 资产不可变，重生成走新版本）。
- 重试有状态守卫：success 任务不可重试/取消；重复生成默认幂等去重，force 是显式动作。

## 溯源与不可篡改

- 输入快照（`task_snapshots`）与任务插入同事务写入，之后**永不更新**。spec 与身份文本
  是副本而非引用，因此事后编辑款式不会改写历史。
- 快照**绝不含密钥**：只记录通道身份与密钥指纹（sha256 前 8 位）。快照生命周期长、
  会被导出、任何排查问题的人都会读到它。已有断言测试确保序列化后的快照里不含密钥。
- 输入资产按内容寻址（`assets/{digest}`），文件名即完整 sha256。覆盖在构造上不可能：
  不同字节 → 不同路径。这修掉了「手模写死在 `{tone}-{view}.png`，重新上传即静默替换了
  过往任务的生成依据」的问题。用完整 sha256 而非 12 位前缀——前缀碰撞虽罕见，但会静默
  把一个运营者的资产换成另一个的。
- 发布使用版本化 R2 键，永不覆盖；`publish_versions` 台账在第一次远程调用前记录意图，
  未完成的发布可见且可幂等重跑。

## 输入与文件安全

- 所有 API 入参过 pydantic Schema（extra="forbid"、长度/数量上限、enum 校验）。
- 上传：≤ `LUNELLE_MAX_UPLOAD_MB`、Pillow verify 魔数校验、仅 PNG/JPEG/WEBP；
  存储文件名由服务端生成（`ref-<sha256前12>.<ext>`），用户文件名不落盘 → 无路径穿越。
- 图片下载（供应商返回 URL 时）：仅 https、50MB 上限、流式读取。
- 出图接口回文件前校验路径 resolve 后仍在 `LUNELLE_OUTPUT_DIR` 内。

## 运行安全

- 生产 `LUNELLE_DEBUG=0`（validate_for_serve 强制）；500 响应只含 error_id，堆栈仅入日志。
- `/docs` (OpenAPI UI) 在 production 关闭。
- Docker 以非 root 用户（uid 10001）运行；服务默认绑定 127.0.0.1，公网必须经反代+HTTPS。
- 依赖全部 `==` 锁定（requirements.txt / requirements-dev.txt），无未记录全局依赖。

## 合规提示

`LUNELLE_DISABLE_PROVIDER_WATERMARK=1`（默认）会请求供应商去掉「AI生成」角标。
部分司法辖区/平台要求对 AI 生成内容进行标识 —— 面向消费者发布前请确认贵司所在
市场的标识义务，必要时设为 0 或在商品页自行披露。
