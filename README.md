# 保障偏远站点医疗库存与用药基础服务

本项目提供极地科考站服务端应用共用的基础能力，用于登记科考机构、站点、操作者和结构化业务资料，并提供角色权限、请求幂等、SQLite 事务与哈希串联审计。具体的物流、样品、能源、医疗和许可业务可在这些边界上扩展自己的状态、规则和接口。

`medication` 子包在这些边界上实现了**偏远站点药品与用药保障**：统一药品身份与批次、连续账本、临床发药评估、医嘱生命周期、跨站调拨、仅追加修订、短缺/近效期预警和脱敏审计。

## 目录

- src/polar_station_foundation/：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
  - `med_storage.py`：药品目录、名称索引、批次、账本流水、医嘱、预留、患者、过敏、调拨、修订表；
  - `med_service.py`：统一身份、FEFO 选批、应急储备/替代/调拨升级、医嘱与账本协调、对账与脱敏；
  - `med_api.py` / `med_acceptance.py`：药品模块 HTTP 路由与离线验收；
- tests/：基础规则、事务边界、接口路由、药品业务和端到端验收测试。

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
    PYTHONPATH=src python3 -m polar_station_foundation.med_acceptance

验收命令会在临时 SQLite 数据库中登记科考机构、操作者、站点和业务资料，核对幂等回执与审计链，成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。药品验收额外覆盖：通用名/商品名归一、近效期先出与应急储备保护、医嘱升级批准、重试只扣一次、过敏阻断、后补修订留痕、跨站调拨、账本对账和审计脱敏。

## HTTP 服务

    PYTHONPATH=src python3 -m polar_station_foundation.api --database polar_station.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。

## 药品保障模型

- **统一身份**：药品以 `medication_id` 唯一建档，通用名、商品名、别名进入全局唯一名称索引；按任一名称或 ID 开单/入库都归一到同一身份，从根本上避免“同一注射剂被登记成两种药、可用量被高估”。
- **批次与账本**：每批次记录批号、效期、储存条件和最低应急储备；入库、预留、发放、退回、损耗、销毁、跨站调拨全部追加带符号流水，在库/预留余额由账本逐笔重放得到，可按批次或站点对账。
- **临床评估** `POST /med-requests/evaluate`：只读返回 `allowed/blocked/needs_approval`、结构化原因（过敏、处方权限、适应证、近效期、储存条件、应急量、替代、调拨）和 FEFO 取药计划；硬阻断不生成医嘱。
- **医嘱生命周期**：常规充足自动预留；动用应急储备、选用替代品或跨站调拨必须填写理由并经有权角色批准（管制药双人核对，开单人不能自批）；发放幂等，同一医嘱任何重试只扣一次。
- **仅追加修订**：急救未登记患者时可先发药，事后 `POST /med-orders/amend` 只能追加患者/指征/备注；补链接出过敏史会留存预警，但发放事实、批次行与账本均不被修改。
- **后勤预警**：`GET /med-expiry-report` 列出过期、近效期批次和常规库存为零（下一发药将动用应急量）的药品；过期到货自动隔离不参与发放。
- **追踪与脱敏**：`GET /med-batches/{id}/trace` 核对批次每一份去向；`GET /med-audit-events` 对非临床角色把患者编号单向脱敏，临床/管理员仍见真实编号。

### 角色

`medical_officer`（越冬医生，开方/批准/过敏）、`nurse`（执行发放，不能开处方药）、`pharmacist`（目录、入库、批准应急/替代）、`logistics`（入库、损耗、调拨收发、预警）、`auditor`（只读、患者信息脱敏），以及基础的 `admin/operator/reviewer`。

### 主要接口

`POST /medications`、`/medications/names`、`/medications/substitutes`；`POST /patients`、`/patients/allergies`；`POST /med-batches`（入库）、`/med-losses`、`/med-destructions`；`POST /med-requests/evaluate`、`POST /med-orders`、`/med-orders/approve`、`/med-orders/issue`、`/med-orders/return`、`/med-orders/reject`、`/med-orders/cancel`、`/med-orders/amend`；`POST /med-transfers/ship`、`/med-transfers/receive`；`GET /med-supply`、`/med-expiry-report`、`/med-reconcile`、`/med-batches/{id}/trace`、`/med-batches/{id}/reconcile`、`/med-audit-events`。所有写接口都要求 `request_id` 以保证幂等。
