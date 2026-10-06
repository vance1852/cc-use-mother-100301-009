# 保障偏远站点医疗库存与用药基础服务

本项目提供极地科考站服务端应用共用的基础能力，用于登记科考机构、站点、操作者和结构化业务资料，并提供角色权限、请求幂等、SQLite 事务与哈希串联审计。在此之上，`pharmacy` 模块为偏远站点建立完整的药品与用药保障项目。

## 药品与用药保障能力

- **统一药品身份与批次**：目录以「通用名 + 剂型 + 规格」唯一识别，商品名/别名经 `medication-aliases` 归并到同一身份；同一注射剂按商品名和通用名入库不会重复计数。批次记录批号、效期与储存条件。
- **站点储存条件**：常温/冷藏/冷冻能力按站点登记；入库、调入时校验站点是否具备对应储存条件。
- **连续库存账本**：入库、预留、预留释放、发放、退回、损耗、销毁、调拨出/调入逐笔写入 `inventory_ledger`，每笔带发生后余额与预留量，可逐批重放核对。
- **发放决策（医生立即可知为何获准或阻止）**：
  - 适应证是否在登记范围；
  - 患者过敏（同时匹配通用名、编号与商品名/别名）；
  - 开方者管制药权限等级；
  - 批次在效、未隔离、站点具备储存条件；
  - FEFO（近效期先发）；
  - 最低应急储备保底，常规处方最后才动用应急量；
  - 可替代关系：本药常规量不足时改走合格替代品，本药过敏可自动绕开。
- **批准留痕**：动用应急储备、选用近效期替代品、跨站调拨都必须出示带理由的批准；批准单次使用、不可复用。
- **同一医嘱只扣减一次**：`dispensations.prescription_id` 唯一，重试或换请求号重发都返回原始发放事实。
- **后补信息只追加**：诊疗修订写入独立追加表，原处方与发放事实不可变。
- **后勤风险预警**：低于补货点、应急储备不达标、近效期（90 天内）与已过期批次报告。
- **审计隐私**：审计员视图用不可逆假名替换患者身份且不含姓名/过敏明细，同时保留每个批次的完整去向链（发放、退回、调拨）。

角色：`admin`、`physician`（医生）、`logistician`（后勤）、`operator`、`reviewer`、`auditor`。

## 主要 HTTP 接口

写入接口均通过 `X-Actor-Id` 标识操作者，并以 `request_id` 幂等：

- `POST /medications`、`/medication-aliases`、`/medication-substitutes`
- `POST /site-storage`、`/medication-policies`、`/prescriber-profiles`、`/patients`
- `POST /approvals`
- `POST /batches`（入库）、`/prescriptions`、`/prescription-amendments`
- `POST /reservations`（预留）、`/reservation-releases`、`/dispensations`（发放）
- `POST /returns`（退回）、`/losses`（损耗）、`/destructions`（销毁）
- `POST /transfers`（跨站调拨）、`/transfer-receptions`（接收）
- `GET /prescription-decision?prescription_id=`：不放行任何库存，返回允许/阻止原因与批次计划
- `GET /site-inventory`、`/shortage-risks`
- `GET /batch-destinations?batch_id=`：批次去向核对（审计角色自动脱敏）
- `GET /pharmacy-audit-events`：审计链（审计角色自动脱敏）
- `GET /ledger-verification`：重放并核对连续账本

## 目录

- src/polar_station_foundation/：基础模型、SQLite 存储、权限服务、审计链、HTTP 路由，以及药品保障服务（`pharmacy.py`）；
- tests/：基础规则、药品决策与账本、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m polar_station_foundation.acceptance
    PYTHONPATH=src python3 -m polar_station_foundation.pharmacy_acceptance

验收命令在临时 SQLite 数据库中跑通完整链路，成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m polar_station_foundation.api --database polar_station.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。
