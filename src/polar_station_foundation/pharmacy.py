"""实现偏远站点的药品与用药保障：统一身份、批次、发放决策与连续账本。"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any, Callable

from .audit import append_event, canonical_json
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .requests import idempotent
from .service import DomainService, IDENTIFIER
from .storage import Database

STORAGE_MODES = ("room", "refrigerated", "frozen")
STORAGE_LABELS = {"room": "常温", "refrigerated": "冷藏", "frozen": "冷冻"}
NEAR_EXPIRY_DAYS = 90

LEDGER_REF = {
    "intake": "batch",
    "reserve": "reservation",
    "reserve_release": "reservation",
    "dispense": "dispensation",
    "return": "dispensation",
    "loss": "batch",
    "destroy": "batch",
    "transfer_out": "transfer",
    "transfer_in": "transfer",
}


class PharmacyService:
    """协调药品目录、批次库存、处方决策、账本流转与审计。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        self.domains = DomainService(database, self.clock)

    # ------------------------------------------------------------------ 基础工具

    def verify_audit(self) -> tuple[bool, int]:
        return self.domains.verify_audit()

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _today(self) -> date:
        return self.clock.now().date()

    def _id(self, value: str, field: str) -> str:
        value = str(value or "").strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 500) -> str:
        value = str(value or "").strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _qty(self, value: Any, field: str, *, minimum: float = 0) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"{field} 必须是数字")
        if value < minimum:
            raise ValidationError(f"{field} 必须大于等于 {minimum}")
        return round(float(value), 6)

    def _actor(self, connection, actor_id: str):
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require_roles(self, actor, *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _site(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("站点不存在")
        return row

    def _same_org_site(self, actor, site) -> None:
        if actor["organization_id"] != site["organization_id"] and actor["role"] != "admin":
            raise PermissionDenied("不能操作其他组织的站点")

    def _medication(self, connection, organization_id: str, medication_id: str):
        row = connection.execute(
            "SELECT * FROM medications WHERE organization_id=? AND medication_id=?",
            (organization_id, medication_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("药品不存在")
        return row

    def _resolve_medication(self, connection, organization_id: str, name_or_id: str):
        """把统一编号或已登记的商品名/别名归并到同一药品身份。"""

        row = connection.execute(
            "SELECT * FROM medications WHERE organization_id=? AND medication_id=?",
            (organization_id, name_or_id),
        ).fetchone()
        if row is not None:
            return row, False
        alias = connection.execute(
            "SELECT medication_id FROM medication_aliases WHERE organization_id=? AND alias=?",
            (organization_id, name_or_id.strip()),
        ).fetchone()
        if alias is None:
            raise NotFoundError(f"未登记的药品身份或别名：{name_or_id}")
        return self._medication(connection, organization_id, alias["medication_id"]), True

    def _ok(self, receipt) -> dict[str, Any]:
        return {"request_id": receipt.request_id, "resource_type": receipt.resource_type,
                "resource_id": receipt.resource_id, "replayed": receipt.replayed,
                **(receipt.response or {})}

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create: Callable[[], tuple[str, str, dict[str, Any]]]):
        return idempotent(
            connection, now=self._now(),
            identifier_validator=lambda value: self._id(value, "request_id"),
            request_id=request_id, action=action, payload=payload, create=create,
        )

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    def _patient(self, connection, site_id: str, patient_id: str):
        row = connection.execute(
            "SELECT * FROM patients WHERE site_id=? AND patient_id=?", (site_id, patient_id)
        ).fetchone()
        if row is None:
            raise NotFoundError("患者不存在")
        return row

    def _allergy_hits(self, connection, organization_id: str, medication_id: str,
                      allergies: list[str]) -> list[str]:
        """返回与药品通用名/商品名命中的过敏项。"""

        med = self._medication(connection, organization_id, medication_id)
        names = {med["generic_name"], med["medication_id"]}
        names.update(r["alias"] for r in connection.execute(
            "SELECT alias FROM medication_aliases WHERE organization_id=? AND medication_id=?",
            (organization_id, medication_id)))
        return [item for item in allergies if item in names]

    # ------------------------------------------------------------------ 目录与配置

    def register_medication(self, *, request_id: str, actor_id: str, organization_id: str,
                            medication_id: str, generic_name: str, dosage_form: str,
                            strength: str, unit: str, storage_required: str,
                            controlled_level: int = 0,
                            indications: list[str] | None = None) -> dict[str, Any]:
        indications = indications or []
        if not isinstance(indications, list) or not all(isinstance(i, str) and i.strip() for i in indications):
            raise ValidationError("indications 必须是非空字符串列表")
        payload = {"actor_id": actor_id, "organization_id": organization_id,
                   "medication_id": medication_id, "generic_name": generic_name,
                   "dosage_form": dosage_form, "strength": strength, "unit": unit,
                   "storage_required": storage_required, "controlled_level": controlled_level,
                   "indications": indications}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "physician")
            organization_id = self._id(organization_id, "organization_id")
            medication_id = self._id(medication_id, "medication_id")
            generic_name = self._text(generic_name, "generic_name")
            dosage_form = self._text(dosage_form, "dosage_form", 80)
            strength = self._text(strength, "strength", 80)
            unit = self._text(unit, "unit", 20)
            if storage_required not in STORAGE_MODES:
                raise ValidationError("storage_required 必须是 room/refrigerated/frozen")
            if not isinstance(controlled_level, int) or not 0 <= controlled_level <= 3:
                raise ValidationError("controlled_level 必须是 0 到 3 的整数")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO medications(medication_id,organization_id,generic_name,dosage_form,"
                        "strength,unit,storage_required,controlled_level,indications_json,active,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,1,?)",
                        (medication_id, organization_id, generic_name, dosage_form, strength, unit,
                         storage_required, controlled_level, canonical_json(indications), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("药品编号已存在，或同一通用名/剂型/规格已登记") from exc
                self._audit(connection, actor_id=actor_id, action="medication.registered",
                            resource_type="medication", resource_id=medication_id,
                            detail={"organization_id": organization_id, "generic_name": generic_name,
                                    "dosage_form": dosage_form, "strength": strength,
                                    "storage_required": storage_required})
                return "medication", medication_id, {"medication_id": medication_id}

            return self._ok(self._idempotent(connection, request_id=request_id,
                                    action="register_medication", payload=payload, create=create))

    def register_alias(self, *, request_id: str, actor_id: str, organization_id: str,
                       medication_id: str, alias: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "organization_id": organization_id,
                   "medication_id": medication_id, "alias": alias}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "physician")
            organization_id = self._id(organization_id, "organization_id")
            medication_id = self._id(medication_id, "medication_id")
            alias = self._text(alias, "alias", 200)
            self._medication(connection, organization_id, medication_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO medication_aliases(organization_id,alias,medication_id,created_at) "
                        "VALUES(?,?,?,?)",
                        (organization_id, alias, medication_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该别名已经归并到某个药品身份") from exc
                self._audit(connection, actor_id=actor_id, action="medication.alias_registered",
                            resource_type="medication", resource_id=medication_id,
                            detail={"organization_id": organization_id, "alias": alias})
                return "medication_alias", f"{medication_id}:{alias}", {"medication_id": medication_id, "alias": alias}

            return self._ok(self._idempotent(connection, request_id=request_id,
                                    action="register_medication_alias", payload=payload, create=create))

    def register_substitute(self, *, request_id: str, actor_id: str, organization_id: str,
                            medication_id: str, substitute_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "organization_id": organization_id,
                   "medication_id": medication_id, "substitute_id": substitute_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "physician")
            organization_id = self._id(organization_id, "organization_id")
            medication_id = self._id(medication_id, "medication_id")
            substitute_id = self._id(substitute_id, "substitute_id")
            if medication_id == substitute_id:
                raise ValidationError("替代关系必须指向另一种药品")
            self._medication(connection, organization_id, medication_id)
            self._medication(connection, organization_id, substitute_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO medication_substitutes(organization_id,medication_id,substitute_id,created_at) "
                        "VALUES(?,?,?,?)",
                        (organization_id, medication_id, substitute_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("替代关系已存在") from exc
                self._audit(connection, actor_id=actor_id, action="medication.substitute_registered",
                            resource_type="medication", resource_id=medication_id,
                            detail={"organization_id": organization_id, "substitute_id": substitute_id})
                return "medication_substitute", f"{medication_id}:{substitute_id}", {
                    "medication_id": medication_id, "substitute_id": substitute_id}

            return self._ok(self._idempotent(connection, request_id=request_id,
                                    action="register_substitute", payload=payload, create=create))

    def configure_site_storage(self, *, request_id: str, actor_id: str, site_id: str,
                               storage_modes: list[str]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "site_id": site_id, "storage_modes": storage_modes}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator", "logistician")
            site = self._site(connection, site_id)
            self._same_org_site(actor, site)
            if not isinstance(storage_modes, list) or not storage_modes:
                raise ValidationError("storage_modes 必须是非空列表")
            bad = [m for m in storage_modes if m not in STORAGE_MODES]
            if bad:
                raise ValidationError("存在非法储存条件：" + ",".join(bad))

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute("DELETE FROM site_storage_capabilities WHERE site_id=?", (site_id,))
                connection.executemany(
                    "INSERT INTO site_storage_capabilities(site_id,storage_mode) VALUES(?,?)",
                    [(site_id, mode) for mode in sorted(set(storage_modes))],
                )
                self._audit(connection, actor_id=actor_id, action="site_storage.configured",
                            resource_type="site", resource_id=site_id,
                            detail={"storage_modes": sorted(set(storage_modes))})
                response = {"site_id": site_id, "storage_modes": sorted(set(storage_modes))}
                return "site_storage", site_id, response

            return self._ok(self._idempotent(connection, request_id=request_id,
                                    action="configure_site_storage", payload=payload, create=create))

    def configure_medication_policy(self, *, request_id: str, actor_id: str, site_id: str,
                                    medication_id: str, emergency_reserve: float,
                                    reorder_point: float = 0,
                                    next_resupply_date: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "site_id": site_id, "medication_id": medication_id,
                   "emergency_reserve": emergency_reserve, "reorder_point": reorder_point,
                   "next_resupply_date": next_resupply_date}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator", "physician", "logistician")
            site = self._site(connection, site_id)
            self._same_org_site(actor, site)
            med = self._medication(connection, site["organization_id"], medication_id)
            emergency_reserve = self._qty(emergency_reserve, "emergency_reserve")
            reorder_point = self._qty(reorder_point, "reorder_point")
            if next_resupply_date:
                date.fromisoformat(next_resupply_date)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO site_medication_policy(site_id,medication_id,emergency_reserve,"
                    "reorder_point,next_resupply_date) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(site_id,medication_id) DO UPDATE SET emergency_reserve=excluded.emergency_reserve,"
                    "reorder_point=excluded.reorder_point,next_resupply_date=excluded.next_resupply_date",
                    (site_id, medication_id, emergency_reserve, reorder_point, next_resupply_date),
                )
                self._audit(connection, actor_id=actor_id, action="medication_policy.configured",
                            resource_type="site_medication_policy",
                            resource_id=f"{site_id}:{medication_id}",
                            detail={"emergency_reserve": emergency_reserve,
                                    "reorder_point": reorder_point,
                                    "next_resupply_date": next_resupply_date})
                response = {"site_id": site_id, "medication_id": med["medication_id"],
                            "emergency_reserve": emergency_reserve, "reorder_point": reorder_point,
                            "next_resupply_date": next_resupply_date}
                return "medication_policy", f"{site_id}:{medication_id}", response

            return self._ok(self._idempotent(connection, request_id=request_id,
                                    action="configure_medication_policy", payload=payload,
                                    create=create))

    def set_prescriber_profile(self, *, request_id: str, actor_id: str,
                               prescriber_actor_id: str, controlled_level_max: int) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "prescriber_actor_id": prescriber_actor_id,
                   "controlled_level_max": controlled_level_max}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin")
            prescriber = self._actor(connection, prescriber_actor_id)
            if not isinstance(controlled_level_max, int) or not 0 <= controlled_level_max <= 3:
                raise ValidationError("controlled_level_max 必须是 0 到 3 的整数")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO prescriber_profiles(actor_id,controlled_level_max) VALUES(?,?) "
                    "ON CONFLICT(actor_id) DO UPDATE SET controlled_level_max=excluded.controlled_level_max",
                    (prescriber_actor_id, controlled_level_max),
                )
                self._audit(connection, actor_id=actor_id, action="prescriber_profile.configured",
                            resource_type="actor", resource_id=prescriber_actor_id,
                            detail={"controlled_level_max": controlled_level_max})
                return "prescriber_profile", prescriber_actor_id, {
                    "actor_id": prescriber_actor_id, "controlled_level_max": controlled_level_max}

            return self._ok(self._idempotent(connection, request_id=request_id,
                                    action="set_prescriber_profile", payload=payload, create=create))

    def register_patient(self, *, request_id: str, actor_id: str, site_id: str,
                         patient_id: str, display_name: str,
                         allergies: list[str] | None = None) -> dict[str, Any]:
        allergies = allergies or []
        if not isinstance(allergies, list) or not all(isinstance(a, str) and a.strip() for a in allergies):
            raise ValidationError("allergies 必须是字符串列表")
        payload = {"actor_id": actor_id, "site_id": site_id, "patient_id": patient_id,
                   "display_name": display_name, "allergies": allergies}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "physician")
            site = self._site(connection, site_id)
            self._same_org_site(actor, site)
            patient_id = self._id(patient_id, "patient_id")
            display_name = self._text(display_name, "display_name", 100)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO patients(patient_id,site_id,display_name,allergies_json,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (patient_id, site_id, display_name, canonical_json(allergies), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("患者编号已存在") from exc
                # 审计中的患者标识做最小化登记，不写姓名与过敏明细。
                self._audit(connection, actor_id=actor_id, action="patient.registered",
                            resource_type="patient", resource_id=patient_id,
                            detail={"site_id": site_id, "allergy_count": len(allergies)})
                return "patient", patient_id, {"patient_id": patient_id, "site_id": site_id}

            return self._ok(self._idempotent(connection, request_id=request_id,
                                    action="register_patient", payload=payload, create=create))

    # ------------------------------------------------------------------ 批准

    def grant_approval(self, *, request_id: str, actor_id: str, request_kind: str,
                       reason: str, subject_id: str | None = None,
                       approval_id: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "request_kind": request_kind, "reason": reason,
                   "subject_id": subject_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            # 动用应急量/近效期替代品由医疗负责人批准；跨站调拨由后勤/管理负责人批准。
            if request_kind in ("emergency_reserve", "near_expiry_substitute"):
                self._require_roles(actor, "admin", "physician")
            elif request_kind == "cross_site_transfer":
                self._require_roles(actor, "admin", "logistician")
            else:
                raise ValidationError("request_kind 非法")
            reason = self._text(reason, "reason")
            approval_id = self._id(approval_id or f"ap-{uuid.uuid4().hex[:16]}", "approval_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO approvals(approval_id,organization_id,request_kind,reason,"
                        "approver_id,subject_id,created_at) VALUES(?,?,?,?,?,?,?)",
                        (approval_id, actor["organization_id"], request_kind, reason,
                         actor_id, subject_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("批准编号已存在") from exc
                self._audit(connection, actor_id=actor_id, action="approval.granted",
                            resource_type="approval", resource_id=approval_id,
                            detail={"request_kind": request_kind, "reason": reason,
                                    "subject_id": subject_id})
                return "approval", approval_id, {"approval_id": approval_id,
                                                  "request_kind": request_kind}

            return self._ok(self._idempotent(connection, request_id=request_id,
                                    action="grant_approval", payload=payload, create=create))

    def _consume_approval(self, connection, *, actor, approval_id: str, kind: str,
                          ref_type: str, ref_id: str) -> None:
        approval = connection.execute(
            "SELECT * FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()
        if approval is None:
            raise NotFoundError("批准不存在")
        if approval["organization_id"] != actor["organization_id"]:
            raise PermissionDenied("批准属于其他组织")
        if approval["request_kind"] != kind:
            raise ValidationError(f"批准类型不符：需要 {kind}")
        try:
            connection.execute(
                "INSERT INTO approval_usages(approval_id,ref_type,ref_id,used_at) VALUES(?,?,?,?)",
                (approval_id, ref_type, ref_id, self._now()),
            )
        except Exception as exc:
            raise ConflictError("该批准已被使用，不能重复作为扣减依据") from exc

    # ------------------------------------------------------------------ 入库

    def intake_batch(self, *, request_id: str, actor_id: str, site_id: str,
                     medication_ref: str, batch_number: str, quantity: float,
                     expiry_date: str, storage_required: str | None = None,
                     unit: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "site_id": site_id, "medication_ref": medication_ref,
                   "batch_number": batch_number, "quantity": quantity, "expiry_date": expiry_date,
                   "storage_required": storage_required, "unit": unit}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator", "logistician")
            site = self._site(connection, site_id)
            self._same_org_site(actor, site)
            med, via_alias = self._resolve_medication(connection, site["organization_id"], medication_ref)
            batch_number = self._text(batch_number, "batch_number", 120)
            quantity = self._qty(quantity, "quantity", minimum=0.000001)
            try:
                expiry = date.fromisoformat(str(expiry_date))
            except ValueError as exc:
                raise ValidationError("expiry_date 必须是 YYYY-MM-DD") from exc
            if expiry <= self._today():
                raise ValidationError("入库批次必须晚于当天失效")
            storage_required = storage_required or med["storage_required"]
            if storage_required not in STORAGE_MODES:
                raise ValidationError("storage_required 必须是 room/refrigerated/frozen")
            capability = {r["storage_mode"] for r in connection.execute(
                "SELECT storage_mode FROM site_storage_capabilities WHERE site_id=?", (site_id,))}
            if storage_required not in capability:
                raise PermissionDenied(
                    f"站点缺少{STORAGE_LABELS[storage_required]}储存条件，不能接收该批次；请先配置站点储存能力")

            def create() -> tuple[str, str, dict[str, Any]]:
                batch_id = uuid.uuid4().hex
                # 批次以 0 余额建档，入库数量由账本首笔带入，保证账本是唯一数量事实来源。
                try:
                    connection.execute(
                        "INSERT INTO medication_batches(batch_id,organization_id,medication_id,site_id,"
                        "batch_number,quantity_on_hand,unit,expiry_date,storage_required,status,received_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?, 'active', ?)",
                        (batch_id, site["organization_id"], med["medication_id"], site_id, batch_number,
                         0, unit or med["unit"], expiry_date, storage_required, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("同站点同药品的批号已经存在") from exc
                self._ledger(connection, entry_type="intake", batch_row=self._batch(connection, batch_id),
                             delta=quantity, ref_id=batch_id, actor_id=actor_id, reason="入库")
                self._audit(connection, actor_id=actor_id, action="batch.intaken",
                            resource_type="batch", resource_id=batch_id,
                            detail={"site_id": site_id, "medication_id": med["medication_id"],
                                    "resolved_via_alias": via_alias, "batch_number": batch_number,
                                    "quantity": quantity, "expiry_date": expiry_date,
                                    "storage_required": storage_required})
                return "batch", batch_id, {"batch_id": batch_id, "medication_id": med["medication_id"],
                                           "site_id": site_id, "quantity_on_hand": quantity}

            return self._ok(self._idempotent(connection, request_id=request_id,
                                    action="intake_batch", payload=payload, create=create))

    # ------------------------------------------------------------------ 账本

    def _batch(self, connection, batch_id: str):
        row = connection.execute("SELECT * FROM medication_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        return row

    def _held(self, connection, batch_id: str) -> float:
        row = connection.execute(
            "SELECT COALESCE(SUM(quantity),0) AS held FROM reservations WHERE batch_id=? AND status='held'",
            (batch_id,)).fetchone()
        return round(float(row["held"]), 6)

    def _ledger(self, connection, *, entry_type: str, batch_row, delta: float,
                actor_id: str, ref_id: str | None = None, reason: str | None = None,
                approval_id: str | None = None, held_change: float = 0) -> dict[str, Any]:
        new_balance = round(batch_row["quantity_on_hand"] + delta, 6)
        if new_balance < -1e-9:
            raise ConflictError("批次可用数量不足，账本余额不能为负")
        new_balance = max(new_balance, 0.0)
        connection.execute(
            "UPDATE medication_batches SET quantity_on_hand=? WHERE batch_id=?",
            (new_balance, batch_row["batch_id"]),
        )
        held_after = round(self._held(connection, batch_row["batch_id"]) + held_change, 6)
        cur = connection.execute(
            "INSERT INTO inventory_ledger(organization_id,site_id,batch_id,medication_id,entry_type,"
            "delta_qty,balance_after,held_after,ref_type,ref_id,reason,approval_id,actor_id,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (batch_row["organization_id"], batch_row["site_id"], batch_row["batch_id"],
             batch_row["medication_id"], entry_type, delta, new_balance, held_after,
             LEDGER_REF[entry_type], ref_id, reason, approval_id, actor_id, self._now()),
        )
        return {"sequence": cur.lastrowid, "balance_after": new_balance, "held_after": held_after}

    def verify_ledger(self, site_id: str | None = None) -> dict[str, Any]:
        """逐批重放账本，核对余额连续且与现存量一致。"""

        query = "SELECT DISTINCT batch_id FROM inventory_ledger"
        parameters: list[Any] = []
        if site_id:
            query += " WHERE site_id=?"
            parameters.append(site_id)
        checked = 0
        with self.database.transaction() as connection:
            for row in connection.execute(query, parameters):
                entries = connection.execute(
                    "SELECT * FROM inventory_ledger WHERE batch_id=? ORDER BY sequence", (row["batch_id"],)
                ).fetchall()
                balance = 0.0
                prev_sequence = None
                for entry in entries:
                    if prev_sequence is not None and entry["sequence"] <= prev_sequence:
                        raise ConflictError("账本序列不连续")
                    balance = round(balance + entry["delta_qty"], 6)
                    if abs(balance - entry["balance_after"]) > 1e-6:
                        raise ConflictError(f"批次 {row['batch_id']} 余额与账本重放不一致")
                    prev_sequence = entry["sequence"]
                batch = self._batch(connection, row["batch_id"])
                if abs(balance - batch["quantity_on_hand"]) > 1e-6:
                    raise ConflictError(f"批次 {row['batch_id']} 现存量与账本不一致")
                checked += 1
        return {"valid": True, "batches_checked": checked}

    # ------------------------------------------------------------------ 处方决策

    def record_prescription(self, *, request_id: str, actor_id: str, site_id: str,
                            patient_id: str, medication_ref: str, quantity: float,
                            indication: str, allow_substitute: bool = False,
                            emergency_use: bool = False,
                            prescription_id: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "site_id": site_id, "patient_id": patient_id,
                   "medication_ref": medication_ref, "quantity": quantity, "indication": indication,
                   "allow_substitute": allow_substitute, "emergency_use": emergency_use}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "physician")
            site = self._site(connection, site_id)
            self._same_org_site(actor, site)
            patient = self._patient(connection, site_id, patient_id)
            med, via_alias = self._resolve_medication(connection, site["organization_id"], medication_ref)
            quantity = self._qty(quantity, "quantity", minimum=0.000001)
            indication = self._text(indication, "indication", 200)
            prescription_id = self._id(prescription_id or f"rx-{uuid.uuid4().hex[:16]}", "prescription_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO prescriptions(prescription_id,organization_id,site_id,patient_id,"
                        "medication_id,quantity,indication,allow_substitute,emergency_use,prescriber_id,"
                        "status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?, 'recorded', ?)",
                        (prescription_id, site["organization_id"], site_id, patient_id, med["medication_id"],
                         quantity, indication, 1 if allow_substitute else 0, 1 if emergency_use else 0,
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("医嘱编号已存在") from exc
                self._audit(connection, actor_id=actor_id, action="prescription.recorded",
                            resource_type="prescription", resource_id=prescription_id,
                            detail={"site_id": site_id, "medication_id": med["medication_id"],
                                    "resolved_via_alias": via_alias, "quantity": quantity,
                                    "indication": indication, "allow_substitute": allow_substitute,
                                    "emergency_use": emergency_use})
                return "prescription", prescription_id, {"prescription_id": prescription_id,
                                                         "medication_id": med["medication_id"]}

            return self._ok(self._idempotent(connection, request_id=request_id,
                                    action="record_prescription", payload=payload, create=create))

    def amend_prescription(self, *, request_id: str, actor_id: str, prescription_id: str,
                           note: str, clinical_data: dict[str, Any] | None = None) -> dict[str, Any]:
        """追加后补诊疗信息；原处方与发放事实保持不变。"""

        clinical_data = clinical_data or {}
        if not isinstance(clinical_data, dict):
            raise ValidationError("clinical_data 必须是对象")
        payload = {"actor_id": actor_id, "prescription_id": prescription_id, "note": note,
                   "clinical_data": clinical_data}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "physician", "reviewer")
            rx = connection.execute(
                "SELECT * FROM prescriptions WHERE prescription_id=?", (prescription_id,)).fetchone()
            if rx is None:
                raise NotFoundError("医嘱不存在")
            site = self._site(connection, rx["site_id"])
            self._same_org_site(actor, site)
            note = self._text(note, "note")

            def create() -> tuple[str, str, dict[str, Any]]:
                amendment_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO prescription_amendments(amendment_id,prescription_id,note,payload_json,"
                    "amended_by,created_at) VALUES(?,?,?,?,?,?)",
                    (amendment_id, prescription_id, note, canonical_json(clinical_data),
                     actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="prescription.amended",
                            resource_type="prescription_amendment", resource_id=amendment_id,
                            detail={"prescription_id": prescription_id,
                                    "data_keys": sorted(clinical_data.keys())})
                return "prescription_amendment", amendment_id, {"amendment_id": amendment_id,
                                                                "prescription_id": prescription_id}

            return self._ok(self._idempotent(connection, request_id=request_id,
                                    action="amend_prescription", payload=payload, create=create))

    def list_amendments(self, prescription_id: str) -> list[dict[str, Any]]:
        import json

        rows = self.database.connection.execute(
            "SELECT * FROM prescription_amendments WHERE prescription_id=? ORDER BY created_at, amendment_id",
            (prescription_id,)).fetchall()
        return [{"amendment_id": r["amendment_id"], "prescription_id": r["prescription_id"],
                 "note": r["note"], "clinical_data": json.loads(r["payload_json"]),
                 "amended_by": r["amended_by"], "created_at": r["created_at"]} for r in rows]

    def _eligible_batches(self, connection, *, organization_id: str, site_id: str,
                          medication_id: str) -> list[Any]:
        today = self._today()
        rows = connection.execute(
            "SELECT * FROM medication_batches WHERE organization_id=? AND site_id=? AND medication_id=? "
            "AND status='active' AND quantity_on_hand>0 AND expiry_date>=? "
            "ORDER BY expiry_date, received_at, batch_id",
            (organization_id, site_id, medication_id, today.isoformat())).fetchall()
        capability = {r["storage_mode"] for r in connection.execute(
            "SELECT storage_mode FROM site_storage_capabilities WHERE site_id=?", (site_id,))}
        return [r for r in rows if r["storage_required"] in capability]

    def _policy(self, connection, site_id: str, medication_id: str) -> dict[str, float]:
        row = connection.execute(
            "SELECT * FROM site_medication_policy WHERE site_id=? AND medication_id=?",
            (site_id, medication_id)).fetchone()
        if row is None:
            return {"emergency_reserve": 0.0, "reorder_point": 0.0, "next_resupply_date": None}
        return {"emergency_reserve": float(row["emergency_reserve"]),
                "reorder_point": float(row["reorder_point"]),
                "next_resupply_date": row["next_resupply_date"]}

    def evaluate_prescription(self, prescription_id: str) -> dict[str, Any]:
        """不写库，返回这份医嘱此刻为何可发或被阻止。"""

        with self.database.transaction() as connection:
            plan, decision = self._plan_dispense(connection, prescription_id=prescription_id,
                                                 approval_id=None, enforce_approval=False)
            return decision

    def _check_clinical(self, connection, actor, site, patient, med, indication: str) -> list[str]:
        blockers: list[str] = []
        import json

        indications = json.loads(med["indications_json"])
        if indications and indication not in indications:
            blockers.append(f"适应证 {indication} 不在 {med['generic_name']} 登记范围内")
        allergies = json.loads(patient["allergies_json"])
        hits = self._allergy_hits(connection, site["organization_id"], med["medication_id"], allergies)
        if hits:
            blockers.append("患者对该药相关名称过敏：" + "、".join(sorted(set(hits))))
        profile = connection.execute(
            "SELECT controlled_level_max FROM prescriber_profiles WHERE actor_id=?", (actor["actor_id"],)
        ).fetchone()
        level_max = profile["controlled_level_max"] if profile else 0
        if med["controlled_level"] > level_max:
            blockers.append(
                f"开方者管制药权限等级 {level_max} 低于该药要求 {med['controlled_level']}")
        return blockers

    def _plan_dispense(self, connection, *, prescription_id: str, approval_id: str | None,
                       enforce_approval: bool):
        """返回（批次计划, 决策说明）。计划元素为 (batch, qty, emergency_portion)。"""

        rx = connection.execute("SELECT * FROM prescriptions WHERE prescription_id=?",
                                (prescription_id,)).fetchone()
        if rx is None:
            raise NotFoundError("医嘱不存在")
        site = self._site(connection, rx["site_id"])
        patient = self._patient(connection, rx["site_id"], rx["patient_id"])
        prescriber = self._actor(connection, rx["prescriber_id"])
        requested_med = self._medication(connection, rx["organization_id"], rx["medication_id"])
        need = round(float(rx["quantity"]), 6)
        blockers: list[str] = []
        warnings: list[str] = []
        approvals_needed: list[str] = []

        near_limit = self._today() + timedelta(days=NEAR_EXPIRY_DAYS)

        def build_plan(med, *, allow_emergency: bool) -> tuple[list[tuple[Any, float, float]], float, float]:
            """按 FEFO 先常规量后应急量分配，返回 (计划, 缺口, 应急用量)。"""

            batches = self._eligible_batches(
                connection, organization_id=rx["organization_id"], site_id=rx["site_id"],
                medication_id=med["medication_id"])
            policy = self._policy(connection, rx["site_id"], med["medication_id"])
            reserve = policy["emergency_reserve"]
            total_available = 0.0
            candidates = []
            for batch in batches:
                held = self._held(connection, batch["batch_id"])
                avail = max(0.0, round(batch["quantity_on_hand"] - held, 6))
                if avail <= 0:
                    continue
                total_available = round(total_available + avail, 6)
                candidates.append((batch, avail))
            emergency_pool = round(max(0.0, min(total_available, reserve)), 6)
            general_pool = round(max(0.0, total_available - emergency_pool), 6)
            remaining = need
            plan: list[tuple[Any, float, float]] = []
            used_emergency = 0.0
            for batch, avail in candidates:
                if remaining <= 0:
                    break
                take = min(avail, general_pool, remaining)
                if take > 0:
                    plan.append((batch, take, 0.0))
                    general_pool = round(general_pool - take, 6)
                    remaining = round(remaining - take, 6)
            if allow_emergency:
                for batch, avail in candidates:
                    if remaining <= 0:
                        break
                    planned_here = sum(q for b, q, _ in plan if b["batch_id"] == batch["batch_id"])
                    room = round(avail - planned_here, 6)
                    take = min(room, emergency_pool - used_emergency, remaining)
                    if take > 0:
                        plan.append((batch, take, take))
                        used_emergency = round(used_emergency + take, 6)
                        remaining = round(remaining - take, 6)
            return plan, round(remaining, 6), used_emergency

        # 候选药按优先级排列：本药 → 登记的可替代品；临床规则逐药判定，
        # 例如本药过敏但替代品无过敏时，允许改走替代品。
        candidates: list[tuple[Any, list[str]]] = [
            (requested_med, self._check_clinical(connection, prescriber, site, patient,
                                                 requested_med, rx["indication"]))]
        if rx["allow_substitute"]:
            for row in connection.execute(
                    "SELECT substitute_id FROM medication_substitutes WHERE organization_id=? AND medication_id=? "
                    "ORDER BY substitute_id",
                    (rx["organization_id"], requested_med["medication_id"])).fetchall():
                sub_med = self._medication(connection, rx["organization_id"], row["substitute_id"])
                candidates.append((sub_med, self._check_clinical(
                    connection, prescriber, site, patient, sub_med, rx["indication"])))
        clinical_notes = {med["medication_id"]: msgs for med, msgs in candidates if msgs}
        valid_candidates = [med for med, msgs in candidates if not msgs]
        if not valid_candidates:
            for med, msgs in candidates:
                blockers.extend(f"{med['generic_name']}：{msg}" for msg in msgs)

        # 选择次序：本药常规 → 替代品常规 → 本药应急 → 替代品应急。
        # 常规处方先耗尽自己的合格常规库存与替代品，急救储备始终排在最后。
        ordered: list[tuple[Any, bool]] = []
        for med in valid_candidates:
            if med["medication_id"] == requested_med["medication_id"]:
                ordered.insert(0, (med, False))
            else:
                ordered.append((med, False))
        for med in valid_candidates:
            ordered.append((med, True))

        plan: list[tuple[Any, float, float]] = []
        chosen_med = requested_med
        chosen_emergency = 0.0
        shortage = True
        for med, allow_emergency in ordered:
            candidate_plan, shortfall, emergency_used = build_plan(med, allow_emergency=allow_emergency)
            if shortfall <= 0 and candidate_plan:
                plan = candidate_plan
                chosen_med = med
                chosen_emergency = emergency_used
                shortage = False
                break

        used_medication_id = chosen_med["medication_id"]
        substituted = chosen_med["medication_id"] != requested_med["medication_id"]
        near_expiry_substitute = substituted and any(
            b["expiry_date"] <= near_limit.isoformat() for b, _, _ in plan)
        if near_expiry_substitute:
            approvals_needed.append("near_expiry_substitute")
        if chosen_emergency > 0:
            approvals_needed.append("emergency_reserve")
        # 本药近效期批次按 FEFO 正常先发，仅作提示；替代品近效期才需批准。
        for b, _, _ in plan:
            if not substituted and b["expiry_date"] <= near_limit.isoformat():
                warnings.append(f"批次 {b['batch_number']} 临近效期（{b['expiry_date']}），按 FEFO 优先发放")
        for med_id, msgs in clinical_notes.items():
            if med_id != used_medication_id:
                warnings.append(f"候选药 {med_id} 未选用：" + "；".join(msgs))

        emergency_qty = chosen_emergency
        if shortage:
            total = self._site_totals(connection, rx["site_id"], requested_med["medication_id"])
            blockers.append(
                f"合格库存不足：需要 {need:g}，本药常规可用 {total['general_available']:g}、"
                f"应急锁定 {total['emergency_reserve_locked']:g}"
                + ("，且无合格替代品可补足" if rx["allow_substitute"] else "，医嘱未允许替代品"))

        approval_ids: list[str] = []
        if enforce_approval and approvals_needed:
            kinds = sorted(set(approvals_needed))
            if not approval_id:
                blockers.append("该发放需要批准，缺少 approval_id；需要类型：" + "、".join(kinds))
            else:
                raw_ids = [v.strip() for v in str(approval_id).split(",") if v.strip()]
                by_kind: dict[str, str] = {}
                for ap_id in raw_ids:
                    approval = connection.execute(
                        "SELECT * FROM approvals WHERE approval_id=?", (ap_id,)).fetchone()
                    if approval is None:
                        blockers.append(f"批准 {ap_id} 不存在")
                        continue
                    if approval["organization_id"] != rx["organization_id"]:
                        blockers.append(f"批准 {ap_id} 属于其他组织")
                        continue
                    if connection.execute(
                            "SELECT 1 FROM approval_usages WHERE approval_id=?", (ap_id,)).fetchone():
                        blockers.append(f"批准 {ap_id} 已被使用，不能重复扣减")
                        continue
                    by_kind.setdefault(approval["request_kind"], ap_id)
                missing = [kind for kind in kinds if kind not in by_kind]
                if missing:
                    blockers.append("缺少以下类型的有效批准：" + "、".join(missing))
                approval_ids = [by_kind[kind] for kind in kinds if kind in by_kind]
        if not enforce_approval and approvals_needed:
            warnings.append("执行时必须提供批准：" + "、".join(sorted(set(approvals_needed))))

        decision = {
            "prescription_id": prescription_id,
            "site_id": rx["site_id"],
            "requested_medication_id": requested_med["medication_id"],
            "dispense_medication_id": used_medication_id,
            "quantity_needed": need,
            "allowed": not blockers,
            "blocked": bool(blockers),
            "blockers": blockers,
            "warnings": sorted(set(warnings)),
            "approvals_required": sorted(set(approvals_needed)),
            "approval_ids": sorted(set(approval_ids)),
            "substituted": substituted,
            "uses_emergency_reserve": emergency_qty > 0,
            "emergency_quantity": emergency_qty,
            "near_expiry_substitute": near_expiry_substitute,
            "plan": [{"batch_id": b["batch_id"], "batch_number": b["batch_number"],
                      "quantity": round(q, 6), "expiry_date": b["expiry_date"],
                      "from_emergency_reserve": p > 0} for b, q, p in plan],
        }
        return plan, decision

    def _general_shares(self, connection, site_id: str, medication_id: str) -> dict[str, float]:
        """按 FEFO 计算每个批次可作常规（非应急）用途的数量，与发放引擎口径一致。"""

        org_id = self._site(connection, site_id)["organization_id"]
        batches = self._eligible_batches(connection, organization_id=org_id,
                                         site_id=site_id, medication_id=medication_id)
        total = 0.0
        avails: list[tuple[str, float]] = []
        for b in batches:
            avail = max(0.0, round(b["quantity_on_hand"] - self._held(connection, b["batch_id"]), 6))
            if avail > 0:
                total = round(total + avail, 6)
                avails.append((b["batch_id"], avail))
        reserve = self._policy(connection, site_id, medication_id)["emergency_reserve"]
        general_pool = round(max(0.0, total - min(total, reserve)), 6)
        shares: dict[str, float] = {}
        remaining = general_pool
        for batch_id, avail in avails:
            share = min(avail, remaining)
            shares[batch_id] = share
            remaining = round(remaining - share, 6)
        return shares

    def _site_totals(self, connection, site_id: str, medication_id: str) -> dict[str, float]:
        batches = self._eligible_batches(
            connection, organization_id=self._site(connection, site_id)["organization_id"],
            site_id=site_id, medication_id=medication_id)
        on_hand = available = 0.0
        for b in batches:
            on_hand = round(on_hand + b["quantity_on_hand"], 6)
            available = round(available + max(0.0, b["quantity_on_hand"] - self._held(connection, b["batch_id"])), 6)
        policy = self._policy(connection, site_id, medication_id)
        locked = round(min(available, policy["emergency_reserve"]), 6)
        return {"on_hand": on_hand, "available": available,
                "emergency_reserve_locked": locked,
                "general_available": round(available - locked, 6)}

    def reserve_prescription(self, *, request_id: str, actor_id: str, prescription_id: str,
                             approval_id: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "prescription_id": prescription_id, "approval_id": approval_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "physician", "operator")
            rx = connection.execute("SELECT * FROM prescriptions WHERE prescription_id=?",
                                    (prescription_id,)).fetchone()
            if rx is None:
                raise NotFoundError("医嘱不存在")
            site = self._site(connection, rx["site_id"])
            self._same_org_site(actor, site)
            if rx["status"] != "recorded":
                raise ConflictError("医嘱已预留或已发放，不能重复预留")
            if connection.execute(
                    "SELECT 1 FROM reservations WHERE prescription_id=? AND status='held'",
                    (prescription_id,)).fetchone():
                raise ConflictError("该医嘱已有有效预留")
            # 预留即按临床规则选批次；需要应急量或近效期替代品时必须当场出示批准。
            plan, decision = self._plan_dispense(connection, prescription_id=prescription_id,
                                                 approval_id=approval_id, enforce_approval=True)
            if decision["blocked"]:
                raise PermissionDenied("预留被阻止：" + "；".join(decision["blockers"]))
            approval_map = dict(zip(decision["approvals_required"], decision["approval_ids"]))

            def create() -> tuple[str, str, dict[str, Any]]:
                reservation_ids = []
                for batch, qty, _emergency in plan:
                    reservation_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO reservations(reservation_id,organization_id,site_id,prescription_id,"
                        "batch_id,quantity,status,approvals_json,created_at) "
                        "VALUES(?,?,?,?,?,?,'held',?,?)",
                        (reservation_id, rx["organization_id"], rx["site_id"], prescription_id,
                         batch["batch_id"], qty, canonical_json(approval_map), self._now()),
                    )
                    fresh = self._batch(connection, batch["batch_id"])
                    self._ledger(connection, entry_type="reserve", batch_row=fresh, delta=0.0,
                                 ref_id=reservation_id, actor_id=actor_id,
                                 reason=f"为医嘱 {prescription_id} 预留",
                                 approval_id=approval_id, held_change=qty)
                    reservation_ids.append(reservation_id)
                connection.execute("UPDATE prescriptions SET status='reserved', decision_json=? WHERE prescription_id=?",
                                   (canonical_json(decision), prescription_id))
                self._audit(connection, actor_id=actor_id, action="prescription.reserved",
                            resource_type="prescription", resource_id=prescription_id,
                            detail={"site_id": rx["site_id"], "decision": decision})
                return "reservation", reservation_ids[0], {
                    "prescription_id": prescription_id, "reservations": reservation_ids,
                    "decision": decision}

            return self._ok(self._idempotent(connection, request_id=request_id,
                                    action="reserve_prescription", payload=payload, create=create))

    def release_reservation(self, *, request_id: str, actor_id: str, prescription_id: str,
                            reason: str) -> dict[str, Any]:
        """释放尚未发放的预留，批次回到可发放池。"""

        payload = {"actor_id": actor_id, "prescription_id": prescription_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "physician", "operator")
            rx = connection.execute("SELECT * FROM prescriptions WHERE prescription_id=?",
                                    (prescription_id,)).fetchone()
            if rx is None:
                raise NotFoundError("医嘱不存在")
            site = self._site(connection, rx["site_id"])
            self._same_org_site(actor, site)
            held = connection.execute(
                "SELECT * FROM reservations WHERE prescription_id=? AND status='held'",
                (prescription_id,)).fetchall()
            if not held:
                raise ConflictError("该医嘱没有可释放的有效预留")
            reason = self._text(reason, "reason")

            def create() -> tuple[str, str, dict[str, Any]]:
                # 先记账（此时预留仍处于 held，held_after 正确减少），再翻状态。
                for reservation in held:
                    fresh = self._batch(connection, reservation["batch_id"])
                    self._ledger(connection, entry_type="reserve_release", batch_row=fresh, delta=0.0,
                                 ref_id=reservation["reservation_id"], actor_id=actor_id,
                                 reason=f"释放预留：{reason}", held_change=-reservation["quantity"])
                    connection.execute(
                        "UPDATE reservations SET status='released' WHERE reservation_id=?",
                        (reservation["reservation_id"],))
                connection.execute(
                    "UPDATE prescriptions SET status='recorded', decision_json=NULL WHERE prescription_id=?",
                    (prescription_id,))
                self._audit(connection, actor_id=actor_id, action="prescription.reservation_released",
                            resource_type="prescription", resource_id=prescription_id,
                            detail={"site_id": rx["site_id"], "reason": reason,
                                    "released_lines": len(held)})
                return "reservation_release", prescription_id, {
                    "prescription_id": prescription_id, "released_lines": len(held)}

            return self._ok(self._idempotent(connection, request_id=request_id,
                                    action="release_reservation", payload=payload, create=create))

    def dispense_prescription(self, *, request_id: str, actor_id: str, prescription_id: str,
                              approval_id: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "prescription_id": prescription_id, "approval_id": approval_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "physician", "operator")
            rx = connection.execute("SELECT * FROM prescriptions WHERE prescription_id=?",
                                    (prescription_id,)).fetchone()
            if rx is None:
                raise NotFoundError("医嘱不存在")
            site = self._site(connection, rx["site_id"])
            self._same_org_site(actor, site)
            duplicate = connection.execute(
                "SELECT dispensation_id FROM dispensations WHERE prescription_id=?",
                (prescription_id,)).fetchone()
            if duplicate is not None:
                # 同一医嘱重试不再次扣减，直接回送原始发放事实。
                return {"dispensation_id": duplicate["dispensation_id"],
                        "prescription_id": prescription_id, "replayed": True,
                        "message": "同一医嘱只能扣减一次，已返回原始发放记录"}
            import json

            held = connection.execute(
                "SELECT * FROM reservations WHERE prescription_id=? AND status='held' "
                "ORDER BY created_at, reservation_id",
                (prescription_id,)).fetchall()
            if held:
                # 批次已在预留时按临床规则选定，发放只复核批准仍与预留一致。
                prior = json.loads(rx["decision_json"] or "{}")
                required = prior.get("approvals_required", [])
                approval_map = dict(zip(required, prior.get("approval_ids", [])))
                if len(approval_map) != len(required):
                    raise PermissionDenied("发放被阻止：预留时登记的批准不完整，请重新预留")
                if approval_id is not None:
                    presented = {v.strip() for v in approval_id.split(",") if v.strip()}
                    if set(approval_map.values()) != presented:
                        raise PermissionDenied("发放被阻止：出示的批准与预留登记的批准不一致")
                plan = [(self._batch(connection, r["batch_id"]), r["quantity"], 0.0) for r in held]
                if abs(round(sum(q for _, q, _ in plan), 6) - round(float(rx["quantity"]), 6)) > 1e-6:
                    raise PermissionDenied("发放被阻止：预留数量与医嘱不一致，请释放预留后重新开方")
                # 发放瞬间复核：预留后批次可能已被销毁、隔离或过期。
                today = self._today().isoformat()
                stale = [b["batch_number"] for b, _, _ in plan
                         if b["status"] != "active" or b["expiry_date"] < today]
                if stale:
                    raise PermissionDenied(
                        "发放被阻止：预留批次已失效（销毁/隔离/过期）：" + "、".join(stale)
                        + "；请释放预留后重新开方")
                decision = {**prior, "allowed": True, "blocked": [], "plan": [
                    {"batch_id": b["batch_id"], "batch_number": b["batch_number"],
                     "quantity": round(q, 6), "expiry_date": b["expiry_date"]}
                    for b, q, _ in plan]}
                emergency_qty = float(prior.get("emergency_quantity", 0.0))
                substituted = bool(prior.get("substituted"))
                near_sub = bool(prior.get("near_expiry_substitute"))
                used_medication_id = prior.get("dispense_medication_id", rx["medication_id"])
                reserved_path = True
            else:
                # 未预留的直发：当场执行完整临床规则与批次选择。
                plan, decision = self._plan_dispense(connection, prescription_id=prescription_id,
                                                     approval_id=approval_id, enforce_approval=True)
                if decision["blocked"]:
                    raise PermissionDenied("发放被阻止：" + "；".join(decision["blockers"]))
                approval_map = dict(zip(decision["approvals_required"], decision["approval_ids"]))
                emergency_qty = decision["emergency_quantity"]
                substituted = decision["substituted"]
                near_sub = decision["near_expiry_substitute"]
                used_medication_id = decision["dispense_medication_id"]
                reserved_path = False

            dispensation_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO dispensations(dispensation_id,prescription_id,organization_id,site_id,"
                    "patient_id,medication_id,requested_medication_id,quantity,emergency_used,substituted,"
                    "near_expiry_substitute,approval_id,prescriber_id,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (dispensation_id, prescription_id, rx["organization_id"], rx["site_id"],
                     rx["patient_id"], used_medication_id, rx["medication_id"], rx["quantity"],
                     1 if emergency_qty > 0 else 0, 1 if substituted else 0,
                     1 if near_sub else 0, approval_id, rx["prescriber_id"], self._now()),
                )
                connection.executemany(
                    "INSERT INTO dispensation_lines(dispensation_id,batch_id,quantity) VALUES(?,?,?)",
                    [(dispensation_id, b["batch_id"], q) for b, q, _ in plan],
                )
                for batch, qty, _em in plan:
                    fresh = self._batch(connection, batch["batch_id"])
                    # 先记账（held 仍包含该预留），再把预留翻成 consumed。
                    self._ledger(connection, entry_type="dispense", batch_row=fresh, delta=-qty,
                                 ref_id=dispensation_id, actor_id=actor_id,
                                 reason=f"执行医嘱 {prescription_id}",
                                 approval_id=approval_id,
                                 held_change=-qty if reserved_path else 0)
                    if reserved_path:
                        connection.execute(
                            "UPDATE reservations SET status='consumed' WHERE prescription_id=? AND batch_id=? AND status='held'",
                            (prescription_id, batch["batch_id"]))
                connection.execute(
                    "UPDATE prescriptions SET status='dispensed', decision_json=? WHERE prescription_id=?",
                    (canonical_json(decision), prescription_id))
                # 批准在实际扣减发生时一次性消费，重试不会重复扣减或重复占用。
                for kind, ap_id in approval_map.items():
                    self._consume_approval(connection, actor=actor, approval_id=ap_id, kind=kind,
                                           ref_type="dispensation", ref_id=dispensation_id)
                self._audit(connection, actor_id=actor_id, action="prescription.dispensed",
                            resource_type="dispensation", resource_id=dispensation_id,
                            detail={"site_id": rx["site_id"],
                                    "prescription_id": prescription_id,
                                    "medication_id": used_medication_id,
                                    "requested_medication_id": rx["medication_id"],
                                    "quantity": rx["quantity"],
                                    "substituted": substituted,
                                    "emergency_quantity": emergency_qty,
                                    "near_expiry_substitute": near_sub,
                                    "approval_id": approval_id,
                                    "batches": [{"batch_id": b["batch_id"], "quantity": q}
                                               for b, q, _ in plan]})
                response = {"dispensation_id": dispensation_id,
                            "prescription_id": prescription_id,
                            "replayed": False,
                            "medication_id": used_medication_id,
                            "substituted": substituted,
                            "uses_emergency_reserve": emergency_qty > 0,
                            "decision": decision}
                return "dispensation", dispensation_id, response

            receipt = self._idempotent(connection, request_id=request_id,
                                       action="dispense_prescription", payload=payload, create=create)
            return {"request_id": receipt.request_id, "dispensation_id": dispensation_id,
                    "replayed": receipt.replayed, "decision": decision}

    # ------------------------------------------------------------------ 退回 / 损耗 / 销毁

    def return_dispensed(self, *, request_id: str, actor_id: str, dispensation_id: str,
                         batch_id: str, quantity: float, reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "dispensation_id": dispensation_id,
                   "batch_id": batch_id, "quantity": quantity, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "physician", "operator")
            disp = connection.execute("SELECT * FROM dispensations WHERE dispensation_id=?",
                                      (dispensation_id,)).fetchone()
            if disp is None:
                raise NotFoundError("发放记录不存在")
            site = self._site(connection, disp["site_id"])
            self._same_org_site(actor, site)
            line = connection.execute(
                "SELECT * FROM dispensation_lines WHERE dispensation_id=? AND batch_id=?",
                (dispensation_id, batch_id)).fetchone()
            if line is None:
                raise ValidationError("该批次不属于这份发放")
            batch = self._batch(connection, batch_id)
            if batch["status"] != "active":
                raise ValidationError("批次已隔离或销毁，不能接收退回；请走损耗/销毁流程")
            if batch["expiry_date"] <= self._today().isoformat():
                raise ValidationError("退回批次已过效期，不能重新入库；请登记损耗或销毁")
            returned = connection.execute(
                "SELECT COALESCE(SUM(delta_qty),0) AS q FROM inventory_ledger "
                "WHERE entry_type='return' AND ref_id=? AND batch_id=?",
                (dispensation_id, batch_id)).fetchone()["q"]
            quantity = self._qty(quantity, "quantity", minimum=0.000001)
            if round(float(returned) + quantity, 6) > line["quantity"] + 1e-6:
                raise ValidationError("退回数量不能超过该批次在本发放中的数量")
            reason = self._text(reason, "reason")

            def create() -> tuple[str, str, dict[str, Any]]:
                self._ledger(connection, entry_type="return", batch_row=batch, delta=quantity,
                             ref_id=dispensation_id, actor_id=actor_id,
                             reason=f"患者退回：{reason}")
                self._audit(connection, actor_id=actor_id, action="dispensation.returned",
                            resource_type="dispensation", resource_id=dispensation_id,
                            detail={"batch_id": batch_id, "quantity": quantity, "reason": reason})
                return "return", f"{dispensation_id}:{batch_id}", {
                    "dispensation_id": dispensation_id, "batch_id": batch_id, "quantity": quantity}

            return self._ok(self._idempotent(connection, request_id=request_id,
                                    action="return_dispensed", payload=payload, create=create))

    def record_loss(self, *, request_id: str, actor_id: str, batch_id: str,
                    quantity: float, reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "batch_id": batch_id, "quantity": quantity, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator", "logistician")
            batch = self._batch(connection, batch_id)
            site = self._site(connection, batch["site_id"])
            self._same_org_site(actor, site)
            quantity = self._qty(quantity, "quantity", minimum=0.000001)
            held = self._held(connection, batch_id)
            if batch["quantity_on_hand"] - held + 1e-6 < quantity:
                raise ConflictError("损耗数量超过未预留现存数量")
            reason = self._text(reason, "reason")

            def create() -> tuple[str, str, dict[str, Any]]:
                self._ledger(connection, entry_type="loss", batch_row=batch, delta=-quantity,
                             ref_id=batch_id, actor_id=actor_id, reason=f"损耗：{reason}")
                self._audit(connection, actor_id=actor_id, action="batch.lost",
                            resource_type="batch", resource_id=batch_id,
                            detail={"quantity": quantity, "reason": reason})
                return "loss", f"{batch_id}:{uuid.uuid4().hex[:8]}", {
                    "batch_id": batch_id, "quantity": quantity}

            return self._ok(self._idempotent(connection, request_id=request_id,
                                    action="record_loss", payload=payload, create=create))

    def destroy_batch(self, *, request_id: str, actor_id: str, batch_id: str,
                      quantity: float, reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "batch_id": batch_id, "quantity": quantity, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "operator", "logistician")
            batch = self._batch(connection, batch_id)
            site = self._site(connection, batch["site_id"])
            self._same_org_site(actor, site)
            if batch["status"] == "destroyed":
                raise ConflictError("批次已经销毁")
            quantity = self._qty(quantity, "quantity", minimum=0.000001)
            held = self._held(connection, batch_id)
            if batch["quantity_on_hand"] - held + 1e-6 < quantity:
                raise ConflictError("销毁数量超过未预留现存数量")
            reason = self._text(reason, "reason")

            def create() -> tuple[str, str, dict[str, Any]]:
                self._ledger(connection, entry_type="destroy", batch_row=batch, delta=-quantity,
                             ref_id=batch_id, actor_id=actor_id, reason=f"销毁：{reason}")
                fresh = self._batch(connection, batch_id)
                if fresh["quantity_on_hand"] <= 1e-6:
                    connection.execute(
                        "UPDATE medication_batches SET status='destroyed' WHERE batch_id=?", (batch_id,))
                self._audit(connection, actor_id=actor_id, action="batch.destroyed",
                            resource_type="batch", resource_id=batch_id,
                            detail={"quantity": quantity, "reason": reason,
                                    "batch_closed": fresh["quantity_on_hand"] <= 1e-6})
                return "destroy", f"{batch_id}:{uuid.uuid4().hex[:8]}", {
                    "batch_id": batch_id, "quantity": quantity}

            return self._ok(self._idempotent(connection, request_id=request_id,
                                    action="destroy_batch", payload=payload, create=create))

    # ------------------------------------------------------------------ 跨站调拨

    def transfer_batch(self, *, request_id: str, actor_id: str, batch_id: str,
                       quantity: float, to_site_id: str, approval_id: str,
                       reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "batch_id": batch_id, "quantity": quantity,
                   "to_site_id": to_site_id, "approval_id": approval_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "logistician")
            batch = self._batch(connection, batch_id)
            from_site = self._site(connection, batch["site_id"])
            self._same_org_site(actor, from_site)
            to_site = self._site(connection, to_site_id)
            if to_site["organization_id"] != from_site["organization_id"]:
                raise ValidationError("跨站调拨只能在同一组织内进行")
            if to_site_id == batch["site_id"]:
                raise ValidationError("目的站点不能与来源站点相同")
            med = self._medication(connection, from_site["organization_id"], batch["medication_id"])
            capability = {r["storage_mode"] for r in connection.execute(
                "SELECT storage_mode FROM site_storage_capabilities WHERE site_id=?", (to_site_id,))}
            if batch["storage_required"] not in capability:
                raise PermissionDenied(
                    f"目的站点缺少{STORAGE_LABELS[batch['storage_required']]}储存条件，不能调入")
            quantity = self._qty(quantity, "quantity", minimum=0.000001)
            general_share = self._general_shares(connection, batch["site_id"], batch["medication_id"])
            if quantity > general_share.get(batch_id, 0.0) + 1e-6:
                raise PermissionDenied("调拨数量超过该批次未锁定的常规库存，不能动用急救储备")
            reason = self._text(reason, "reason")

            def create() -> tuple[str, str, dict[str, Any]]:
                transfer_id = uuid.uuid4().hex
                self._consume_approval(connection, actor=actor, approval_id=approval_id,
                                       kind="cross_site_transfer", ref_type="transfer",
                                       ref_id=transfer_id)
                connection.execute(
                    "INSERT INTO transfers(transfer_id,organization_id,batch_id,medication_id,quantity,"
                    "from_site_id,to_site_id,status,approval_id,created_at) "
                    "VALUES(?,?,?,?,?,?,?,'in_transit',?,?)",
                    (transfer_id, from_site["organization_id"], batch_id, batch["medication_id"],
                     quantity, from_site["site_id"], to_site_id, approval_id, self._now()),
                )
                self._ledger(connection, entry_type="transfer_out", batch_row=batch, delta=-quantity,
                             ref_id=transfer_id, actor_id=actor_id,
                             approval_id=approval_id, reason=f"调拨至 {to_site_id}：{reason}")
                self._audit(connection, actor_id=actor_id, action="transfer.created",
                            resource_type="transfer", resource_id=transfer_id,
                            detail={"batch_id": batch_id, "quantity": quantity,
                                    "from_site_id": from_site["site_id"],
                                    "to_site_id": to_site_id, "approval_id": approval_id,
                                    "reason": reason, "medication_id": med["medication_id"]})
                return "transfer", transfer_id, {"transfer_id": transfer_id, "status": "in_transit"}

            return self._ok(self._idempotent(connection, request_id=request_id,
                                    action="transfer_batch", payload=payload, create=create))

    def receive_transfer(self, *, request_id: str, actor_id: str, transfer_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "transfer_id": transfer_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "logistician", "operator")
            transfer = connection.execute("SELECT * FROM transfers WHERE transfer_id=?",
                                          (transfer_id,)).fetchone()
            if transfer is None:
                raise NotFoundError("调拨单不存在")
            if actor["organization_id"] != transfer["organization_id"]:
                raise PermissionDenied("不能接收其他组织的调拨")
            site = self._site(connection, transfer["to_site_id"])
            self._same_org_site(actor, site)

            def create() -> tuple[str, str, dict[str, Any]]:
                current = connection.execute("SELECT status FROM transfers WHERE transfer_id=?",
                                             (transfer_id,)).fetchone()
                if current["status"] != "in_transit":
                    raise ConflictError("调拨单已经接收")
                source = self._batch(connection, transfer["batch_id"])
                new_batch_id = uuid.uuid4().hex
                # 调入批次同样以 0 建档，数量由 transfer_in 账本笔带入。
                connection.execute(
                    "INSERT INTO medication_batches(batch_id,organization_id,medication_id,site_id,"
                    "batch_number,quantity_on_hand,unit,expiry_date,storage_required,status,received_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?, 'active', ?)",
                    (new_batch_id, transfer["organization_id"], transfer["medication_id"],
                     transfer["to_site_id"], source["batch_number"], 0,
                     source["unit"], source["expiry_date"], source["storage_required"], self._now()),
                )
                new_batch = self._batch(connection, new_batch_id)
                self._ledger(connection, entry_type="transfer_in", batch_row=new_batch,
                             delta=transfer["quantity"], ref_id=transfer_id, actor_id=actor_id,
                             reason=f"由 {transfer['from_site_id']} 调入")
                connection.execute(
                    "UPDATE transfers SET status='received', received_at=? WHERE transfer_id=?",
                    (self._now(), transfer_id))
                self._audit(connection, actor_id=actor_id, action="transfer.received",
                            resource_type="transfer", resource_id=transfer_id,
                            detail={"batch_id": new_batch_id, "source_batch_id": transfer["batch_id"],
                                    "quantity": transfer["quantity"],
                                    "from_site_id": transfer["from_site_id"],
                                    "to_site_id": transfer["to_site_id"]})
                return "transfer", transfer_id, {"transfer_id": transfer_id, "status": "received",
                                                 "batch_id": new_batch_id}

            return self._ok(self._idempotent(connection, request_id=request_id,
                                    action="receive_transfer", payload=payload, create=create))

    # ------------------------------------------------------------------ 查询与风险

    def site_inventory(self, site_id: str) -> dict[str, Any]:
        import json

        with self.database.transaction() as connection:
            site = self._site(connection, site_id)
            meds = connection.execute(
                "SELECT * FROM medications WHERE organization_id=? AND active=1 ORDER BY generic_name",
                (site["organization_id"],)).fetchall()
            today = self._today()
            near_limit = (today + timedelta(days=NEAR_EXPIRY_DAYS)).isoformat()
            items = []
            for med in meds:
                batches = connection.execute(
                    "SELECT * FROM medication_batches WHERE site_id=? AND medication_id=? ORDER BY expiry_date",
                    (site_id, med["medication_id"])).fetchall()
                if not batches:
                    continue
                on_hand = held = usable = usable_held = 0.0
                batch_views = []
                for b in batches:
                    held_b = self._held(connection, b["batch_id"])
                    on_hand = round(on_hand + b["quantity_on_hand"], 6)
                    held = round(held + held_b, 6)
                    expired = b["expiry_date"] < today.isoformat()
                    near_expiry = b["status"] == "active" and b["expiry_date"] <= near_limit
                    if b["status"] == "active" and not expired:
                        # 只有在效且未隔离的批次才计入可发放量。
                        usable = round(usable + b["quantity_on_hand"], 6)
                        usable_held = round(usable_held + held_b, 6)
                    batch_views.append({
                        "batch_id": b["batch_id"], "batch_number": b["batch_number"],
                        "quantity_on_hand": b["quantity_on_hand"], "held": held_b,
                        "expiry_date": b["expiry_date"], "storage_required": b["storage_required"],
                        "status": b["status"],
                        "expired": expired,
                        "near_expiry": near_expiry,
                    })
                policy = self._policy(connection, site_id, med["medication_id"])
                available = round(usable - usable_held, 6)
                locked = round(min(max(available, 0), policy["emergency_reserve"]), 6)
                items.append({
                    "medication_id": med["medication_id"], "generic_name": med["generic_name"],
                    "dosage_form": med["dosage_form"], "strength": med["strength"],
                    "unit": med["unit"], "on_hand": on_hand, "held": held,
                    "available": max(available, 0),
                    "emergency_reserve": policy["emergency_reserve"],
                    "emergency_locked": locked,
                    "general_available": round(max(available, 0) - locked, 6),
                    "reorder_point": policy["reorder_point"],
                    "next_resupply_date": policy["next_resupply_date"],
                    "batches": batch_views,
                })
            return {"site_id": site_id, "evaluated_at": self._now(), "items": items}

    def shortage_and_expiry_risks(self, site_id: str) -> dict[str, Any]:
        """供后勤提前处理：低于补货点、应急储备不达标、近效期/过期批次。"""

        inventory = self.site_inventory(site_id)
        today = self._today()
        shortage_items = []
        expiry_risks = []
        for item in inventory["items"]:
            reasons = []
            if item["general_available"] <= item["reorder_point"] and item["reorder_point"] > 0:
                reasons.append("常规可用量已低于补货点")
            if item["available"] < item["emergency_reserve"]:
                reasons.append("应急储备未达标")
            if reasons:
                shortage_items.append({"medication_id": item["medication_id"],
                                       "generic_name": item["generic_name"],
                                       "available": item["available"],
                                       "general_available": item["general_available"],
                                       "emergency_reserve": item["emergency_reserve"],
                                       "reorder_point": item["reorder_point"],
                                       "next_resupply_date": item["next_resupply_date"],
                                       "reasons": reasons})
            for b in item["batches"]:
                if b["status"] != "active" or b["quantity_on_hand"] <= 0:
                    continue
                if b["expired"]:
                    expiry_risks.append({"batch_id": b["batch_id"], "batch_number": b["batch_number"],
                                         "medication_id": item["medication_id"],
                                         "quantity_on_hand": b["quantity_on_hand"],
                                         "expiry_date": b["expiry_date"], "risk": "expired"})
                elif b["near_expiry"]:
                    expiry_risks.append({"batch_id": b["batch_id"], "batch_number": b["batch_number"],
                                         "medication_id": item["medication_id"],
                                         "quantity_on_hand": b["quantity_on_hand"],
                                         "expiry_date": b["expiry_date"], "risk": "near_expiry",
                                         "days_to_expiry": (date.fromisoformat(b["expiry_date"]) - today).days})
        return {"site_id": site_id, "evaluated_at": inventory["evaluated_at"],
                "shortage_risks": shortage_items, "expiry_risks": expiry_risks}

    # ------------------------------------------------------------------ 审计与去向核对

    def batch_destinations(self, *, actor_id: str, batch_id: str) -> dict[str, Any]:
        """核对一个批次的完整去向；审计角色看到的患者信息被脱敏。"""

        import json

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", "auditor", "physician", "logistician", "operator", "reviewer")
            batch = self._batch(connection, batch_id)
            site = self._site(connection, batch["site_id"])
            if actor["organization_id"] != site["organization_id"] and actor["role"] != "admin":
                raise PermissionDenied("不能查询其他组织的批次")
            redact = actor["role"] == "auditor"
            entries = []
            for e in connection.execute(
                    "SELECT * FROM inventory_ledger WHERE batch_id=? ORDER BY sequence", (batch_id,)):
                entries.append({"sequence": e["sequence"], "entry_type": e["entry_type"],
                                "delta_qty": e["delta_qty"], "balance_after": e["balance_after"],
                                "held_after": e["held_after"], "ref_type": e["ref_type"],
                                "ref_id": e["ref_id"], "reason": e["reason"],
                                "approval_id": e["approval_id"], "actor_id": e["actor_id"],
                                "created_at": e["created_at"]})
            destinations = []
            for d in connection.execute(
                    "SELECT dl.*, dp.prescription_id, dp.patient_id, dp.medication_id, dp.quantity, "
                    "dp.created_at AS dispensed_at FROM dispensation_lines dl "
                    "JOIN dispensations dp ON dp.dispensation_id=dl.dispensation_id "
                    "WHERE dl.batch_id=? ORDER BY dp.created_at", (batch_id,)):
                destinations.append({
                    "dispensation_id": d["dispensation_id"],
                    "prescription_id": d["prescription_id"],
                    "medication_id": d["medication_id"],
                    "quantity": d["quantity"],
                    "dispensed_at": d["dispensed_at"],
                    "patient": self._patient_view(connection, site["site_id"], d["patient_id"], redact),
                })
            transfers_out = [{"transfer_id": r["transfer_id"], "to_site_id": r["to_site_id"],
                              "quantity": r["quantity"], "status": r["status"]}
                             for r in connection.execute(
                                 "SELECT * FROM transfers WHERE batch_id=? ORDER BY created_at", (batch_id,))]
            med = self._medication(connection, batch["organization_id"], batch["medication_id"])
            return {"batch_id": batch_id, "site_id": batch["site_id"],
                    "medication_id": med["medication_id"], "generic_name": med["generic_name"],
                    "batch_number": batch["batch_number"],
                    "quantity_on_hand": batch["quantity_on_hand"],
                    "expiry_date": batch["expiry_date"], "status": batch["status"],
                    "ledger": entries, "dispensations": destinations,
                    "transfers": transfers_out, "redacted": redact}

    @staticmethod
    def _pseudonym(patient_id: str) -> str:
        import hashlib

        return "pat-" + hashlib.sha256(patient_id.encode("utf-8")).hexdigest()[:12]

    def _patient_view(self, connection, site_id: str, patient_id: str, redact: bool):
        if redact:
            return {"patient_ref": self._pseudonym(f"{site_id}:{patient_id}")}
        patient = self._patient(connection, site_id, patient_id)
        import json

        return {"patient_id": patient["patient_id"], "display_name": patient["display_name"],
                "allergies": json.loads(patient["allergies_json"])}

    def redacted_audit_events(self, *, actor_id: str, after_sequence: int = 0) -> dict[str, Any]:
        """审计链查询：审计员看到的患者身份一律用不可逆假名替换。"""

        import json

        events = self.domains.audit_events(after_sequence)
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
        redact = actor["role"] == "auditor"
        if not redact:
            return {"items": events, "redacted": False}
        for event in events:
            detail = event.get("detail") or {}
            if event["resource_type"] in ("patient",) and event["action"] == "patient.registered":
                event["resource_id"] = self._pseudonym(event["resource_id"])
                detail = {k: v for k, v in detail.items() if k != "site_id"}
            if event["action"] == "prescription.recorded":
                detail = {k: v for k, v in detail.items()}
            # 发放/预留事件不含患者姓名；如后续扩展携带患者标识，在此统一脱敏。
            for key in ("patient_id", "patient_name", "display_name"):
                if key in detail:
                    detail[key] = self._pseudonym(str(detail[key]))
            event["detail"] = detail
        return {"items": events, "redacted": True}

