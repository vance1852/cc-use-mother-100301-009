"""偏远站点药品与用药保障领域服务。

在基础身份、场所、幂等与审计能力之上实现：

- 药品统一身份（通用名/商品名/别名归一）与可替代关系；
- 按批次管理效期、储存条件与最低应急储备；
- 入库、预留、发放、退回、损耗、销毁、跨站调拨的连续账本；
- 医嘱评估（FEFO、过敏、适应证、处方权限、应急量、替代、调拨）、
  批准留痕、幂等发放与仅追加修订；
- 后勤短缺/近效期预警、批次去向追踪与脱敏审计查询。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, timedelta
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    PolicyDenied,
    ValidationError,
)
from .models import Actor
from .storage import Database

NEAR_EXPIRY_DAYS = 30

PRESCRIBER_ROLES = frozenset({"medical_officer", "pharmacist"})
CLINICAL_ROLES = frozenset({"medical_officer", "nurse", "pharmacist"})
CATALOG_ROLES = frozenset({"pharmacist", "admin"})
RECEIVE_ROLES = frozenset({"pharmacist", "logistics", "admin"})
APPROVER_ROLES = frozenset({"medical_officer", "pharmacist", "admin"})
TRANSFER_APPROVER_ROLES = frozenset({"admin", "operator", "medical_officer"})

# 流水对在库数量的符号：预留类只改 reserved，不在库数量。
MOVEMENT_SIGN = {
    "receive": 1,
    "return": 1,
    "transfer_in": 1,
    "issue": -1,
    "loss": -1,
    "destroy": -1,
    "transfer_out": -1,
    "reserve": 0,
    "release_reservation": 0,
}

STORAGE_CONDITIONS = frozenset({"room", "cool", "refrigerated", "frozen"})


def _mask_patient(patient_id: str) -> str:
    return "pat-" + hashlib.sha256(patient_id.encode("utf-8")).hexdigest()[:10]


class MedicationService:
    """药品目录、批次库存、医嘱与调拨的协调服务。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _today(self) -> date:
        return self.clock.now().date()

    def _date(self, value: str, field: str) -> str:
        try:
            parsed = date.fromisoformat(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期") from exc
        return parsed.isoformat()

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _site(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    def _require_site_access(self, actor: Actor, site_row) -> None:
        if actor.organization_id != site_row["organization_id"] and actor.role != "admin":
            raise PermissionDenied("不能操作其他组织的场所")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create: Callable[[], tuple[str, str, dict[str, Any]]]):
        request_id = str(request_id).strip()
        if not request_id:
            raise ValidationError("request_id 不能为空")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {
                "request_id": request_id,
                "resource_type": row["resource_type"],
                "resource_id": row["resource_id"],
                "replayed": True,
                "response": json.loads(row["response_json"]),
            }
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {
            "request_id": request_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "replayed": False,
            "response": response,
        }

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        # 药品审计明细只记录不透明患者编号，绝不记录姓名等身份信息。
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    def _resolve_medication(self, connection, key: str):
        """按药品身份 ID 或任一已登记名称（通用名/商品名/别名）解析统一身份。"""

        key = str(key).strip()
        if not key:
            raise ValidationError("medication 不能为空")
        row = connection.execute(
            "SELECT * FROM med_catalog WHERE medication_id=?", (key,)
        ).fetchone()
        if row:
            return row
        row = connection.execute(
            "SELECT m.* FROM med_catalog m JOIN med_catalog_names n ON n.medication_id=m.medication_id "
            "WHERE n.name=? COLLATE NOCASE",
            (key,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"未找到药品：{key}")
        return row

    def _substitutes(self, connection, medication_id: str) -> list[str]:
        rows = connection.execute(
            "SELECT substitute_id FROM med_substitutes WHERE medication_id=?",
            (medication_id,),
        ).fetchall()
        return [row["substitute_id"] for row in rows]

    def _patient_allergies(self, connection, patient_id: str | None) -> dict[str, str]:
        if not patient_id:
            return {}
        rows = connection.execute(
            "SELECT medication_id, severity FROM med_patient_allergies WHERE patient_id=?",
            (patient_id,),
        ).fetchall()
        return {row["medication_id"]: row["severity"] for row in rows}

    # ----------------------------------------------------------- 目录与患者

    def register_medication(self, *, request_id: str, actor_id: str, medication_id: str,
                            generic_name: str, form: str, strength: str, route: str,
                            storage_condition: str = "room", controlled: bool = False,
                            prescription_only: bool = True, indications: list[str] | None = None,
                            trade_names: list[str] | None = None,
                            aliases: list[str] | None = None) -> dict[str, Any]:
        """登记药品统一身份及全部名称，避免同一药品被重复建档。"""

        indications = indications or []
        trade_names = trade_names or []
        aliases = aliases or []
        payload = locals_kwargs(locals(), exclude={"self"})
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *CATALOG_ROLES)
            medication_id = str(medication_id).strip()
            generic_name = str(generic_name).strip()
            if not medication_id or not generic_name:
                raise ValidationError("medication_id 与 generic_name 不能为空")
            if storage_condition not in STORAGE_CONDITIONS:
                raise ValidationError("storage_condition 不在允许范围内")
            for list_field, values in (("indications", indications),
                                       ("trade_names", trade_names), ("aliases", aliases)):
                if not all(isinstance(v, str) and v.strip() for v in values):
                    raise ValidationError(f"{list_field} 必须是非空字符串列表")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO med_catalog(medication_id,generic_name,form,strength,route,"
                        "storage_condition,controlled,prescription_only,indications_json,"
                        "created_by,created_at,version) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
                        (medication_id, generic_name, form, strength, route, storage_condition,
                         int(controlled), int(prescription_only), canonical_json(indications),
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("药品编号已经存在") from exc
                self._insert_name(connection, medication_id, generic_name, "generic")
                for name in trade_names:
                    self._insert_name(connection, medication_id, name, "trade")
                for name in aliases:
                    self._insert_name(connection, medication_id, name, "alias")
                self._audit(connection, actor_id=actor_id, action="medication.registered",
                            resource_type="medication", resource_id=medication_id,
                            detail={"generic_name": generic_name, "form": form, "strength": strength,
                                    "controlled": bool(controlled), "trade_names": trade_names,
                                    "aliases": aliases})
                response = {"medication_id": medication_id, "generic_name": generic_name,
                            "names": [generic_name, *trade_names, *aliases]}
                return "medication", medication_id, response

            return self._idempotent(connection, request_id=request_id,
                                    action="register_medication", payload=payload, create=create)

    def _insert_name(self, connection, medication_id: str, name: str, kind: str) -> None:
        try:
            connection.execute(
                "INSERT INTO med_catalog_names(medication_id,name,name_kind) VALUES(?,?,?)",
                (medication_id, name.strip(), kind),
            )
        except Exception as exc:
            raise ConflictError(f"药品名称已被其他身份占用：{name}") from exc

    def add_medication_name(self, *, request_id: str, actor_id: str, medication_id: str,
                            name: str, kind: str = "alias") -> dict[str, Any]:
        """为既有药品身份追加名称（如发现商品名曾被误当作独立药品）。"""

        payload = {"actor_id": actor_id, "medication_id": medication_id, "name": name, "kind": kind}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *CATALOG_ROLES)
            if kind not in {"generic", "trade", "alias"}:
                raise ValidationError("kind 必须是 generic/trade/alias")
            med = connection.execute(
                "SELECT 1 FROM med_catalog WHERE medication_id=?", (medication_id,)
            ).fetchone()
            if med is None:
                raise NotFoundError("药品不存在")

            def create():
                self._insert_name(connection, medication_id, name, kind)
                self._audit(connection, actor_id=actor_id, action="medication.name_added",
                            resource_type="medication", resource_id=medication_id,
                            detail={"name": name, "name_kind": kind})
                return "medication_name", f"{medication_id}:{name}", {
                    "medication_id": medication_id, "name": name, "name_kind": kind}

            return self._idempotent(connection, request_id=request_id,
                                    action="add_medication_name", payload=payload, create=create)

    def add_substitute(self, *, request_id: str, actor_id: str, medication_id: str,
                       substitute_id: str, note: str = "") -> dict[str, Any]:
        """登记双向可替代关系。"""

        payload = {"actor_id": actor_id, "medication_id": medication_id,
                   "substitute_id": substitute_id, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *CATALOG_ROLES, "medical_officer")
            if medication_id == substitute_id:
                raise ValidationError("药品不能替代自身")
            for mid in (medication_id, substitute_id):
                if connection.execute("SELECT 1 FROM med_catalog WHERE medication_id=?", (mid,)).fetchone() is None:
                    raise NotFoundError(f"药品不存在：{mid}")

            def create():
                for left, right in ((medication_id, substitute_id), (substitute_id, medication_id)):
                    connection.execute(
                        "INSERT INTO med_substitutes(medication_id,substitute_id,note,created_by,created_at) "
                        "VALUES(?,?,?,?,?) ON CONFLICT(medication_id,substitute_id) DO NOTHING",
                        (left, right, note, actor_id, self._now()),
                    )
                self._audit(connection, actor_id=actor_id, action="medication.substitute_linked",
                            resource_type="medication", resource_id=medication_id,
                            detail={"substitute_id": substitute_id, "note": note})
                return "medication_substitute", f"{medication_id}->{substitute_id}", {
                    "medication_id": medication_id, "substitute_id": substitute_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="add_substitute", payload=payload, create=create)

    def register_patient(self, *, request_id: str, actor_id: str, patient_id: str,
                         site_id: str, display_name: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "patient_id": patient_id, "site_id": site_id,
                   "display_name": display_name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *CLINICAL_ROLES, "admin")
            site = self._site(connection, site_id)
            self._require_site_access(actor, site)

            def create():
                try:
                    connection.execute(
                        "INSERT INTO med_patients(patient_id,site_id,display_name,active,"
                        "created_by,created_at,version) VALUES(?,?,?,1,?,?,1)",
                        (patient_id, site_id, display_name, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("患者编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="patient.registered",
                            resource_type="patient", resource_id=patient_id,
                            detail={"site_id": site_id})
                return "patient", patient_id, {"patient_id": patient_id, "site_id": site_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_patient", payload=payload, create=create)

    def add_allergy(self, *, request_id: str, actor_id: str, patient_id: str,
                    medication: str, severity: str, note: str = "") -> dict[str, Any]:
        """登记患者对某药品身份（含其名称）的过敏及严重程度。"""

        payload = {"actor_id": actor_id, "patient_id": patient_id, "medication": medication,
                   "severity": severity, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "medical_officer", "admin")
            if severity not in {"mild", "moderate", "severe"}:
                raise ValidationError("severity 必须是 mild/moderate/severe")
            patient = connection.execute(
                "SELECT * FROM med_patients WHERE patient_id=?", (patient_id,)
            ).fetchone()
            if patient is None:
                raise NotFoundError("患者不存在")
            med = self._resolve_medication(connection, medication)

            def create():
                connection.execute(
                    "INSERT INTO med_patient_allergies(patient_id,medication_id,severity,note,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(patient_id,medication_id) DO UPDATE SET "
                    "severity=excluded.severity,note=excluded.note",
                    (patient_id, med["medication_id"], severity, note, actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="patient.allergy_recorded",
                            resource_type="patient", resource_id=patient_id,
                            detail={"medication_id": med["medication_id"], "severity": severity})
                return "patient_allergy", f"{patient_id}:{med['medication_id']}", {
                    "patient_id": patient_id, "medication_id": med["medication_id"],
                    "severity": severity}

            return self._idempotent(connection, request_id=request_id,
                                    action="add_allergy", payload=payload, create=create)

    # ------------------------------------------------------------- 批次入库

    def receive_batch(self, *, request_id: str, actor_id: str, site_id: str, medication: str,
                      lot: str, expiry_date: str, quantity: int, emergency_reserve: int = 0,
                      storage_condition: str | None = None,
                      source_transfer_id: str | None = None) -> dict[str, Any]:
        """药品到货入库，按统一身份与批次登记，建立起始账本流水。"""

        payload = {"actor_id": actor_id, "site_id": site_id, "medication": medication,
                   "lot": lot, "expiry_date": expiry_date, "quantity": quantity,
                   "emergency_reserve": emergency_reserve,
                   "storage_condition": storage_condition,
                   "source_transfer_id": source_transfer_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *RECEIVE_ROLES)
            site = self._site(connection, site_id)
            self._require_site_access(actor, site)
            med = self._resolve_medication(connection, medication)
            lot = str(lot).strip()
            expiry_date = self._date(expiry_date, "expiry_date")
            if not lot:
                raise ValidationError("lot 不能为空")
            quantity = int(quantity)
            emergency_reserve = int(emergency_reserve)
            if quantity <= 0:
                raise ValidationError("quantity 必须为正")
            if emergency_reserve < 0 or emergency_reserve > quantity:
                raise ValidationError("应急储备不能为负且不能超过入库数量")
            condition = storage_condition or med["storage_condition"]
            if condition not in STORAGE_CONDITIONS:
                raise ValidationError("storage_condition 不在允许范围内")

            def create():
                batch_id = uuid.uuid4().hex
                # 在库余额完全由账本维护：插入时为 0，由下方 receive 流水建立余额。
                connection.execute(
                    "INSERT INTO med_batches(batch_id,site_id,medication_id,lot,expiry_date,"
                    "quantity_initial,quantity_on_hand,reserved,emergency_reserve,storage_condition,"
                    "status,source_transfer_id,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,0,0,?,?,?,?,?,?)",
                    (batch_id, site_id, med["medication_id"], lot, expiry_date, quantity,
                     emergency_reserve, condition, "available", source_transfer_id,
                     actor_id, self._now()),
                )
                self._append_ledger(connection, batch_id=batch_id, site_id=site_id,
                                    movement_type="receive", quantity=quantity,
                                    transfer_id=source_transfer_id,
                                    reason=f"入库 {med['medication_id']} 批号 {lot}",
                                    actor_id=actor_id)
                if expiry_date <= self._today().isoformat():
                    # 已到效期仍登记入库但立即隔离，避免被发放。
                    connection.execute(
                        "UPDATE med_batches SET status='quarantined' WHERE batch_id=?", (batch_id,))
                self._audit(connection, actor_id=actor_id, action="medication.received",
                            resource_type="med_batch", resource_id=batch_id,
                            detail={"site_id": site_id, "medication_id": med["medication_id"],
                                    "lot": lot, "expiry_date": expiry_date, "quantity": quantity,
                                    "emergency_reserve": emergency_reserve,
                                    "source_transfer_id": source_transfer_id})
                return "med_batch", batch_id, {
                    "batch_id": batch_id, "site_id": site_id,
                    "medication_id": med["medication_id"], "lot": lot,
                    "expiry_date": expiry_date, "quantity_on_hand": quantity,
                    "emergency_reserve": emergency_reserve, "storage_condition": condition}

            return self._idempotent(connection, request_id=request_id,
                                    action="receive_batch", payload=payload, create=create)

    # --------------------------------------------------------------- 账本

    def _append_ledger(self, connection, *, batch_id: str, site_id: str, movement_type: str,
                       quantity: int, reserved_delta: int = 0, order_id: str | None = None,
                       reservation_id: str | None = None, transfer_id: str | None = None,
                       counterparty_site_id: str | None = None, reason: str = "",
                       actor_id: str) -> None:
        sign = MOVEMENT_SIGN[movement_type]
        row = connection.execute(
            "SELECT quantity_on_hand,reserved FROM med_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        new_on_hand = row["quantity_on_hand"] + sign * quantity
        new_reserved = row["reserved"] + reserved_delta
        if new_on_hand < 0 or new_reserved < 0 or new_reserved > new_on_hand:
            raise ConflictError("库存或预留余额不足，账本拒绝越界")
        connection.execute(
            "INSERT INTO med_ledger_entries(entry_id,batch_id,site_id,movement_type,quantity,"
            "reserved_delta,balance_after,reserved_after,order_id,reservation_id,transfer_id,"
            "counterparty_site_id,reason,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, batch_id, site_id, movement_type, quantity, reserved_delta,
             new_on_hand, new_reserved, order_id, reservation_id, transfer_id,
             counterparty_site_id, reason, actor_id, self._now()),
        )
        connection.execute(
            "UPDATE med_batches SET quantity_on_hand=?, reserved=? WHERE batch_id=?",
            (new_on_hand, new_reserved, batch_id),
        )

    def _batch(self, connection, batch_id: str):
        row = connection.execute("SELECT * FROM med_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        return row

    def record_loss(self, *, request_id: str, actor_id: str, batch_id: str, quantity: int,
                    reason: str) -> dict[str, Any]:
        """登记破损/遗失等损耗，必须填写原因。"""

        payload = {"actor_id": actor_id, "batch_id": batch_id, "quantity": quantity, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "pharmacist", "admin", "logistics")
            batch = self._accessible_batch(connection, actor, batch_id)
            quantity = int(quantity)
            if quantity <= 0 or not str(reason).strip():
                raise ValidationError("quantity 必须为正且 reason 必填")
            if batch["reserved"] > batch["quantity_on_hand"] - quantity:
                raise PolicyDenied("损耗数量超过未预留库存", [
                    {"code": "reserved_stock_protected", "severity": "blocker",
                     "message": "该批次存在预留，不能把预留药品记为损耗"}])

            def create():
                self._append_ledger(connection, batch_id=batch_id, site_id=batch["site_id"],
                                    movement_type="loss", quantity=quantity, reason=reason,
                                    actor_id=actor_id)
                self._deplete_if_empty(connection, batch_id)
                self._audit(connection, actor_id=actor_id, action="medication.lost",
                            resource_type="med_batch", resource_id=batch_id,
                            detail={"quantity": quantity, "reason": reason})
                return "med_ledger_loss", batch_id, {"batch_id": batch_id, "lost": quantity}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_loss", payload=payload, create=create)

    def destroy_batch(self, *, request_id: str, actor_id: str, batch_id: str,
                      quantity: int | None = None, reason: str = "") -> dict[str, Any]:
        """销毁过期或不合格药品；可整批或部分销毁，必须填写原因。"""

        payload = {"actor_id": actor_id, "batch_id": batch_id,
                   "quantity": quantity, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "pharmacist", "admin")
            batch = self._accessible_batch(connection, actor, batch_id)
            if not str(reason).strip():
                raise ValidationError("销毁原因必填")
            quantity = batch["quantity_on_hand"] if quantity is None else int(quantity)
            if quantity <= 0 or quantity > batch["quantity_on_hand"]:
                raise ValidationError("销毁数量超出在库")
            if batch["reserved"] > batch["quantity_on_hand"] - quantity:
                raise PolicyDenied("不能销毁已预留药品", [
                    {"code": "reserved_stock_protected", "severity": "blocker",
                     "message": "该批次存在未完成预留，需先释放预留"}])

            def create():
                self._append_ledger(connection, batch_id=batch_id, site_id=batch["site_id"],
                                    movement_type="destroy", quantity=quantity, reason=reason,
                                    actor_id=actor_id)
                full = quantity >= batch["quantity_on_hand"]
                new_status = "destroyed" if full else "available"
                connection.execute("UPDATE med_batches SET status=? WHERE batch_id=?",
                                   (new_status, batch_id))
                if not full:
                    self._deplete_if_empty(connection, batch_id)
                self._audit(connection, actor_id=actor_id, action="medication.destroyed",
                            resource_type="med_batch", resource_id=batch_id,
                            detail={"quantity": quantity, "reason": reason, "full": full})
                return "med_ledger_destroy", batch_id, {
                    "batch_id": batch_id, "destroyed": quantity, "status": new_status}

            return self._idempotent(connection, request_id=request_id,
                                    action="destroy_batch", payload=payload, create=create)

    def _deplete_if_empty(self, connection, batch_id: str) -> None:
        row = connection.execute(
            "SELECT quantity_on_hand FROM med_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if row["quantity_on_hand"] == 0:
            connection.execute(
                "UPDATE med_batches SET status='depleted' WHERE batch_id=? AND status='available'",
                (batch_id,))

    # ------------------------------------------------------- 临床评估与医嘱

    def _eligible_batches(self, connection, *, site_id: str, medication_id: str, today: str,
                          include_expired: bool = False) -> list[Any]:
        rows = connection.execute(
            "SELECT * FROM med_batches WHERE site_id=? AND medication_id=? AND status='available' "
            "ORDER BY expiry_date, lot, batch_id",
            (site_id, medication_id),
        ).fetchall()
        if include_expired:
            return list(rows)
        return [row for row in rows if row["expiry_date"] >= today]

    @staticmethod
    def _regular_pool(batch) -> int:
        """常规发放可取：在库减预留减最低应急储备。"""

        return max(0, batch["quantity_on_hand"] - batch["reserved"] - batch["emergency_reserve"])

    @staticmethod
    def _emergency_pool(batch) -> int:
        """应急发放可额外动用的储备。"""

        return min(batch["emergency_reserve"],
                   batch["quantity_on_hand"] - batch["reserved"])

    def evaluate_request(self, *, actor_id: str, site_id: str, patient_id: str | None,
                         medication: str, quantity: int, indication: str = "",
                         is_emergency: bool = False, allow_substitute: bool = False,
                         allow_transfer: bool = False) -> dict[str, Any]:
        """只读评估一次用药请求，立即说明获准或阻止原因及取药批次计划。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            site = self._site(connection, site_id)
            self._require_site_access(actor, site)
            med = self._resolve_medication(connection, medication)
            quantity = int(quantity)
            if quantity <= 0:
                raise ValidationError("quantity 必须为正")
            return self._evaluate(connection, actor=actor, site_id=site_id, patient_id=patient_id,
                                  med=med, quantity=quantity, indication=indication,
                                  is_emergency=is_emergency, allow_substitute=allow_substitute,
                                  allow_transfer=allow_transfer)

    def _evaluate(self, connection, *, actor: Actor, site_id: str, patient_id: str | None,
                  med, quantity: int, indication: str, is_emergency: bool,
                  allow_substitute: bool, allow_transfer: bool) -> dict[str, Any]:
        today = self._today().isoformat()
        near_cutoff = (self._today() + timedelta(days=NEAR_EXPIRY_DAYS)).isoformat()
        reasons: list[dict[str, str]] = []
        plan_items: list[dict[str, Any]] = []
        remaining = quantity
        approvals: list[str] = []
        blockers: list[str] = []

        allergies = self._patient_allergies(connection, patient_id)
        if patient_id is None:
            reasons.append({"code": "patient_missing", "severity": "info",
                            "message": "未关联患者，按急救后补流程处理，发放后须补录患者信息"})
        elif allergies.get(med["medication_id"]) == "severe":
            blockers.append("severe_allergy")
            reasons.append({"code": "severe_allergy", "severity": "blocker",
                            "message": f"患者对 {med['generic_name']} 有严重过敏史，禁止发放"})
        elif med["medication_id"] in allergies:
            approvals.append("allergy_override")
            reasons.append({"code": "allergy_override", "severity": "approval",
                            "message": f"患者对该药品有 {allergies[med['medication_id']]} 级过敏，"
                                       "发放需医生再次确认并批准"})

        # 处方权限。
        if med["controlled"] and actor.role != "medical_officer":
            blockers.append("controlled_privilege")
            reasons.append({"code": "controlled_privilege", "severity": "blocker",
                            "message": "管制药品只能由越冬医生开具"})
        elif med["prescription_only"] and actor.role not in PRESCRIBER_ROLES:
            blockers.append("prescription_privilege")
            reasons.append({"code": "prescription_privilege", "severity": "blocker",
                            "message": "处方药必须由医生或药剂师开具，护士不能直接开立"})

        indications = json.loads(med["indications_json"])
        if indication and indications and indication not in indications:
            reasons.append({"code": "indication_unlisted", "severity": "warning",
                            "message": f"指征“{indication}”不在该药品登记适应证内，请核实"})

        def fill_from(batches, *, substitute: bool, emergency: bool, pool_fn) -> None:
            nonlocal remaining
            for batch in batches:
                if remaining <= 0:
                    return
                available = pool_fn(batch)
                if available <= 0:
                    continue
                if batch["expiry_date"] <= near_cutoff:
                    reasons.append({"code": "near_expiry_first", "severity": "info",
                                    "message": f"批次 {batch['lot']} 将于 {batch['expiry_date']} 到期，"
                                               "按近效期先出优先使用"})
                if batch["storage_condition"] != med["storage_condition"] and not substitute:
                    reasons.append({"code": "storage_mismatch", "severity": "warning",
                                    "message": f"批次 {batch['lot']} 储存条件与目录不一致，发放前核查"})
                take = min(remaining, available)
                item = {"site_id": batch["site_id"], "batch_id": batch["batch_id"],
                        "medication_id": batch["medication_id"], "quantity": take,
                        "substitute": substitute, "emergency": emergency,
                        "expiry_date": batch["expiry_date"], "transfer": False}
                if substitute:
                    severity = allergies.get(batch["medication_id"])
                    if severity == "severe":
                        reasons.append({"code": "substitute_allergy", "severity": "warning",
                                        "message": "存在严重过敏的替代品已跳过"})
                        continue
                    if severity:
                        approvals.append("allergy_override")
                        reasons.append({"code": "substitute_allergy", "severity": "approval",
                                        "message": f"替代品存在 {severity} 级过敏史，需医生批准"})
                plan_items.append(item)
                remaining -= take
                if emergency:
                    if "emergency_reserve" not in approvals:
                        approvals.append("emergency_reserve")
                    reasons.append({"code": "emergency_reserve", "severity": "approval",
                                    "message": f"需动用批次 {batch['lot']} 的最低应急储备 {take} 份，"
                                               "须药剂/医生批准并补充补货"})
                if substitute and "substitute" not in approvals:
                    approvals.append("substitute")
                    sub_med = connection.execute(
                        "SELECT generic_name FROM med_catalog WHERE medication_id=?",
                        (batch["medication_id"],)).fetchone()
                    reasons.append({"code": "substitute", "severity": "approval",
                                    "message": f"原药品不足，拟用近效期替代品 "
                                               f"{sub_med['generic_name']}，须医生批准"})

        # 1) 本站常规量 FEFO。
        fill_from(self._eligible_batches(connection, site_id=site_id,
                                         medication_id=med["medication_id"], today=today),
                  substitute=False, emergency=False, pool_fn=self._regular_pool)
        # 2) 急救请求动用应急储备。
        if remaining > 0 and is_emergency:
            fill_from(self._eligible_batches(connection, site_id=site_id,
                                             medication_id=med["medication_id"], today=today),
                      substitute=False, emergency=True, pool_fn=self._emergency_pool)
        # 3) 经允许的替代品（先常规后应急）。
        if remaining > 0 and allow_substitute:
            for sub_id in self._substitutes(connection, med["medication_id"]):
                if remaining <= 0:
                    break
                sub_med = connection.execute(
                    "SELECT * FROM med_catalog WHERE medication_id=?", (sub_id,)).fetchone()
                fill_from(self._eligible_batches(connection, site_id=site_id,
                                                 medication_id=sub_id, today=today),
                          substitute=True, emergency=False, pool_fn=self._regular_pool)
                if remaining > 0 and is_emergency:
                    fill_from(self._eligible_batches(connection, site_id=site_id,
                                                     medication_id=sub_id, today=today),
                              substitute=True, emergency=True, pool_fn=self._emergency_pool)
        # 4) 经允许的跨站调拨（仅原药品常规量）。
        transfer_plan = None
        if remaining > 0 and allow_transfer:
            other_rows = connection.execute(
                "SELECT b.*, s.name AS site_name FROM med_batches b JOIN sites s ON s.site_id=b.site_id "
                "WHERE b.medication_id=? AND b.status='available' AND b.expiry_date>=? "
                "AND b.site_id<>? ORDER BY b.expiry_date, b.lot",
                (med["medication_id"], today, site_id),
            ).fetchall()
            for row in other_rows:
                if remaining <= 0:
                    break
                take = min(remaining, self._regular_pool(row))
                if take <= 0:
                    continue
                transfer_plan = {"from_site_id": row["site_id"], "from_site_name": row["site_name"],
                                 "medication_id": med["medication_id"], "quantity": remaining}
                plan_items.append({"site_id": row["site_id"], "batch_id": None,
                                   "medication_id": med["medication_id"], "quantity": take,
                                   "substitute": False, "emergency": False,
                                   "expiry_date": None, "transfer": True})
                approvals.append("transfer")
                reasons.append({"code": "transfer", "severity": "approval",
                                "message": f"本站库存不足，拟从 {row['site_name']} 调拨 {take} 份，"
                                           "须批准并记录理由"})
                remaining -= take

        if remaining > 0 and not blockers:
            blockers.append("insufficient_stock")
            reasons.append({"code": "insufficient_stock", "severity": "blocker",
                            "message": f"全部允许来源仍缺 {remaining} 份，且不能动用未批准的应急/替代/调拨"})

        stock = self._supply_summary(connection, site_id, med["medication_id"])
        return {
            "allowed": not blockers,
            "blocked": bool(blockers),
            "needs_approval": bool(approvals) and not blockers,
            "approval_types": approvals,
            "blockers": blockers,
            "reasons": reasons,
            "plan": {"items": plan_items, "transfer": transfer_plan},
            "stock": stock,
        }

    def _supply_summary(self, connection, site_id: str, medication_id: str) -> dict[str, Any]:
        rows = connection.execute(
            "SELECT * FROM med_batches WHERE site_id=? AND medication_id=? AND status!='destroyed'",
            (site_id, medication_id),
        ).fetchall()
        on_hand = sum(r["quantity_on_hand"] for r in rows)
        reserved = sum(r["reserved"] for r in rows)
        emergency = sum(min(r["emergency_reserve"], r["quantity_on_hand"] - r["reserved"]) for r in rows)
        return {"site_id": site_id, "medication_id": medication_id,
                "on_hand": on_hand, "reserved": reserved,
                "regular_available": max(0, on_hand - reserved - sum(r["emergency_reserve"] for r in rows)),
                "emergency_available": emergency}

    def create_order(self, *, request_id: str, actor_id: str, site_id: str, medication: str,
                     quantity: int, patient_id: str | None = None, indication: str = "",
                     is_emergency: bool = False, allow_substitute: bool = False,
                     allow_transfer: bool = False, order_id: str | None = None) -> dict[str, Any]:
        """开立医嘱。常规充足即自动预留；需要升级处置则进入待批准；硬阻断直接拒绝。"""

        payload = {"actor_id": actor_id, "site_id": site_id, "patient_id": patient_id,
                   "medication": medication, "quantity": quantity, "indication": indication,
                   "is_emergency": is_emergency, "allow_substitute": allow_substitute,
                   "allow_transfer": allow_transfer, "order_id": order_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "medical_officer", "nurse", "pharmacist", "admin")
            site = self._site(connection, site_id)
            self._require_site_access(actor, site)
            med = self._resolve_medication(connection, medication)
            quantity = int(quantity)
            if quantity <= 0:
                raise ValidationError("quantity 必须为正")
            if patient_id:
                if connection.execute("SELECT 1 FROM med_patients WHERE patient_id=?",
                                      (patient_id,)).fetchone() is None:
                    raise NotFoundError("患者不存在")
            decision = self._evaluate(connection, actor=actor, site_id=site_id,
                                      patient_id=patient_id, med=med, quantity=quantity,
                                      indication=indication, is_emergency=is_emergency,
                                      allow_substitute=allow_substitute,
                                      allow_transfer=allow_transfer)
            if decision["blocked"]:
                raise PolicyDenied("用药请求被临床或库存规则阻止", decision["reasons"])
            order_id = order_id or uuid.uuid4().hex

            def create():
                now = self._now()
                connection.execute(
                    "INSERT INTO med_orders(order_id,site_id,patient_id,medication_id,quantity,"
                    "indication,is_emergency,allow_substitute,allow_transfer,status,prescriber_id,"
                    "plan_json,decision_json,created_at,updated_at,version) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                    (order_id, site_id, patient_id, med["medication_id"], quantity, indication,
                     int(is_emergency), int(allow_substitute), int(allow_transfer),
                     "pending_review", actor_id, canonical_json(decision["plan"]),
                     canonical_json(_public_decision(decision)), now, now),
                )
                if decision["needs_approval"]:
                    status = "pending_review"
                else:
                    self._reserve_plan(connection, order_id=order_id, plan=decision["plan"],
                                       actor_id=actor_id)
                    status = "reserved"
                    connection.execute("UPDATE med_orders SET status='reserved',updated_at=? "
                                       "WHERE order_id=?", (now, order_id))
                self._audit(connection, actor_id=actor_id, action="medication.order_created",
                            resource_type="med_order", resource_id=order_id,
                            detail={"site_id": site_id, "patient_id": patient_id,
                                    "medication_id": med["medication_id"], "quantity": quantity,
                                    "is_emergency": is_emergency, "status": status,
                                    "approval_types": decision["approval_types"]})
                return "med_order", order_id, {"order_id": order_id, "status": status,
                                               "decision": _public_decision(decision)}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_order", payload=payload, create=create)

    def _reserve_plan(self, connection, *, order_id: str, plan: dict[str, Any],
                      actor_id: str) -> None:
        """按评估计划在本站批次上建立预留；transfer 项由调拨到达后处理。"""

        for item in plan["items"]:
            if item["transfer"]:
                continue
            batch = self._batch(connection, item["batch_id"])
            pool = (self._emergency_pool(batch) if item["emergency"]
                    else self._regular_pool(batch))
            if item["quantity"] > pool or batch["status"] != "available" \
                    or batch["expiry_date"] < self._today().isoformat():
                raise PolicyDenied("库存情况已变化，原取药计划失效，请重新评估", [
                    {"code": "plan_stale", "severity": "blocker",
                     "message": f"批次 {batch['lot']} 可用量不足或已失效"}])
            reservation_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO med_reservations(reservation_id,batch_id,order_id,quantity,"
                "is_substitute,is_emergency,status,created_at) VALUES(?,?,?,?,?,?,'held',?)",
                (reservation_id, item["batch_id"], order_id, item["quantity"],
                 int(item["substitute"]), int(item["emergency"]), self._now()),
            )
            self._append_ledger(connection, batch_id=item["batch_id"], site_id=batch["site_id"],
                                movement_type="reserve", quantity=item["quantity"],
                                reserved_delta=item["quantity"], order_id=order_id,
                                reservation_id=reservation_id, actor_id=actor_id,
                                reason=f"医嘱 {order_id} 预留")

    def approve_order(self, *, request_id: str, actor_id: str, order_id: str,
                      justification: str) -> dict[str, Any]:
        """批准待审医嘱：动用应急量、替代品、过敏覆盖或跨站调拨均需理由与批准。"""

        payload = {"actor_id": actor_id, "order_id": order_id, "justification": justification}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            order = self._accessible_order(connection, actor, order_id)
            if order["status"] != "pending_review":
                raise ConflictError("医嘱不在待批准状态")
            decision = json.loads(order["decision_json"])
            if not decision.get("needs_approval"):
                raise ConflictError("该医嘱无需批准")
            approvals = set(decision["approval_types"])
            # 不同升级处置对应不同批准权限，取交集后必须仍有人能批。
            allowed = set(APPROVER_ROLES)
            if "allergy_override" in approvals:
                allowed &= {"medical_officer"}
            if "substitute" in approvals:
                allowed &= {"medical_officer", "pharmacist"}
            if "emergency_reserve" in approvals:
                allowed &= {"medical_officer", "pharmacist"}
            if "transfer" in approvals:
                allowed &= set(TRANSFER_APPROVER_ROLES) | {"pharmacist"}
            self._require(actor, *sorted(allowed))
            if not str(justification).strip():
                raise ValidationError("批准必须填写理由")
            med = connection.execute("SELECT * FROM med_catalog WHERE medication_id=?",
                                     (order["medication_id"],)).fetchone()
            if med["controlled"] and actor.actor_id == order["prescriber_id"]:
                raise PermissionDenied("管制药品需双人核对，开单人不能批准自己的医嘱")

            def create():
                now = self._now()
                transfer_id = None
                status = "reserved"
                plan = json.loads(order["plan_json"])
                local_items = [i for i in plan["items"] if not i["transfer"]]
                if local_items:
                    self._reserve_plan(connection, order_id=order_id, plan=plan, actor_id=actor_id)
                if plan.get("transfer"):
                    transfer_id = self._open_transfer_for_order(connection, actor=actor, order=order,
                                                                plan=plan, justification=justification,
                                                                now=now)
                    status = "awaiting_transfer"
                connection.execute(
                    "UPDATE med_orders SET status=?,approver_id=?,approved_at=?,justification=?,"
                    "updated_at=?,version=version+1 WHERE order_id=?",
                    (status, actor_id, now, justification, now, order_id))
                self._audit(connection, actor_id=actor_id, action="medication.order_approved",
                            resource_type="med_order", resource_id=order_id,
                            detail={"approval_types": sorted(approvals),
                                    "justification": justification, "transfer_id": transfer_id,
                                    "patient_id": order["patient_id"]})
                return "med_order_approval", order_id, {
                    "order_id": order_id, "status": status, "transfer_id": transfer_id,
                    "approval_types": sorted(approvals)}

            return self._idempotent(connection, request_id=request_id,
                                    action="approve_order", payload=payload, create=create)

    def _open_transfer_for_order(self, connection, *, actor: Actor, order, plan: dict[str, Any],
                                 justification: str, now: str) -> str:
        transfer = plan["transfer"]
        transfer_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO med_transfers(transfer_id,from_site_id,to_site_id,medication_id,quantity,"
            "status,reason,justification,approver_id,approved_at,order_id,created_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (transfer_id, transfer["from_site_id"], order["site_id"], order["medication_id"],
             transfer["quantity"], "approved", f"医嘱 {order['order_id']} 批准调拨",
             justification, actor.actor_id, now, order["order_id"], actor.actor_id, now, now),
        )
        connection.execute(
            "UPDATE med_orders SET source_transfer_id=? WHERE order_id=?",
            (transfer_id, order["order_id"]))
        return transfer_id

    def issue_order(self, *, request_id: str, actor_id: str, order_id: str) -> dict[str, Any]:
        """发放已预留医嘱。幂等回执保证同一医嘱重试只扣减一次库存。"""

        payload = {"actor_id": actor_id, "order_id": order_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *CLINICAL_ROLES, "admin")
            order = self._accessible_order(connection, actor, order_id)
            if order["status"] == "issued":
                # 同一医嘱的任何重试都只能看到原发放事实，绝不二次扣减。
                existing = connection.execute(
                    "SELECT COALESCE(SUM(quantity),0) AS q FROM med_ledger_entries "
                    "WHERE order_id=? AND movement_type='issue'", (order_id,)).fetchone()
                return {"request_id": request_id, "resource_type": "med_order_issue",
                        "resource_id": order_id, "replayed": True,
                        "response": {"order_id": order_id, "issued": existing["q"],
                                     "already_issued": True}}
            if order["status"] != "reserved":
                raise PolicyDenied("医嘱尚未完成预留，不能发放", [
                    {"code": "not_reserved", "severity": "blocker",
                     "message": f"当前状态为 {order['status']}"}])

            def create():
                now = self._now()
                reservations = connection.execute(
                    "SELECT * FROM med_reservations WHERE order_id=? AND status='held'",
                    (order_id,),
                ).fetchall()
                issued = 0
                for res in reservations:
                    batch = self._batch(connection, res["batch_id"])
                    # 发放即消费预留：在库与预留同时扣减，保持 reserved <= on_hand。
                    self._append_ledger(connection, batch_id=res["batch_id"],
                                        site_id=batch["site_id"], movement_type="issue",
                                        quantity=res["quantity"],
                                        reserved_delta=-res["quantity"], order_id=order_id,
                                        reservation_id=res["reservation_id"], actor_id=actor_id,
                                        reason=f"医嘱 {order_id} 发放")
                    connection.execute(
                        "UPDATE med_reservations SET status='consumed' WHERE reservation_id=?",
                        (res["reservation_id"],))
                    connection.execute(
                        "INSERT INTO med_order_lines(line_id,order_id,batch_id,medication_id,"
                        "quantity,is_substitute,is_emergency_stock,is_transfer_stock,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, order_id, res["batch_id"], batch["medication_id"],
                         res["quantity"], res["is_substitute"], res["is_emergency"],
                         1 if batch["source_transfer_id"] else 0, now))
                    issued += res["quantity"]
                    self._deplete_if_empty(connection, res["batch_id"])
                connection.execute(
                    "UPDATE med_orders SET status='issued',updated_at=?,version=version+1 "
                    "WHERE order_id=?", (now, order_id))
                self._audit(connection, actor_id=actor_id, action="medication.issued",
                            resource_type="med_order", resource_id=order_id,
                            detail={"patient_id": order["patient_id"],
                                    "medication_id": order["medication_id"], "quantity": issued,
                                    "lines": len(reservations)})
                return "med_order_issue", order_id, {"order_id": order_id, "issued": issued}

            return self._idempotent(connection, request_id=request_id,
                                    action="issue_order", payload=payload, create=create)

    def return_medication(self, *, request_id: str, actor_id: str, order_id: str,
                          batch_id: str, quantity: int, reason: str) -> dict[str, Any]:
        """患者退药：仅追加正向回库流水，发放事实保留不变。"""

        payload = {"actor_id": actor_id, "order_id": order_id, "batch_id": batch_id,
                   "quantity": quantity, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *CLINICAL_ROLES, "logistics")
            order = self._accessible_order(connection, actor, order_id)
            if order["status"] != "issued":
                raise ConflictError("只能退回已发放医嘱的药品")
            line = connection.execute(
                "SELECT SUM(quantity) AS q FROM med_order_lines WHERE order_id=? AND batch_id=?",
                (order_id, batch_id),
            ).fetchone()
            batch = self._accessible_batch(connection, actor, batch_id)
            if batch["site_id"] != order["site_id"]:
                raise ValidationError("退回批次不属于该医嘱所在站点")
            quantity = int(quantity)
            if quantity <= 0 or not str(reason).strip():
                raise ValidationError("quantity 必须为正且 reason 必填")
            already_returned = connection.execute(
                "SELECT COALESCE(SUM(quantity),0) AS q FROM med_ledger_entries "
                "WHERE order_id=? AND batch_id=? AND movement_type='return'",
                (order_id, batch_id),
            ).fetchone()["q"]
            if already_returned + quantity > (line["q"] or 0):
                raise ValidationError("退回数量不能超过该批次在本医嘱的发放数量")

            def create():
                self._append_ledger(connection, batch_id=batch_id, site_id=batch["site_id"],
                                    movement_type="return", quantity=quantity, order_id=order_id,
                                    reason=reason, actor_id=actor_id)
                # 仅“耗尽”批次因退药恢复可用；已销毁/隔离批次退回的货继续隔离，不得复活。
                if batch["status"] == "depleted" and batch["expiry_date"] >= self._today().isoformat():
                    connection.execute(
                        "UPDATE med_batches SET status='available' WHERE batch_id=?",
                        (batch_id,))
                self._audit(connection, actor_id=actor_id, action="medication.returned",
                            resource_type="med_order", resource_id=order_id,
                            detail={"batch_id": batch_id, "quantity": quantity, "reason": reason,
                                    "patient_id": order["patient_id"]})
                return "med_order_return", order_id, {
                    "order_id": order_id, "batch_id": batch_id, "returned": quantity}

            return self._idempotent(connection, request_id=request_id,
                                    action="return_medication", payload=payload, create=create)

    def reject_order(self, *, request_id: str, actor_id: str, order_id: str,
                     reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "order_id": order_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *APPROVER_ROLES)
            order = self._accessible_order(connection, actor, order_id)
            if order["status"] != "pending_review":
                raise ConflictError("只能拒绝待批准医嘱")

            def create():
                now = self._now()
                connection.execute(
                    "UPDATE med_orders SET status='rejected',justification=?,updated_at=?,"
                    "version=version+1 WHERE order_id=?", (reason, now, order_id))
                self._audit(connection, actor_id=actor_id, action="medication.order_rejected",
                            resource_type="med_order", resource_id=order_id,
                            detail={"reason": reason, "patient_id": order["patient_id"]})
                return "med_order_reject", order_id, {"order_id": order_id, "status": "rejected"}

            return self._idempotent(connection, request_id=request_id,
                                    action="reject_order", payload=payload, create=create)

    def cancel_order(self, *, request_id: str, actor_id: str, order_id: str,
                     reason: str) -> dict[str, Any]:
        """取消医嘱，释放全部本站预留；若已发调拨则一并中止未发货调拨。"""

        payload = {"actor_id": actor_id, "order_id": order_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *CLINICAL_ROLES, "admin")
            order = self._accessible_order(connection, actor, order_id)
            if order["status"] in ("issued", "rejected", "cancelled"):
                raise ConflictError("当前医嘱状态不能取消")

            def create():
                now = self._now()
                for res in connection.execute(
                        "SELECT * FROM med_reservations WHERE order_id=? AND status='held'",
                        (order_id,)).fetchall():
                    batch = self._batch(connection, res["batch_id"])
                    self._append_ledger(connection, batch_id=res["batch_id"],
                                        site_id=batch["site_id"],
                                        movement_type="release_reservation",
                                        quantity=res["quantity"], reserved_delta=-res["quantity"],
                                        order_id=order_id, reservation_id=res["reservation_id"],
                                        actor_id=actor_id, reason=f"取消医嘱 {order_id}")
                    connection.execute(
                        "UPDATE med_reservations SET status='released',released_at=? "
                        "WHERE reservation_id=?", (now, res["reservation_id"]))
                if order["source_transfer_id"]:
                    connection.execute(
                        "UPDATE med_transfers SET status='cancelled',updated_at=? "
                        "WHERE transfer_id=? AND status IN ('proposed','approved')",
                        (now, order["source_transfer_id"]))
                connection.execute(
                    "UPDATE med_orders SET status='cancelled',updated_at=?,version=version+1 "
                    "WHERE order_id=?", (now, order_id))
                self._audit(connection, actor_id=actor_id, action="medication.order_cancelled",
                            resource_type="med_order", resource_id=order_id,
                            detail={"reason": reason, "patient_id": order["patient_id"]})
                return "med_order_cancel", order_id, {"order_id": order_id, "status": "cancelled"}

            return self._idempotent(connection, request_id=request_id,
                                    action="cancel_order", payload=payload, create=create)

    def _order(self, connection, order_id: str):
        row = connection.execute("SELECT * FROM med_orders WHERE order_id=?", (order_id,)).fetchone()
        if row is None:
            raise NotFoundError("医嘱不存在")
        return row

    def _accessible_order(self, connection, actor: Actor, order_id: str):
        """取出医嘱并确认操作者有权访问其所属站点。"""

        order = self._order(connection, order_id)
        self._require_site_access(actor, self._site(connection, order["site_id"]))
        return order

    def _accessible_batch(self, connection, actor: Actor, batch_id: str):
        """取出批次并确认操作者有权访问其所属站点。"""

        batch = self._batch(connection, batch_id)
        self._require_site_access(actor, self._site(connection, batch["site_id"]))
        return batch

    # ----------------------------------------------------------- 仅追加修订

    def amend_order(self, *, request_id: str, actor_id: str, order_id: str, kind: str,
                    value: str) -> dict[str, Any]:
        """后补诊疗信息：只追加修订记录，不修改或抹掉任何已发放事实。

        发放后补链接患者若对已发药品有过敏史，会返回并留存显著预警。
        """

        payload = {"actor_id": actor_id, "order_id": order_id, "kind": kind, "value": value}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *CLINICAL_ROLES, "admin")
            order = self._accessible_order(connection, actor, order_id)
            if kind not in {"patient_link", "indication", "note", "cancel_note"}:
                raise ValidationError("kind 不支持")
            if not str(value).strip():
                raise ValidationError("修订内容不能为空")
            warning: dict[str, Any] | None = None
            patient_id = order["patient_id"]
            if kind == "patient_link":
                patient = connection.execute(
                    "SELECT * FROM med_patients WHERE patient_id=?", (value,)).fetchone()
                if patient is None:
                    raise NotFoundError("患者不存在")
                if patient["site_id"] != order["site_id"]:
                    raise ValidationError("患者不属于医嘱所在站点")
                if order["patient_id"] and order["patient_id"] != value:
                    raise ConflictError("医嘱已关联其他患者，不能改挂，只能追加说明")
                patient_id = value
                allergy = connection.execute(
                    "SELECT severity FROM med_patient_allergies WHERE patient_id=? "
                    "AND medication_id=?", (value, order["medication_id"])).fetchone()
                if allergy:
                    warning = {"code": "post_issue_allergy",
                               "severity": "warning" if order["status"] == "issued" else "info",
                               "message": f"后补患者对所发药品有 {allergy['severity']} 级过敏史，"
                                          "发放事实保留，立即通知医生随访"}

            def create():
                amendment_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO med_amendments(amendment_id,order_id,kind,payload_json,"
                    "warning_json,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (amendment_id, order_id, kind, canonical_json({"value": value}),
                     canonical_json(warning) if warning else "", actor_id, self._now()),
                )
                if kind == "patient_link" and not order["patient_id"]:
                    connection.execute(
                        "UPDATE med_orders SET patient_id=?,updated_at=?,version=version+1 "
                        "WHERE order_id=?", (value, self._now(), order_id))
                self._audit(connection, actor_id=actor_id, action="medication.order_amended",
                            resource_type="med_order", resource_id=order_id,
                            detail={"kind": kind, "warning": warning,
                                    "patient_id": patient_id})
                return "med_order_amendment", amendment_id, {
                    "amendment_id": amendment_id, "order_id": order_id, "kind": kind,
                    "warning": warning, "issued_fact_preserved": order["status"] == "issued"}

            return self._idempotent(connection, request_id=request_id,
                                    action="amend_order", payload=payload, create=create)

    def get_order(self, *, actor_id: str, order_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *CLINICAL_ROLES, "admin")
            order = self._order(connection, order_id)
            site = self._site(connection, order["site_id"])
            self._require_site_access(actor, site)
            lines = connection.execute(
                "SELECT * FROM med_order_lines WHERE order_id=? ORDER BY line_id", (order_id,)).fetchall()
            amendments = connection.execute(
                "SELECT * FROM med_amendments WHERE order_id=? ORDER BY created_at", (order_id,)).fetchall()
            med = connection.execute("SELECT * FROM med_catalog WHERE medication_id=?",
                                     (order["medication_id"],)).fetchone()
            return {
                "order_id": order["order_id"], "site_id": order["site_id"],
                "patient_id": order["patient_id"], "medication_id": order["medication_id"],
                "generic_name": med["generic_name"], "quantity": order["quantity"],
                "indication": order["indication"], "is_emergency": bool(order["is_emergency"]),
                "status": order["status"], "justification": order["justification"],
                "prescriber_id": order["prescriber_id"], "approver_id": order["approver_id"],
                "source_transfer_id": order["source_transfer_id"],
                "created_at": order["created_at"], "updated_at": order["updated_at"],
                "decision": json.loads(order["decision_json"]) if order["decision_json"] else None,
                "lines": [{"batch_id": r["batch_id"], "medication_id": r["medication_id"],
                           "quantity": r["quantity"], "is_substitute": bool(r["is_substitute"]),
                           "is_emergency_stock": bool(r["is_emergency_stock"]),
                           "is_transfer_stock": bool(r["is_transfer_stock"])} for r in lines],
                "amendments": [{"kind": r["kind"], "payload": json.loads(r["payload_json"]),
                                "warning": json.loads(r["warning_json"]) if r["warning_json"] else None,
                                "created_by": r["created_by"], "created_at": r["created_at"]}
                               for r in amendments],
            }

    # ------------------------------------------------------------- 跨站调拨

    def ship_transfer(self, *, request_id: str, actor_id: str, transfer_id: str,
                      batch_id: str | None = None) -> dict[str, Any]:
        """调出方发货：按 FEFO 扣减本站库存并形成 transfer_out 流水。"""

        payload = {"actor_id": actor_id, "transfer_id": transfer_id, "batch_id": batch_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "logistics", "pharmacist", "admin", "operator")
            transfer = self._transfer(connection, transfer_id)
            if transfer["status"] not in ("approved", "proposed"):
                raise ConflictError("调拨单未处于可发货状态")
            site = self._site(connection, transfer["from_site_id"])
            self._require_site_access(actor, site)
            if batch_id is None:
                batch = connection.execute(
                    "SELECT * FROM med_batches WHERE site_id=? AND medication_id=? AND status='available' "
                    "AND expiry_date>=? ORDER BY expiry_date LIMIT 1",
                    (transfer["from_site_id"], transfer["medication_id"], self._today().isoformat()),
                ).fetchone()
                if batch is None:
                    raise PolicyDenied("调出站点没有合格批次", [
                        {"code": "no_source_batch", "severity": "blocker",
                         "message": "没有未过期可用批次可供调拨"}])
            else:
                batch = self._batch(connection, batch_id)
                if batch["site_id"] != transfer["from_site_id"]:
                    raise ValidationError("批次不属于调出站点")
            if self._regular_pool(batch) < transfer["quantity"]:
                raise PolicyDenied("该批次常规可用量不足，不能占用应急储备发货", [
                    {"code": "emergency_reserve", "severity": "blocker",
                     "message": "调拨数量超过该批次常规可用量"}])

            def create():
                now = self._now()
                self._append_ledger(connection, batch_id=batch["batch_id"],
                                    site_id=transfer["from_site_id"], movement_type="transfer_out",
                                    quantity=transfer["quantity"], transfer_id=transfer_id,
                                    counterparty_site_id=transfer["to_site_id"],
                                    reason=f"调拨至 {transfer['to_site_id']}", actor_id=actor_id)
                self._deplete_if_empty(connection, batch["batch_id"])
                connection.execute(
                    "UPDATE med_transfers SET status='shipped',from_batch_id=?,updated_at=? "
                    "WHERE transfer_id=?", (batch["batch_id"], now, transfer_id))
                self._audit(connection, actor_id=actor_id, action="medication.transfer_shipped",
                            resource_type="med_transfer", resource_id=transfer_id,
                            detail={"from_site_id": transfer["from_site_id"],
                                    "to_site_id": transfer["to_site_id"],
                                    "medication_id": transfer["medication_id"],
                                    "quantity": transfer["quantity"],
                                    "batch_id": batch["batch_id"]})
                return "med_transfer_ship", transfer_id, {
                    "transfer_id": transfer_id, "status": "shipped",
                    "batch_id": batch["batch_id"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="ship_transfer", payload=payload, create=create)

    def receive_transfer(self, *, request_id: str, actor_id: str, transfer_id: str,
                         lot: str | None = None, expiry_date: str | None = None,
                         emergency_reserve: int = 0) -> dict[str, Any]:
        """调入方收货：生成新批次并记 transfer_in；关联医嘱则自动完成预留。"""

        payload = {"actor_id": actor_id, "transfer_id": transfer_id, "lot": lot,
                   "expiry_date": expiry_date, "emergency_reserve": emergency_reserve}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *RECEIVE_ROLES)
            transfer = self._transfer(connection, transfer_id)
            if transfer["status"] != "shipped":
                raise ConflictError("调拨单未发货，不能收货")
            site = self._site(connection, transfer["to_site_id"])
            self._require_site_access(actor, site)
            source_batch = self._batch(connection, transfer["from_batch_id"])
            lot = lot or source_batch["lot"]
            expiry_date = self._date(expiry_date or source_batch["expiry_date"], "expiry_date")

            def create():
                now = self._now()
                batch_id = uuid.uuid4().hex
                # 在库余额由 transfer_in 流水建立，插入时为 0。
                connection.execute(
                    "INSERT INTO med_batches(batch_id,site_id,medication_id,lot,expiry_date,"
                    "quantity_initial,quantity_on_hand,reserved,emergency_reserve,storage_condition,"
                    "status,source_transfer_id,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,0,0,?,?,?,?,?,?)",
                    (batch_id, transfer["to_site_id"], transfer["medication_id"], lot, expiry_date,
                     transfer["quantity"], emergency_reserve,
                     source_batch["storage_condition"], "available", transfer_id, actor_id, now),
                )
                self._append_ledger(connection, batch_id=batch_id, site_id=transfer["to_site_id"],
                                    movement_type="transfer_in", quantity=transfer["quantity"],
                                    transfer_id=transfer_id,
                                    counterparty_site_id=transfer["from_site_id"],
                                    reason=f"由 {transfer['from_site_id']} 调入", actor_id=actor_id)
                order_status = None
                if transfer["order_id"]:
                    order = self._order(connection, transfer["order_id"])
                    if order["status"] == "awaiting_transfer":
                        # 本地项在批准时已预留，这里只把原调拨项落到新到批次上。
                        transfer_plan = {"items": []}
                        for item in json.loads(order["plan_json"])["items"]:
                            if item["transfer"]:
                                transfer_plan["items"].append(
                                    {**item, "batch_id": batch_id,
                                     "site_id": transfer["to_site_id"], "transfer": False})
                        self._reserve_plan(connection, order_id=order["order_id"],
                                           plan=transfer_plan, actor_id=actor_id)
                        connection.execute(
                            "UPDATE med_orders SET status='reserved',updated_at=?,"
                            "version=version+1 WHERE order_id=?", (now, order["order_id"]))
                        order_status = "reserved"
                connection.execute(
                    "UPDATE med_transfers SET status='received',to_batch_id=?,updated_at=? "
                    "WHERE transfer_id=?", (batch_id, now, transfer_id))
                self._audit(connection, actor_id=actor_id, action="medication.transfer_received",
                            resource_type="med_transfer", resource_id=transfer_id,
                            detail={"from_site_id": transfer["from_site_id"],
                                    "to_site_id": transfer["to_site_id"],
                                    "medication_id": transfer["medication_id"],
                                    "quantity": transfer["quantity"], "batch_id": batch_id,
                                    "order_id": transfer["order_id"]})
                return "med_transfer_receive", transfer_id, {
                    "transfer_id": transfer_id, "batch_id": batch_id, "status": "received",
                    "order_status": order_status}

            return self._idempotent(connection, request_id=request_id,
                                    action="receive_transfer", payload=payload, create=create)

    def _transfer(self, connection, transfer_id: str):
        row = connection.execute(
            "SELECT * FROM med_transfers WHERE transfer_id=?", (transfer_id,)).fetchone()
        if row is None:
            raise NotFoundError("调拨单不存在")
        return row

    # ----------------------------------------------------------- 查询与报表

    def medication(self, actor_id: str, medication: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            med = self._resolve_medication(connection, medication)
            names = connection.execute(
                "SELECT name,name_kind FROM med_catalog_names WHERE medication_id=? ORDER BY name",
                (med["medication_id"],)).fetchall()
            return {"medication_id": med["medication_id"], "generic_name": med["generic_name"],
                    "form": med["form"], "strength": med["strength"], "route": med["route"],
                    "storage_condition": med["storage_condition"],
                    "controlled": bool(med["controlled"]),
                    "prescription_only": bool(med["prescription_only"]),
                    "indications": json.loads(med["indications_json"]),
                    "names": [{"name": r["name"], "kind": r["name_kind"]} for r in names],
                    "substitutes": self._substitutes(connection, med["medication_id"])}

    def supply_overview(self, actor_id: str, site_id: str) -> dict[str, Any]:
        """站点药品总览：在库、预留、常规可用、应急可用与最低储备是否被挤占。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            site = self._site(connection, site_id)
            self._require_site_access(actor, site)
            rows = connection.execute(
                "SELECT b.medication_id,m.generic_name,b.* FROM med_batches b "
                "JOIN med_catalog m ON m.medication_id=b.medication_id "
                "WHERE b.site_id=? ORDER BY m.generic_name,b.expiry_date", (site_id,)).fetchall()
            meds: dict[str, dict[str, Any]] = {}
            for row in rows:
                item = meds.setdefault(row["medication_id"], {
                    "medication_id": row["medication_id"],
                    "generic_name": row["generic_name"],
                    "on_hand": 0, "reserved": 0, "emergency_reserve": 0,
                    "regular_available": 0, "batches": []})
                item["on_hand"] += row["quantity_on_hand"]
                item["reserved"] += row["reserved"]
                item["emergency_reserve"] += row["emergency_reserve"]
                item["regular_available"] += max(
                    0, row["quantity_on_hand"] - row["reserved"] - row["emergency_reserve"])
                item["batches"].append({
                    "batch_id": row["batch_id"], "lot": row["lot"],
                    "expiry_date": row["expiry_date"], "status": row["status"],
                    "on_hand": row["quantity_on_hand"], "reserved": row["reserved"],
                    "emergency_reserve": row["emergency_reserve"],
                    "storage_condition": row["storage_condition"]})
            for item in meds.values():
                item["emergency_at_risk"] = (
                    item["on_hand"] - item["reserved"] <= item["emergency_reserve"])
            return {"site_id": site_id, "items": sorted(meds.values(),
                                                        key=lambda x: x["generic_name"])}

    def expiry_and_shortage_report(self, actor_id: str, site_id: str,
                                   within_days: int = NEAR_EXPIRY_DAYS) -> dict[str, Any]:
        """供后勤提前处理近效期与短缺风险。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            site = self._site(connection, site_id)
            self._require_site_access(actor, site)
            today = self._today()
            cutoff = (today + timedelta(days=int(within_days))).isoformat()
            today_s = today.isoformat()
            expiring, expired = [], []
            for row in connection.execute(
                    "SELECT b.*,m.generic_name FROM med_batches b "
                    "JOIN med_catalog m ON m.medication_id=b.medication_id "
                    "WHERE b.site_id=? AND b.status IN ('available','quarantined') "
                    "AND b.quantity_on_hand>0 ORDER BY b.expiry_date", (site_id,)).fetchall():
                entry = {"batch_id": row["batch_id"], "medication_id": row["medication_id"],
                         "generic_name": row["generic_name"], "lot": row["lot"],
                         "expiry_date": row["expiry_date"],
                         "on_hand": row["quantity_on_hand"], "reserved": row["reserved"]}
                if row["expiry_date"] < today_s:
                    expired.append(entry)
                elif row["expiry_date"] <= cutoff:
                    expiring.append(entry)
            shortages = []
            for row in connection.execute(
                    "SELECT medication_id,SUM(quantity_on_hand) AS on_hand,SUM(reserved) AS reserved,"
                    "SUM(emergency_reserve) AS emergency FROM med_batches "
                    "WHERE site_id=? AND status='available' GROUP BY medication_id",
                    (site_id,)).fetchall():
                regular = max(0, row["on_hand"] - row["reserved"] - row["emergency"])
                if regular == 0:
                    med = connection.execute(
                        "SELECT generic_name FROM med_catalog WHERE medication_id=?",
                        (row["medication_id"],)).fetchone()
                    shortages.append({"medication_id": row["medication_id"],
                                      "generic_name": med["generic_name"],
                                      "on_hand": row["on_hand"], "reserved": row["reserved"],
                                      "emergency_reserve": row["emergency"],
                                      "reason": "常规库存为零，下一发药将动用应急储备或替代品"})
            return {"site_id": site_id, "as_of": today_s,
                    "expiring_within_days": int(within_days),
                    "expiring": expiring, "expired": expired, "shortages": shortages}

    def reconcile_batch(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        """逐笔重放账本，核对流水合计与批次当前余额是否一致。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            batch = self._accessible_batch(connection, actor, batch_id)
            return self._reconcile(connection, batch, actor)

    def reconcile_site(self, actor_id: str, site_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            site = self._site(connection, site_id)
            self._require_site_access(actor, site)
            results = []
            for row in connection.execute(
                    "SELECT * FROM med_batches WHERE site_id=? ORDER BY created_at", (site_id,)).fetchall():
                results.append(self._reconcile(connection, row, actor))
            return {"site_id": site_id, "all_matched": all(r["matched"] for r in results),
                    "batches": results}

    def _reconcile(self, connection, batch, actor: Actor) -> dict[str, Any]:
        entries = connection.execute(
            "SELECT * FROM med_ledger_entries WHERE batch_id=? ORDER BY sequence",
            (batch["batch_id"],)).fetchall()
        # 逐条重放：首条入库/调入建立基线，其余带符号流水更新在库与预留。
        running_on_hand = 0
        running_reserved = 0
        balances_ok = True
        for index, entry in enumerate(entries):
            if index == 0 and entry["movement_type"] in ("receive", "transfer_in"):
                running_on_hand = entry["balance_after"]
            else:
                running_on_hand += MOVEMENT_SIGN[entry["movement_type"]] * entry["quantity"]
            running_reserved += entry["reserved_delta"]
            if running_on_hand != entry["balance_after"] \
                    or running_reserved != entry["reserved_after"] \
                    or running_on_hand < 0 or running_reserved < 0:
                balances_ok = False
        matched = (balances_ok
                   and running_on_hand == batch["quantity_on_hand"]
                   and running_reserved == batch["reserved"])
        return {"batch_id": batch["batch_id"], "site_id": batch["site_id"],
                "medication_id": batch["medication_id"], "lot": batch["lot"],
                "entries": len(entries),
                "ledger_on_hand": running_on_hand,
                "ledger_reserved": running_reserved,
                "batch_on_hand": batch["quantity_on_hand"],
                "batch_reserved": batch["reserved"],
                "matched": matched}

    def batch_trace(self, *, actor_id: str, batch_id: str) -> dict[str, Any]:
        """追踪一个批次的完整去向；审计角色只能看到脱敏患者信息。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            batch = self._accessible_batch(connection, actor, batch_id)
            can_see_patient = actor.role in CLINICAL_ROLES or actor.role == "admin"
            entries = connection.execute(
                "SELECT * FROM med_ledger_entries WHERE batch_id=? ORDER BY sequence",
                (batch_id,)).fetchall()
            orders = connection.execute(
                "SELECT DISTINCT o.order_id,o.patient_id,o.status,l.quantity "
                "FROM med_order_lines l JOIN med_orders o ON o.order_id=l.order_id "
                "WHERE l.batch_id=? ORDER BY o.order_id", (batch_id,)).fetchall()
            med = connection.execute("SELECT generic_name FROM med_catalog WHERE medication_id=?",
                                     (batch["medication_id"],)).fetchone()
            dispatched = sum(r["quantity"] for r in entries if r["movement_type"] == "issue")
            transferred = sum(r["quantity"] for r in entries if r["movement_type"] == "transfer_out")
            destroyed = sum(r["quantity"] for r in entries if r["movement_type"] == "destroy")
            lost = sum(r["quantity"] for r in entries if r["movement_type"] == "loss")
            returned = sum(r["quantity"] for r in entries if r["movement_type"] == "return")
            return {
                "batch_id": batch_id, "site_id": batch["site_id"],
                "medication_id": batch["medication_id"], "generic_name": med["generic_name"],
                "lot": batch["lot"], "expiry_date": batch["expiry_date"],
                "initial": batch["quantity_initial"], "on_hand": batch["quantity_on_hand"],
                "status": batch["status"],
                "totals": {"issued": dispatched, "transferred_out": transferred,
                           "destroyed": destroyed, "lost": lost, "returned": returned},
                "destinations": [{
                    "order_id": r["order_id"], "status": r["status"], "quantity": r["quantity"],
                    "patient_ref": r["patient_id"] if can_see_patient
                    else (_mask_patient(r["patient_id"]) if r["patient_id"] else None),
                    "patient_redacted": not can_see_patient,
                } for r in orders],
                "ledger": [{
                    "sequence": e["sequence"], "movement_type": e["movement_type"],
                    "quantity": e["quantity"], "balance_after": e["balance_after"],
                    "reserved_after": e["reserved_after"], "order_id": e["order_id"],
                    "transfer_id": e["transfer_id"],
                    "counterparty_site_id": e["counterparty_site_id"],
                    "reason": e["reason"], "created_by": e["created_by"],
                    "created_at": e["created_at"],
                } for e in entries],
            }

    def redacted_audit_events(self, *, actor_id: str, after_sequence: int = 0) -> dict[str, Any]:
        """审计查询：非临床角色看到的药品事件中患者信息全部脱敏。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            can_see_patient = actor.role in CLINICAL_ROLES or actor.role == "admin"
            rows = connection.execute(
                "SELECT * FROM audit_events WHERE sequence>? ORDER BY sequence",
                (after_sequence,)).fetchall()
            events = []
            for row in rows:
                detail = json.loads(row["detail_json"])
                redacted = False
                if not can_see_patient:
                    # 患者类事件的 resource_id 本身即患者编号，同样脱敏。
                    if row["resource_type"] == "patient":
                        resource_id = _mask_patient(row["resource_id"])
                        redacted = True
                    else:
                        resource_id = row["resource_id"]
                    if detail.get("patient_id"):
                        detail["patient_id"] = _mask_patient(detail["patient_id"])
                        redacted = True
                    detail.pop("patient_name", None)
                else:
                    resource_id = row["resource_id"]
                events.append({"sequence": row["sequence"], "event_id": row["event_id"],
                               "actor_id": row["actor_id"], "action": row["action"],
                               "resource_type": row["resource_type"],
                               "resource_id": resource_id, "detail": detail,
                               "patient_redacted": redacted,
                               "occurred_at": row["occurred_at"]})
            return {"viewer": actor_id, "viewer_role": actor.role, "items": events}


def _public_decision(decision: dict[str, Any]) -> dict[str, Any]:
    """去掉内部 blockers 标记外的可持久化决策快照。"""

    return {"allowed": decision["allowed"], "blocked": decision["blocked"],
            "needs_approval": decision["needs_approval"],
            "approval_types": decision["approval_types"],
            "reasons": decision["reasons"], "plan": decision["plan"],
            "stock": decision["stock"]}


def locals_kwargs(values: dict[str, Any], *, exclude: set[str]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if key not in exclude}
