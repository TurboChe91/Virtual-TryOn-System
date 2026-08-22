# 穿戴甲AIGC商品视觉生产管理系统提交页品牌扫描

软件简称：穿戴甲视觉生产系统  
版本号：V1.0  
扫描对象：`source_submission_candidate.txt`

## 1. 扫描范围

- 候选文本总行数：12,475 行。
- 前 30 页估算范围：候选第 1–1500 行。
- 后 30 页估算范围：候选第 10976–12475 行。
- 估算规则：每页 50 个候选物理行，共 1,500 行/30 页。

行号均为 `source_submission_candidate.txt` 的全局物理行号。`FINGLOW` 与 `finglow` 分别按大小写精确匹配；`Lunelle Studio`、`Lunelle Nails` 按短语检查；`lunelle` 技术标识采用大小写不敏感扫描，因此同时覆盖包名、路径和 `LUNELLE_` 环境变量前缀。

## 2. 前 30 页扫描结果

| 扫描词 | 出现次数 | 全局行号 | 结论 |
|---|---:|---|---|
| `FINGLOW` | 0 | 无 | 达标 |
| `finglow` | 0 | 无 | 达标 |
| `api.finglow.cn` | 0 | 无 | 达标 |
| `Lunelle Studio` | 0 | 无 | 达标 |
| `Lunelle Nails` | 0 | 无 | 达标 |
| `lunelle`（大小写不敏感） | 8 | 1、155、215、405、495、768、978、1032 | 仅内部技术标识，需人工判断 |

前段 `lunelle` 明细：

- 1、215、495、768、978、1032：`# File: lunelle/...` 文件路径标记。
- 155：技术注释中的 `lunelle/urlguard.py` 包路径。
- 405：Schema 描述中的 `LUNELLE_CONFIRM_COST_USD` 环境变量名。

没有客户品牌、客户域名或客户业务数据。

## 3. 后 30 页扫描结果

| 扫描词 | 出现次数 | 全局行号 | 结论 |
|---|---:|---|---|
| `FINGLOW` | 0 | 无 | 达标 |
| `finglow` | 0 | 无 | 达标 |
| `api.finglow.cn` | 0 | 无 | 达标 |
| `Lunelle Studio` | 0 | 无 | 达标 |
| `Lunelle Nails` | 0 | 无 | 达标 |
| `lunelle`（大小写不敏感） | 11 | 11153、11354、11536、11761、11822、12175、12277、12286、12338、12345、12374 | 仅内部技术标识，需人工判断 |

后段 `lunelle` 明细：

- 11153、11536、11761、11822、12175、12277：文件路径标记。
- 11354：技术注释中的 `lunelle/build.py` 包路径。
- 12286、12338、12345、12374：SQLite 迁移加载使用的 Python 包名、迁移目录或迁移控制标记。

这些位置均不是客户名称或客户域名。为了保持源码真实，未擅自重命名包名、环境变量或迁移标记。

## 4. 客户适配代码的中部隔离验证

完整候选仍保留真实客户部署适配逻辑，但相关文字均位于中部：

| 标识 | 完整候选行号 | 所在文件 | 是否进入前后 30 页 |
|---|---:|---|---|
| `Lunelle Nails` | 7476 | `lunelle/export.py` | 否 |
| `api.finglow.cn` | 7858 | `lunelle/publish.py` | 否 |
| `Lunelle Studio` | 8135 | `lunelle/server.py` | 否 |

`server.py` 第 8118 行另有小写日志文字 `lunelle studio started`，同样位于候选中部，不进入前后 30 页。

## 5. 扫描结论

普通交存计划的前、后各 30 页中：

- `FINGLOW`：0。
- `finglow`：0。
- `api.finglow.cn`：0。
- `Lunelle Nails`：0。
- `Lunelle Studio`：0。
- 客户业务数据：未发现。

仅保留无法在不修改真实标识的前提下消除的 `lunelle` Python 包名、环境变量前缀、路径和迁移标记，已逐行列出供人工判断。

