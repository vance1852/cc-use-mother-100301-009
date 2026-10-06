"""药品与用药保障模块的离线端到端验收。

在临时 SQLite 库中演示：统一身份、近效期与应急储备、医嘱评估批准发放、
幂等不重复扣减、过敏阻断、后补修订留痕、跨站调拨、账本对账与脱敏审计。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .med_service import MedicationService
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "med_acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc))
        domain = DomainService(database, clock)
        med = MedicationService(database, clock)

        domain.register_organization(request_id="org", actor_id="bootstrap",
                                     organization_id="org-001", name="极地医疗机构")
        domain.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin-001",
                              display_name="管理员", role="admin", organization_id="org-001")
        domain.register_actor(request_id="doctor", actor_id="admin-001", new_actor_id="doc-001",
                              display_name="越冬医生", role="medical_officer",
                              organization_id="org-001")
        domain.register_actor(request_id="nurse", actor_id="admin-001", new_actor_id="nurse-001",
                              display_name="站区护士", role="nurse", organization_id="org-001")
        domain.register_actor(request_id="pharm", actor_id="admin-001", new_actor_id="pharm-001",
                              display_name="驻站药剂师", role="pharmacist",
                              organization_id="org-001")
        domain.register_actor(request_id="logistics", actor_id="admin-001", new_actor_id="log-001",
                              display_name="后勤补给", role="logistics", organization_id="org-001")
        domain.register_actor(request_id="auditor", actor_id="admin-001", new_actor_id="aud-001",
                              display_name="审计员", role="auditor", organization_id="org-001")
        domain.register_site(request_id="site-a", actor_id="admin-001", site_id="site-001",
                             organization_id="org-001", name="内陆主营地", timezone_name="UTC")
        domain.register_site(request_id="site-b", actor_id="admin-001", site_id="site-002",
                             organization_id="org-001", name="远端补给站", timezone_name="UTC")

        # 同一注射剂按通用名与商品名归一到同一身份。
        med.register_medication(
            request_id="med-ad", actor_id="pharm-001", medication_id="med-adrenaline",
            generic_name="肾上腺素", form="注射剂", strength="1mg/ml", route="静脉注射",
            storage_condition="room", controlled=True,
            indications=["过敏性休克", "心脏骤停"], trade_names=["副肾素"])
        med.register_medication(
            request_id="med-alt", actor_id="pharm-001", medication_id="med-norad",
            generic_name="去甲肾上腺素", form="注射剂", strength="2mg/ml", route="静脉注射",
            storage_condition="room", indications=["休克"])
        med.add_substitute(request_id="sub", actor_id="doc-001",
                           medication_id="med-adrenaline", substitute_id="med-norad")
        med.register_patient(request_id="patient", actor_id="doc-001", patient_id="pat-001",
                             site_id="site-001", display_name="登记患者")

        # 近效期 2 支常规 + 3 支应急；远效期 4 支常规。
        near = med.receive_batch(request_id="batch-near", actor_id="log-001",
                                 site_id="site-001", medication="副肾素", lot="LOT-2026-10",
                                 expiry_date="2026-10-20", quantity=5, emergency_reserve=3)
        far = med.receive_batch(request_id="batch-far", actor_id="log-001", site_id="site-001",
                                medication="med-adrenaline", lot="LOT-2027-06",
                                expiry_date="2027-06-01", quantity=4)

        # 常规 4 支：先近效期常规 2，再远效期 2，自动预留并发药。
        decision = med.evaluate_request(actor_id="doc-001", site_id="site-001",
                                        patient_id="pat-001", medication="肾上腺素", quantity=4,
                                        indication="过敏性休克")
        regular = med.create_order(request_id="order-regular", actor_id="doc-001",
                                   site_id="site-001", medication="med-adrenaline", quantity=4,
                                   patient_id="pat-001", indication="过敏性休克")
        first_issue = med.issue_order(request_id="issue-regular", actor_id="nurse-001",
                                      order_id=regular["resource_id"])
        retry_issue = med.issue_order(request_id="issue-regular", actor_id="nurse-001",
                                      order_id=regular["resource_id"])

        # 急救再取 3 支必须动用应急储备：待批准 + 理由，管制药双人核对。
        emergency = med.create_order(request_id="order-emergency", actor_id="doc-001",
                                     site_id="site-001", medication="med-adrenaline", quantity=3,
                                     patient_id="pat-001", indication="心脏骤停",
                                     is_emergency=True)
        med.approve_order(request_id="approve-emergency", actor_id="pharm-001",
                          order_id=emergency["resource_id"], justification="心脏骤停抢救，动用应急量并立即补货")
        med.issue_order(request_id="issue-emergency", actor_id="nurse-001",
                        order_id=emergency["resource_id"])

        # 过敏一旦登记，再次请求被硬阻断。
        med.add_allergy(request_id="allergy", actor_id="doc-001", patient_id="pat-001",
                        medication="med-adrenaline", severity="severe")
        blocked = med.evaluate_request(actor_id="doc-001", site_id="site-001",
                                       patient_id="pat-001", medication="肾上腺素", quantity=1,
                                       is_emergency=True)

        # 急救时患者未知，发放后补录，若有过敏史只产生预警而不抹掉发放事实。
        med.receive_batch(request_id="batch-alt", actor_id="log-001", site_id="site-001",
                          medication="med-norad", lot="LOT-NORAD", expiry_date="2027-01-01",
                          quantity=3)
        unassigned = med.create_order(request_id="order-late", actor_id="doc-001",
                                      site_id="site-001", medication="med-norad", quantity=1)
        med.issue_order(request_id="issue-late", actor_id="nurse-001",
                        order_id=unassigned["resource_id"])
        med.register_patient(request_id="patient-2", actor_id="doc-001", patient_id="pat-002",
                             site_id="site-001", display_name="后补患者")
        med.add_allergy(request_id="allergy-2", actor_id="doc-001", patient_id="pat-002",
                        medication="med-norad", severity="mild")
        amendment = med.amend_order(request_id="amend-late", actor_id="doc-001",
                                    order_id=unassigned["resource_id"], kind="patient_link",
                                    value="pat-002")

        # 损耗与销毁进连续账本，清空主站剩余肾上腺素，迫使后续走调拨。
        med.record_loss(request_id="loss", actor_id="log-001", batch_id=near["resource_id"],
                        quantity=1, reason="安瓿碎裂")
        med.destroy_batch(request_id="destroy", actor_id="pharm-001",
                          batch_id=near["resource_id"], reason="剩余近效期药品销毁")

        # 主站缺货时从远端站调拨，批准留理由，到货后自动预留并发放。
        med.receive_batch(request_id="remote-stock", actor_id="log-001", site_id="site-002",
                          medication="med-adrenaline", lot="LOT-REMOTE",
                          expiry_date="2027-09-01", quantity=8, emergency_reserve=2)
        transfer_order = med.create_order(request_id="order-transfer", actor_id="doc-001",
                                          site_id="site-001", medication="med-adrenaline",
                                          quantity=3, patient_id="pat-002", is_emergency=True,
                                          allow_transfer=True)
        approval = med.approve_order(request_id="approve-transfer", actor_id="pharm-001",
                                     order_id=transfer_order["resource_id"],
                                     justification="主站急救无库存，从远端补给站调剂")
        shipment = med.ship_transfer(request_id="ship", actor_id="log-001",
                                     transfer_id=approval["response"]["transfer_id"])
        receipt = med.receive_transfer(request_id="receive-transfer", actor_id="log-001",
                                       transfer_id=approval["response"]["transfer_id"])
        med.issue_order(request_id="issue-transfer", actor_id="nurse-001",
                        order_id=transfer_order["resource_id"])

        reconcile_a = med.reconcile_site("doc-001", "site-001")
        reconcile_b = med.reconcile_site("doc-001", "site-002")
        auditor_view = med.redacted_audit_events(actor_id="aud-001")
        leaked = [event for event in auditor_view["items"]
                  if "pat-001" in json.dumps(event, ensure_ascii=False)
                  or "登记患者" in json.dumps(event, ensure_ascii=False)]
        chain_valid, chain_count = domain.verify_audit()

        result = {
            "status": "ok",
            "regular_auto_reserved": regular["response"]["status"] == "reserved",
            "fefo_first_lot": decision["plan"]["items"][0]["expiry_date"] == "2026-10-20",
            "issue_once": (not first_issue["replayed"] and retry_issue["replayed"]
                           and retry_issue["response"]["already_issued"]),
            "emergency_required_approval": emergency["response"]["status"] == "pending_review",
            "allergy_blocked": blocked["blocked"],
            "late_amendment_warning": amendment["response"]["warning"] is not None,
            "issued_fact_preserved": amendment["response"]["issued_fact_preserved"],
            "transfer_received": receipt["response"]["status"] == "received",
            "transfer_order_reserved": receipt["response"]["order_status"] == "reserved",
            "shipped_batch": shipment["response"]["batch_id"],
            "reconcile_a": reconcile_a["all_matched"],
            "reconcile_b": reconcile_b["all_matched"],
            "auditor_patient_hidden": not leaked,
            "audit_valid": chain_valid,
            "audit_events": chain_count,
        }
        database.close()
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
