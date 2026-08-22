# UI Rebrand 变更报告

- 软件登记版本：穿戴甲AIGC商品视觉生产管理系统 V1.0
- 界面展示名称：穿戴甲视觉生产系统
- 原界面展示名称：Lunelle Studio
- 执行日期：2026-08-22

## 1. 变更结论

本次仅完成用户可见软件名称的 UI rebrand：将界面和用户可见启动说明中作为产品名展示的 `Lunelle Studio` 统一替换为“穿戴甲视觉生产系统”。为避免中文名称在侧栏和空缩略图中溢出，仅同步调整了对应品牌文字的字号。

本次没有修改核心业务逻辑、数据库结构、API 路径、任务逻辑、Worker 逻辑、QA 逻辑、Provider 逻辑或发布逻辑；没有重命名 `lunelle` Python 包、文件路径、import 或 `LUNELLE_*` 环境变量。

## 2. 本次修改文件

| 文件 | UI rebrand 内容 |
|---|---|
| `.env.example` | 将环境配置模板顶部的用户可见产品名改为“穿戴甲视觉生产系统”。 |
| `README.md` | 将启动与使用说明首页的软件标题改为“穿戴甲视觉生产系统”。 |
| `docs/DEPLOYMENT.md` | 将 systemd 示例中用户可见的服务描述改为“穿戴甲视觉生产系统”。技术路径和服务文件名保持不变。 |
| `lunelle/server.py` | 将 FastAPI/OpenAPI 文档可见标题改为“穿戴甲视觉生产系统”。API 路径和路由逻辑保持不变。 |
| `lunelle/web/index.html` | 修改浏览器标题、侧栏顶部软件名称和空缩略图品牌占位文字；仅为新中文名称调整相应字号。 |
| `lunelle/web/settings.html` | 修改设置页浏览器标题和侧栏顶部软件名称；仅为新中文名称调整相应字号。 |
| `copyright_materials_v2/ui_rebrand_report.md` | 新增本报告。 |

## 3. 边界复核

本次明确未修改以下内容：

- Python 包名 `lunelle`、`pyproject.toml` 中的技术包名 `lunelle-studio`、import 和文件路径；
- 所有 `LUNELLE_*` 环境变量名；
- 数据库结构、数据库访问逻辑和 `lunelle/migrations/` 历史迁移；
- API 路径、请求/响应结构和鉴权协议；
- 任务、Worker、QA、Provider、发布和导出业务逻辑；
- 内部日志技术标识 `lunelle studio started`；
- `lunelle/__init__.py` 与 `lunelle/cli.py` 中的内部技术文档字符串；
- 历史软著扫描、源码候选和审计快照中的既有记录。

复扫结果显示，运行代码中旧名称仅保留在上述内部技术文档字符串和内部启动日志中；面向用户的 HTML 标题、侧栏标题、设置页标题、API 文档标题及启动说明展示名均已更新。

## 4. 工作区说明

任务开始前工作区已经存在未提交的后端、前端、测试、迁移和软著材料改动。本次操作保留了这些既有改动，没有回滚、覆盖或将其归入 UI rebrand。`lunelle/server.py` 和 `lunelle/web/index.html` 与既有改动重叠，但本次在这两个文件中只新增了上表所列的软件名称/展示字号调整。

因此，“仅发生 UI rebrand”指本次操作产生的增量，不代表任务开始前的整个工作区原本是干净状态。

## 5. 验证结果

执行仓库现有完整验证脚本：

```text
./scripts/test.sh
```

结果：

- Ruff：通过（`All checks passed!`）；
- mypy：通过（`Success: no issues found in 37 source files`）；
- pytest：`661 passed, 2 skipped, 1 warning in 451.63s (0:07:31)`；
- 唯一警告来自测试环境中 FastAPI/Starlette TestClient 的依赖弃用提示，与本次 UI rebrand 无关。

另执行 `git diff --check`，结果通过，未发现空白符错误。

## 6. 发布与版本控制

- 未部署生产环境；
- UI rebrand 完成时未执行普通分支 commit 或 push；
- 根据后续检查要求，最终版本通过独立的 `copyright` 标签快照交付，现有普通分支指针不推进。

## 7. 最终确认

本次增量变更符合“只修改用户界面可见的软件产品名称”的要求，可用于“穿戴甲AIGC商品视觉生产管理系统 V1.0”软件著作权登记截图版本准备。
