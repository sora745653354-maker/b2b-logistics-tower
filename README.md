# B2B 物流控制塔：本机工作流报表监控

## 本机当日工作流目录监控（2026-10-09，北京时间）

当前数据源不再由 Codex 小时任务操作 TMS 导出，而是由本机 watcher 每 30 秒检查 `/Users/harumi/backup/b2b订单明细-files` 下的北京时间当日工作流目录。任务 `tms` 已暂停。目录支持 `M月D日工作流` 和 `M月DD日工作流` 两种命名；只选择当日目录内最新的 SOO 订单明细 XLSX/CSV 文件，经过行数、日期和数据质量校验后导入 SQLite，并由本机 API 和页面展示。没有文件或文件未通过校验时保留上一次成功数据，绝不回退到前一天目录。操作规程见 [TMS_HOURLY_RUNBOOK.md](ingestion/TMS_HOURLY_RUNBOOK.md)。

本机需保持开机且用户会话可用；无需自动操作 Chrome 或登录 TMS。页面每 60 秒读取数据库。新文件导入成功后更新本机数据；目录为空或导入失败时展示监控状态并保留上一成功批次。

## 历史私有 Web 应用（当前暂停部署与推送）

Sites 版已发布到 <https://b2b-logistics-tower.almondtoast.chatgpt.site>，仅当前所有者可访问。源代码在同级 `logistics-tower-site/`；页面与只读 MCP 查询共用线上快照。发布包内置本机成功批次 #2 的 270 条非联系人 SOO 记录；原始 Excel、联系人、电话、地址、Cookie 和本机 SQLite 文件均未打包。只读插件已由用户连接并读回批次 #2；“超时待提”口径修正后，线上插件返回 891 件，与页面一致。数据超过 60 分钟没有新的 TMS 入库批次时，页面和插件将标注延迟。

`ingestion/push_site_snapshot.py` 和 `infra/com.babycare.logistics-tower.site-uploader.plist` 是历史远程推送准备。之前访问站点遇到 Cloudflare HTTP 403，远程上传读回未成功，上传任务未启用。按用户最新要求，暂停 Sites 与 Netlify 部署及推送；下述小时任务只更新本机数据库与本机页面，不更新线上初始快照。

## 当前状态（2026-10-09）

本机数据库驱动页面：<http://127.0.0.1:4174/>。最新成功批次 #6，427 条有效 SOO。当前 `10月9日工作流` 目录尚无 SOO 报表，因此页面继续展示该批次并提示等待当日文件。ODS 原始行、批次快照、SOO 当前态分层保存，原件按 SHA256 归档。大屏只获取非联系人字段。

目录由用户或其他流程维护；watcher 不创建文件、不执行 TMS 导出，也不将历史文件伪装成新采集。若同一日期的补零/不补零目录同时存在，优先选择含报表的目录；文件以修改时间最新者为准，并等待文件大小稳定后进入导入与质量校验。可用 `TMS_DOWNLOAD_DIR` 更改监控根目录。

## 本机运行与检查

在 `/Users/harumi/Documents/Codex/2026-10-08/qin-2` 下启动：

```bash
control-tower/.venv/bin/python -m uvicorn backend.sqlite_app:app --app-dir control-tower --host 127.0.0.1 --port 4174
control-tower/.venv/bin/python control-tower/ingestion/watch_tms_exports.py
```

以上两条仅用于手动兜底，不要与已运行的服务重复启动。两个用户级自启动配置已安装在 `/Users/harumi/Library/LaunchAgents/`。API 曾因直接执行虚拟环境脚本遇到权限错误而重启循环，现已改为 Python 模块启动并重新加载。两个服务均已由 launchd 启动、重启并验证；本次未进行设备重启后的登录测试。检查：

```bash
launchctl print gui/501/com.babycare.logistics-tower.api
launchctl print gui/501/com.babycare.logistics-tower.watcher
```

接口与自动化回归检查：

```bash
curl -fsS http://127.0.0.1:4174/api/v1/health
curl -fsS http://127.0.0.1:4174/api/v1/sync/status
PYTHONPATH=control-tower control-tower/.venv/bin/pytest -q control-tower/tests
```

API：`GET /data.json`、`GET /api/v1/health`、`GET /api/v1/sync/status`、`GET /api/v1/imports`、`GET /api/v1/snapshots/{batch_id}`、`POST /api/v1/imports/upload`。数据质量页提供本地人工上传。上传走与监听器同一校验、幂等及快照流水线。

## 筛选器区域与线路关系

`dist/warehouse-carriers.json` 同时保存线路承运商关系和仓库区域映射（不修改原工作簿）。更新线路承运商关系：

```bash
control-tower/.venv/bin/python control-tower/ingestion/build_origin_routes.py "/path/to/各始发区域线路信息表.xlsx"
```

驾驶舱业务日期筛选仅显示北京时间今天及前两天（最近 3 个日历日），未来日期及更早日期不提供选择。统仓区域、仓库、承运商和渠道均支持多选、全选、反选及模糊搜索；筛选结果同步应用于指标、图表、明细、下载和 BI 报表。仓-区域映射表由 `ingestion/build_warehouse_regions.py` 导入 `dist/warehouse-carriers.json`，不会覆盖已有线路承运商关系。选择区域会收窄仓库、承运商、渠道选项；未出现在映射表中的源仓单独归入“未映射仓库”。区域列表固定展示映射表中的所有区域，即使当前业务日期暂时没有该区域的订单。

```bash
control-tower/.venv/bin/python control-tower/ingestion/build_warehouse_regions.py "/path/to/仓-区域映射.xlsx"
```

选择单一仓库后，承运商选项仍从线路表对应始发仓的承运商中筛出，并与当前筛选下的 TMS 承运商取交集；TMS 仓名与线路表始发仓通过页面中的显式别名对应，未确认别名不会猜配或放出全体承运商选项。

## 运营 BI 报表

顶部“生成并下载 BI 报表”会根据业务日期、仓库、承运商和渠道生成独立 HTML 文件。分析结构包含经营摘要、计划/完成组合图、履约状态环图、仓库规模对比、承运商达成子弹图、渠道结构、链路覆盖及 P50/P90、仓库×承运商超时矩阵和 SOO 明细。图表为内嵌 SVG，无需联网；明细支持搜索、条件筛选和 CSV 下载。打印/保存 PDF 输出分析摘要，完整明细保留在 HTML/CSV。指标口径与驾驶舱一致，模块内部的风险条件不限制全局报表。

端到端验证脚本 `tests/verify_bi_report.cjs` 覆盖真实批次的页面生成下载、离线查看、明细筛选、CSV、移动端布局和打印。验证产物保存在 `outputs/bi-report/`。

## 安全与边界

- API 仅绑定 `127.0.0.1`，不能直接供其他机器访问。没有 SSO/角色授权时不得改成 `0.0.0.0` 或公开部署。
- `runtime/` 及原文件归档权限为 700，数据库文件权限为 600；文件中可能含联系人、电话、地址等敏感内容，不要打包或上传。
- 报表页显示当日监控目录、目录内最新文件及最近成功入库批次。目录暂无文件时沿用上一成功数据并显示等待状态；超过 60 分钟没有新文件成功入库时显示延迟。失败状态保留上一成功数据并记录失败阶段和原因。
- TMS 查询当前是“计划生产日期”范围，不能假定覆盖全量未关闭订单；上线正式口径前须确认回溯窗口、更新字段和全部历史补采策略。
- 提货完成以 TMS“状态”字段为准：`开放`、`委托`、`确认` 为未提，其余非空状态为已提；缺失状态不推定为已提。实际提货时间仅保留为事件时间及数据质量核对字段，不覆盖状态判定。承运商达成统计按所选业务日对应的前一日生产计划匹配当前物流状态。无承运商运力输入，因此不展示能力缺口预测。

## PostgreSQL 迁移准备

`migrations/001_init.sql` 和 `backend/app.py` 是 PostgreSQL 版结构与 API 草案；PostgreSQL 17.11 官方源码已编译在同级 `work/pgsql/`，`backend/setup_local.py` 负责初始化。Codex 的当前 macOS 执行沙箱禁止 `shmget`，导致 `initdb` 无法在此环境完成，因此**当前运行数据库是 SQLite，不应声称 PostgreSQL 已部署**。后续需在正常系统终端或受管内网服务器完成 PostgreSQL 初始化、将现有批次迁移并重新验证，再切换 API。不要对已有 SQLite 数据目录执行清除操作。

当前监控依赖本机文件系统 watcher，不是 TMS 实时接口或独立服务器采集器。若未来恢复自动导出或服务器化，应另行确认专用只读报表账号、TMS 允许的导出频率与正式查询范围，优先采用受支持接口，不复制个人 Cookie 或绕过验证码。
