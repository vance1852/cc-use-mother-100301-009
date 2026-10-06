"""运行药品与用药保障的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .errors import PermissionDenied
from .pharmacy import PharmacyService
from .storage import Database


def run() -> dict[str, object]:
    """执行建档、入库、发放、批准、调拨、审计核对的完整链路。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "pharmacy_acceptance.sqlite3")
        service = PharmacyService(database, FixedClock(datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)))
        base = service.domains
        base.register_organization(request_id="p-org", actor_id="bootstrap",
                                   organization_id="org-001", name="示范极地医疗")
        base.register_actor(request_id="p-admin", actor_id="bootstrap", new_actor_id="admin-001",
                            display_name="管理员", role="admin", organization_id="org-001")
        base.register_actor(request_id="p-doc", actor_id="admin-001", new_actor_id="doc-001",
                            display_name="越冬医生", role="physician", organization_id="org-001")
        base.register_actor(request_id="p-log", actor_id="admin-001", new_actor_id="log-001",
                            display_name="后勤主管", role="logistician", organization_id="org-001")
        base.register_actor(request_id="p-aud", actor_id="admin-001", new_actor_id="aud-001",
                            display_name="审计员", role="auditor", organization_id="org-001")
        base.register_site(request_id="p-site-a", actor_id="log-001", site_id="site-a",
                           organization_id="org-001", name="甲站", timezone_name="Asia/Shanghai")
        base.register_site(request_id="p-site-b", actor_id="log-001", site_id="site-b",
                           organization_id="org-001", name="乙站", timezone_name="Asia/Shanghai")

        service.configure_site_storage(request_id="p-storage-a", actor_id="log-001",
                                       site_id="site-a", storage_modes=["room"])
        service.configure_site_storage(request_id="p-storage-b", actor_id="log-001",
                                       site_id="site-b", storage_modes=["room"])
        service.register_medication(
            request_id="p-med-1", actor_id="doc-001", organization_id="org-001",
            medication_id="med-amox", generic_name="阿莫西林", dosage_form="注射剂",
            strength="0.5g", unit="瓶", storage_required="room",
            indications=["细菌感染"])
        service.register_alias(request_id="p-alias", actor_id="doc-001",
                               organization_id="org-001", medication_id="med-amox", alias="阿莫仙")
        service.set_prescriber_profile(request_id="p-prescriber", actor_id="admin-001",
                                       prescriber_actor_id="doc-001", controlled_level_max=1)
        service.configure_medication_policy(
            request_id="p-policy", actor_id="doc-001", site_id="site-a",
            medication_id="med-amox", emergency_reserve=8, reorder_point=2,
            next_resupply_date="2026-12-01")
        near = service.intake_batch(
            request_id="p-intake-near", actor_id="log-001", site_id="site-a",
            medication_ref="阿莫仙", batch_number="BN-2026-11", quantity=2,
            expiry_date="2026-11-15")
        far = service.intake_batch(
            request_id="p-intake-far", actor_id="log-001", site_id="site-a",
            medication_ref="med-amox", batch_number="BN-2027-09", quantity=12,
            expiry_date="2027-09-01")
        service.register_patient(request_id="p-patient", actor_id="doc-001", site_id="site-a",
                                 patient_id="pat-001", display_name="考察员甲", allergies=[])

        # 常规 4 瓶：总 12、应急锁定 8，恰好发完常规，FEFO 先用近效期批次。
        service.record_prescription(
            request_id="p-rx", actor_id="doc-001", site_id="site-a", patient_id="pat-001",
            medication_ref="阿莫仙", quantity=4, indication="细菌感染", prescription_id="rx-001")
        first = service.dispense_prescription(
            request_id="p-dispense", actor_id="doc-001", prescription_id="rx-001")
        replay = service.dispense_prescription(
            request_id="p-dispense", actor_id="doc-001", prescription_id="rx-001")

        # 再开 3 瓶：常规仅剩 2，必须动用 1 瓶应急储备；评估会要求批准，
        # 无批准的实际发放被阻止，有批准才放行。
        service.record_prescription(
            request_id="p-rx-em", actor_id="doc-001", site_id="site-a", patient_id="pat-001",
            medication_ref="med-amox", quantity=3, indication="细菌感染", prescription_id="rx-002")
        evaluated = service.evaluate_prescription("rx-002")
        needs_approval = "emergency_reserve" in evaluated["approvals_required"]
        no_approval_blocked = False
        try:
            service.dispense_prescription(
                request_id="p-dispense-em-blocked", actor_id="doc-001", prescription_id="rx-002")
        except PermissionDenied:
            no_approval_blocked = True

        # 跨站调拨需要批准；先调出剩余 2 瓶常规余量。
        service.grant_approval(request_id="p-transfer-ap", actor_id="log-001",
                               request_kind="cross_site_transfer", reason="乙站缺口",
                               approval_id="ap-002")
        transfer = service.transfer_batch(
            request_id="p-transfer", actor_id="log-001", batch_id=far["batch_id"],
            quantity=2, to_site_id="site-b", approval_id="ap-002", reason="乙站补给")
        received = service.receive_transfer(
            request_id="p-transfer-in", actor_id="log-001", transfer_id=transfer["transfer_id"])

        service.grant_approval(request_id="p-approval", actor_id="doc-001",
                               request_kind="emergency_reserve", reason="急救需要",
                               approval_id="ap-001")
        emergency = service.dispense_prescription(
            request_id="p-dispense-em", actor_id="doc-001",
            prescription_id="rx-002", approval_id="ap-001")

        # 后补诊疗信息只能追加。
        service.amend_prescription(request_id="p-amend", actor_id="doc-001",
                                   prescription_id="rx-001", note="补充观察记录",
                                   clinical_data={"observed_minutes": 30})

        # 另有一批临近效期仍在库，供后勤风险预警。
        service.intake_batch(
            request_id="p-intake-near-2", actor_id="log-001", site_id="site-a",
            medication_ref="med-amox", batch_number="BN-2026-12", quantity=1,
            expiry_date="2026-12-20")

        ledger = service.verify_ledger()
        risks = service.shortage_and_expiry_risks("site-a")
        destinations = service.batch_destinations(actor_id="aud-001",
                                                  batch_id=near["batch_id"])
        redacted = service.redacted_audit_events(actor_id="aud-001")
        audit_valid, audit_events = service.domains.verify_audit()
        database.close()

        plan = first["decision"]["plan"]
        result = {
            "status": "ok",
            "first_replayed": first["replayed"],
            "replay_replayed": replay["replayed"],
            "single_deduction": replay["dispensation_id"] == first["dispensation_id"],
            "fefo_first_batch": plan[0]["batch_number"],
            "fefo_first_quantity": plan[0]["quantity"],
            "emergency_requires_approval": needs_approval,
            "emergency_blocked_without_approval": no_approval_blocked,
            "emergency_allowed_with_approval": emergency["decision"]["allowed"],
            "emergency_quantity": emergency["decision"]["emergency_quantity"],
            "amendments": 1,
            "transfer_received": received["status"] == "received",
            "ledger_valid": ledger["valid"],
            "ledger_batches": ledger["batches_checked"],
            "near_expiry_alerts": len(risks["expiry_risks"]),
            "shortage_alerts": len(risks["shortage_risks"]),
            "audit_redacted": redacted["redacted"],
            "batch_destinations_traced": len(destinations["ledger"]) >= 2,
            "audit_valid": audit_valid,
            "audit_events": audit_events,
        }
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    required = ["status", "single_deduction", "emergency_blocked_without_approval",
                "emergency_allowed_with_approval", "transfer_received", "ledger_valid",
                "audit_redacted", "audit_valid"]
    ok = result["status"] == "ok" and all(result[key] for key in required)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
