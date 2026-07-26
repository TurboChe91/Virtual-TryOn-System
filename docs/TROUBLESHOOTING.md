# 排障手册

## 定位入口

1. `curl :8300/ready` —— 哪个 check 是 false 就查哪个方向
2. `data/logs/lunelle.log`（或 `docker logs`）—— JSON 行，按 task_id 过滤：
   `grep tk_xxxx data/logs/lunelle.log | python3 -m json.tool` 逐行看 stage/status/error_code
3. 任务详情 `GET /api/tasks/{id}` —— error_code/error_message/attempts（含 http_status 与外部请求 ID）

## 按错误码处理

| error_code | 含义 | 可自动重试 | 处理 |
|---|---|---|---|
| timeout / network / dns | 网络问题 | 是 | 查出网/代理；`LUNELLE_REQUEST_TIMEOUT_S` 可调大 |
| rate_limited | 429 限流 | 是 | 调低 `LUNELLE_MAX_CONCURRENCY`；查供应商配额 |
| server_error | 5xx | 是 | 等自动退避；持续则查供应商状态页 |
| auth_invalid | 401/403 | 否 | 换有效密钥后对失败任务手动重试 |
| config_error | 模型不存在/配置错 | 否 | 核对 `LUNELLE_IMAGE_MODEL`（注意：部分聚合端点对未知模型回 503，本系统已按响应体识别） |
| content_policy | 内容审核拒绝 | 否 | 调整款式描述/avoid 列表后 force 重新生成 |
| bad_request | 请求被拒 | 否 | 看 error_message 前 600 字节的服务端说明 |
| invalid_response | 返回体异常 | 否 | 供应商兼容性问题，附 external_request_id 联系供应商 |
| download_failed | 图片下载失败 | 是 | 网络/CDN 问题 |
| file_write_failed / disk_full | 落盘失败 | 否 | 检查磁盘空间与 outputs 目录权限 |
| interrupted | 服务重启打断 | 是 | 无需处理，自动重试 |
| internal | 未预期异常 | 否 | 按日志中 error_id/exc 排查并提 issue |

## 常见场景

**任务一直 pending**：worker 没起来（`/ready` 的 worker_alive=false）→ 看启动日志；
或 wearing 在等 grid（wait_for 机制，grid 结束后自动继续）。

**任务停在 retrying**：正常退避等待（next_attempt_at 字段是下次执行时间）。

**重启后有任务标 interrupted**：预期行为，说明重启时有任务在跑，已安全转入重试。

**QA passed=false 但图看起来没问题**：看 `qa.checks` 里哪项 fail 及其阈值；
`grid_nail_count`/`color_consistency` 是启发式（heuristic:true），仅供人工复核参考，
可 `POST /api/tasks/{id}/qa` 重跑。

**上传参考图 413/422**：超过 `LUNELLE_MAX_UPLOAD_MB` 或不是 PNG/JPEG/WEBP 真实图片。

**数据库锁等待**：单机多进程共库已配 busy_timeout=30s；若自建脚本直连数据库，
务必用 WAL 且别开长事务。

**误删输出文件**：DB 记录仍在（output_path 指向缺失文件，图片接口 404）；
对该任务 force 重新生成新版本即可，或从备份恢复 outputs/。
