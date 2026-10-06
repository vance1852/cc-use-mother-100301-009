import json
import unittest
from datetime import datetime, timezone

from polar_station_foundation.clock import FixedClock
from polar_station_foundation.errors import (
    ConflictError, NotFoundError, PermissionDenied, PolicyDenied, ValidationError,
)
from polar_station_foundation.med_service import MedicationService
from polar_station_foundation.service import DomainService
from polar_station_foundation.storage import Database


class MedicationTestBase(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
        self.clock = clock
        self.domain = DomainService(self.database, clock)
        self.med = MedicationService(self.database, clock)
        self.domain.register_organization(request_id="org", actor_id="bootstrap",
                                          organization_id="o1", name="极地医疗机构")
        self.domain.register_actor(request_id="root", actor_id="bootstrap", new_actor_id="root",
                                   display_name="管理员", role="admin", organization_id="o1")
        for rid, aid, name, role in [
            ("adoc", "doc", "越冬医生", "medical_officer"),
            ("anurse", "nurse", "护士", "nurse"),
            ("apharm", "pharm", "药剂师", "pharmacist"),
            ("alog", "log", "后勤", "logistics"),
            ("aaud", "aud", "审计员", "auditor"),
        ]:
            self.domain.register_actor(request_id=rid, actor_id="root", new_actor_id=aid,
                                       display_name=name, role=role, organization_id="o1")
        self.domain.register_site(request_id="sitea", actor_id="root", site_id="site-a",
                                  organization_id="o1", name="甲站", timezone_name="UTC")
        self.domain.register_site(request_id="siteb", actor_id="root", site_id="site-b",
                                  organization_id="o1", name="乙站", timezone_name="UTC")

    def tearDown(self):
        self.database.close()

    def catalog(self, medication_id="med-ad", **overrides):
        params = dict(request_id="cat-" + medication_id, actor_id="pharm",
                      medication_id=medication_id, generic_name="肾上腺素", form="注射剂",
                      strength="1mg/ml", route="iv", storage_condition="room",
                      controlled=True, indications=["过敏性休克", "心脏骤停"],
                      trade_names=["副肾素"])
        params.update(overrides)
        return self.med.register_medication(**params)["resource_id"]

    def patient(self, patient_id="pat-1", site_id="site-a"):
        return self.med.register_patient(request_id="pat-" + patient_id, actor_id="doc",
                                         patient_id=patient_id, site_id=site_id,
                                         display_name="患者")["resource_id"]

    def receive(self, *, request_id, site_id="site-a", medication="med-ad", lot,
                expiry_date, quantity, emergency_reserve=0, actor_id="log"):
        return self.med.receive_batch(request_id=request_id, actor_id=actor_id,
                                      site_id=site_id, medication=medication, lot=lot,
                                      expiry_date=expiry_date, quantity=quantity,
                                      emergency_reserve=emergency_reserve)["resource_id"]


class CatalogIdentityTest(MedicationTestBase):
    def test_trade_and_generic_name_resolve_to_same_identity(self):
        self.catalog()
        by_generic = self.med.medication("doc", "肾上腺素")
        by_trade = self.med.medication("doc", "副肾素")
        by_id = self.med.medication("doc", "med-ad")
        self.assertEqual({by_generic["medication_id"], by_trade["medication_id"]},
                         {"med-ad"})
        self.assertEqual(by_id["medication_id"], "med-ad")

    def test_duplicate_name_is_rejected(self):
        self.catalog()
        with self.assertRaises(ConflictError):
            self.catalog("med-other", generic_name="去甲肾上腺素", trade_names=["副肾素"])

    def test_substitute_is_symmetric(self):
        self.catalog("med-ad")
        self.catalog("med-alt", generic_name="去甲肾上腺素", controlled=False, trade_names=[])
        self.med.add_substitute(request_id="sub", actor_id="doc",
                                medication_id="med-ad", substitute_id="med-alt")
        self.assertIn("med-alt", self.med.medication("doc", "med-ad")["substitutes"])
        self.assertIn("med-ad", self.med.medication("doc", "med-alt")["substitutes"])

    def test_nurse_cannot_manage_catalog(self):
        with self.assertRaises(PermissionDenied):
            self.med.register_medication(request_id="x", actor_id="nurse",
                                         medication_id="m", generic_name="g", form="f",
                                         strength="s", route="iv")


class BatchAndFefoTest(MedicationTestBase):
    def test_fefo_prefers_near_expiry_but_protects_emergency_reserve(self):
        self.catalog()
        self.receive(request_id="b-near", lot="NEAR", expiry_date="2026-10-20",
                     quantity=5, emergency_reserve=3)
        self.receive(request_id="b-far", lot="FAR", expiry_date="2027-06-01", quantity=4)
        decision = self.med.evaluate_request(actor_id="doc", site_id="site-a",
                                             patient_id=self.patient(), medication="med-ad",
                                             quantity=4, indication="过敏性休克")
        lots = []
        for row in self.database.connection.execute(
                "SELECT batch_id,lot FROM med_batches"):
            lots.append((row["batch_id"], row["lot"]))
        lot_by_batch = dict(lots)
        chosen = [(lot_by_batch[i["batch_id"]], i["quantity"], i["emergency"])
                  for i in decision["plan"]["items"]]
        self.assertEqual(chosen, [("NEAR", 2, False), ("FAR", 2, False)])

    def test_expired_batch_is_quarantined_and_excluded(self):
        self.catalog(controlled=False)
        batch_id = self.receive(request_id="b-old", lot="OLD",
                                expiry_date="2026-01-01", quantity=3)
        status = self.database.connection.execute(
            "SELECT status FROM med_batches WHERE batch_id=?", (batch_id,)).fetchone()["status"]
        self.assertEqual(status, "quarantined")
        decision = self.med.evaluate_request(actor_id="doc", site_id="site-a",
                                             patient_id=self.patient(), medication="med-ad",
                                             quantity=1)
        self.assertTrue(decision["blocked"])


class OrderLifecycleTest(MedicationTestBase):
    def _stocked(self):
        self.catalog()
        self.receive(request_id="b-near", lot="NEAR", expiry_date="2026-10-20",
                     quantity=5, emergency_reserve=3)
        self.receive(request_id="b-far", lot="FAR", expiry_date="2027-06-01", quantity=4)

    def test_regular_order_auto_reserves_and_issues_once(self):
        self._stocked()
        order = self.med.create_order(request_id="o1", actor_id="doc", site_id="site-a",
                                      medication="med-ad", quantity=4,
                                      patient_id=self.patient(), indication="过敏性休克")
        self.assertEqual(order["response"]["status"], "reserved")
        first = self.med.issue_order(request_id="i1", actor_id="nurse",
                                     order_id=order["resource_id"])
        self.assertFalse(first["replayed"])
        # 同一 request_id 重试
        replay = self.med.issue_order(request_id="i1", actor_id="nurse",
                                      order_id=order["resource_id"])
        self.assertTrue(replay["replayed"])
        # 换 request_id 重试也不能再次扣减
        again = self.med.issue_order(request_id="i2", actor_id="nurse",
                                     order_id=order["resource_id"])
        self.assertTrue(again["response"]["already_issued"])
        issued = self.database.connection.execute(
            "SELECT COALESCE(SUM(quantity),0) AS q FROM med_ledger_entries "
            "WHERE movement_type='issue'").fetchone()["q"]
        self.assertEqual(issued, 4)

    def test_emergency_reserve_requires_approval_with_reason(self):
        self._stocked()
        pid = self.patient()
        # 先耗尽 6 支常规
        order = self.med.create_order(request_id="o1", actor_id="doc", site_id="site-a",
                                      medication="med-ad", quantity=6, patient_id=pid)
        self.med.issue_order(request_id="i1", actor_id="nurse", order_id=order["resource_id"])
        emergency = self.med.create_order(request_id="o2", actor_id="doc", site_id="site-a",
                                          medication="med-ad", quantity=2, patient_id=pid,
                                          is_emergency=True)
        self.assertEqual(emergency["response"]["status"], "pending_review")
        with self.assertRaises(ValidationError):
            self.med.approve_order(request_id="a1", actor_id="pharm",
                                   order_id=emergency["resource_id"], justification="  ")
        # 管制药开单人不能自批
        with self.assertRaises(PermissionDenied):
            self.med.approve_order(request_id="a2", actor_id="doc",
                                   order_id=emergency["resource_id"], justification="抢救")
        self.med.approve_order(request_id="a3", actor_id="pharm",
                               order_id=emergency["resource_id"], justification="抢救动用应急量")
        self.med.issue_order(request_id="i2", actor_id="nurse",
                            order_id=emergency["resource_id"])

    def test_severe_allergy_blocks_request(self):
        self._stocked()
        pid = self.patient()
        self.med.add_allergy(request_id="al", actor_id="doc", patient_id=pid,
                             medication="副肾素", severity="severe")
        decision = self.med.evaluate_request(actor_id="doc", site_id="site-a",
                                             patient_id=pid, medication="med-ad", quantity=1,
                                             is_emergency=True)
        self.assertTrue(decision["blocked"])
        with self.assertRaises(PolicyDenied):
            self.med.create_order(request_id="o", actor_id="doc", site_id="site-a",
                                  medication="med-ad", quantity=1, patient_id=pid,
                                  is_emergency=True)

    def test_nurse_cannot_prescribe_prescription_drug(self):
        self._stocked()
        # 护士可进入流程，但临床评估以处方权限规则阻断并给出原因。
        with self.assertRaises(PolicyDenied) as context:
            self.med.create_order(request_id="o", actor_id="nurse", site_id="site-a",
                                  medication="med-ad", quantity=1, patient_id=self.patient())
        self.assertTrue(any(r["code"] in {"prescription_privilege", "controlled_privilege"}
                            for r in context.exception.reasons))

    def test_indication_outside_catalog_warns_but_allows(self):
        self._stocked()
        decision = self.med.evaluate_request(actor_id="doc", site_id="site-a",
                                             patient_id=self.patient(), medication="med-ad",
                                             quantity=1, indication="偏头疼")
        self.assertTrue(any(r["code"] == "indication_unlisted" for r in decision["reasons"]))
        self.assertTrue(decision["allowed"])


class SubstituteAndTransferTest(MedicationTestBase):
    def test_substitute_requires_approval_and_marks_line(self):
        self.catalog("med-ad", controlled=False)
        self.catalog("med-alt", generic_name="去甲肾上腺素", controlled=False, trade_names=[])
        self.med.add_substitute(request_id="sub", actor_id="doc",
                                medication_id="med-ad", substitute_id="med-alt")
        self.receive(request_id="b-alt", medication="med-alt", lot="ALT",
                     expiry_date="2026-10-25", quantity=5)
        pid = self.patient()
        order = self.med.create_order(request_id="o", actor_id="doc", site_id="site-a",
                                      medication="med-ad", quantity=2, patient_id=pid,
                                      allow_substitute=True)
        self.assertEqual(order["response"]["status"], "pending_review")
        self.med.approve_order(request_id="a", actor_id="pharm",
                               order_id=order["resource_id"], justification="原药缺货用替代药")
        self.med.issue_order(request_id="i", actor_id="nurse", order_id=order["resource_id"])
        detail = self.med.get_order(actor_id="doc", order_id=order["resource_id"])
        self.assertTrue(detail["lines"][0]["is_substitute"])
        self.assertEqual(detail["lines"][0]["medication_id"], "med-alt")

    def test_cross_site_transfer_full_flow(self):
        self.catalog()
        pid = self.patient()
        self.receive(request_id="remote", site_id="site-b", lot="REMOTE",
                     expiry_date="2027-09-01", quantity=8, emergency_reserve=2)
        order = self.med.create_order(request_id="o", actor_id="doc", site_id="site-a",
                                      medication="med-ad", quantity=3, patient_id=pid,
                                      is_emergency=True, allow_transfer=True)
        self.assertEqual(order["response"]["status"], "pending_review")
        approval = self.med.approve_order(request_id="a", actor_id="pharm",
                                          order_id=order["resource_id"],
                                          justification="本站缺货，跨站调拨")
        transfer_id = approval["response"]["transfer_id"]
        shipment = self.med.ship_transfer(request_id="ship", actor_id="log",
                                          transfer_id=transfer_id)
        receipt = self.med.receive_transfer(request_id="recv", actor_id="log",
                                            transfer_id=transfer_id)
        self.assertEqual(receipt["response"]["order_status"], "reserved")
        self.med.issue_order(request_id="issue", actor_id="nurse",
                            order_id=order["resource_id"])
        # 调出站账本余额
        source = self.database.connection.execute(
            "SELECT quantity_on_hand FROM med_batches WHERE batch_id=?",
            (shipment["response"]["batch_id"],)).fetchone()
        self.assertEqual(source["quantity_on_hand"], 5)
        # 新批次标记为调拨来源
        self.assertEqual(
            self.database.connection.execute(
                "SELECT source_transfer_id FROM med_batches WHERE batch_id=?",
                (receipt["response"]["batch_id"],)).fetchone()["source_transfer_id"],
            transfer_id)

    def test_transfer_without_flag_is_not_considered(self):
        self.catalog()
        self.receive(request_id="remote", site_id="site-b", lot="REMOTE",
                     expiry_date="2027-09-01", quantity=8)
        decision = self.med.evaluate_request(actor_id="doc", site_id="site-a",
                                             patient_id=self.patient(), medication="med-ad",
                                             quantity=2, is_emergency=True)
        self.assertTrue(decision["blocked"])
        self.assertNotIn("transfer", decision["approval_types"])


class AmendmentTest(MedicationTestBase):
    def test_late_patient_link_appends_warning_and_keeps_issue_fact(self):
        self.catalog("med-alt", generic_name="去甲肾上腺素", controlled=False, trade_names=[])
        self.receive(request_id="b", medication="med-alt", lot="L",
                     expiry_date="2027-01-01", quantity=3)
        order = self.med.create_order(request_id="o", actor_id="doc", site_id="site-a",
                                      medication="med-alt", quantity=1)
        self.med.issue_order(request_id="i", actor_id="nurse", order_id=order["resource_id"])
        self.patient("pat-9")
        self.med.add_allergy(request_id="al", actor_id="doc", patient_id="pat-9",
                             medication="med-alt", severity="mild")
        result = self.med.amend_order(request_id="am", actor_id="doc",
                                      order_id=order["resource_id"], kind="patient_link",
                                      value="pat-9")
        self.assertIsNotNone(result["response"]["warning"])
        self.assertTrue(result["response"]["issued_fact_preserved"])
        detail = self.med.get_order(actor_id="doc", order_id=order["resource_id"])
        self.assertEqual(detail["status"], "issued")
        self.assertEqual(len(detail["lines"]), 1)
        self.assertEqual(len(detail["amendments"]), 1)

    def test_cannot_relink_order_to_different_patient(self):
        self.catalog("med-alt", generic_name="去甲肾上腺素", controlled=False, trade_names=[])
        self.receive(request_id="b", medication="med-alt", lot="L",
                     expiry_date="2027-01-01", quantity=3)
        p1 = self.patient("pat-1")
        self.patient("pat-2")
        order = self.med.create_order(request_id="o", actor_id="doc", site_id="site-a",
                                      medication="med-alt", quantity=1, patient_id=p1)
        with self.assertRaises(ConflictError):
            self.med.amend_order(request_id="am", actor_id="doc",
                                 order_id=order["resource_id"], kind="patient_link",
                                 value="pat-2")


class LedgerTest(MedicationTestBase):
    def test_loss_and_destroy_require_reason_and_reconcile(self):
        self.catalog()
        batch_id = self.receive(request_id="b", lot="L", expiry_date="2027-01-01", quantity=5)
        with self.assertRaises(ValidationError):
            self.med.record_loss(request_id="lo", actor_id="log", batch_id=batch_id,
                                 quantity=1, reason=" ")
        self.med.record_loss(request_id="lo", actor_id="log", batch_id=batch_id,
                             quantity=1, reason="破碎")
        self.med.destroy_batch(request_id="de", actor_id="pharm", batch_id=batch_id,
                               quantity=2, reason="变质")
        reconciliation = self.med.reconcile_batch("doc", batch_id)
        self.assertTrue(reconciliation["matched"])
        self.assertEqual(reconciliation["batch_on_hand"], 2)

    def test_cannot_destroy_reserved_stock(self):
        self.catalog(controlled=False)
        batch_id = self.receive(request_id="b", lot="L", expiry_date="2027-01-01", quantity=3)
        order = self.med.create_order(request_id="o", actor_id="doc", site_id="site-a",
                                      medication="med-ad", quantity=3, patient_id=self.patient())
        self.assertEqual(order["response"]["status"], "reserved")
        with self.assertRaises(PolicyDenied):
            self.med.destroy_batch(request_id="de", actor_id="pharm", batch_id=batch_id,
                                   reason="尝试销毁预留药")

    def test_cancel_order_releases_reservation(self):
        self.catalog(controlled=False)
        self.receive(request_id="b", lot="L", expiry_date="2027-01-01", quantity=3)
        order = self.med.create_order(request_id="o", actor_id="doc", site_id="site-a",
                                      medication="med-ad", quantity=2, patient_id=self.patient())
        self.med.cancel_order(request_id="c", actor_id="doc", order_id=order["resource_id"],
                              reason="患者拒绝")
        reserved = self.database.connection.execute(
            "SELECT COALESCE(SUM(reserved),0) AS r FROM med_batches").fetchone()["r"]
        self.assertEqual(reserved, 0)


class ReportAndAuditTest(MedicationTestBase):
    def test_expiry_shortage_report(self):
        self.catalog()
        # 2 支全部划为应急储备：常规可用为 0，既近效期又短缺。
        self.receive(request_id="b-near", lot="NEAR", expiry_date="2026-10-18",
                     quantity=2, emergency_reserve=2)
        self.receive(request_id="b-old", lot="OLD", expiry_date="2026-09-01", quantity=1)
        report = self.med.expiry_and_shortage_report("log", "site-a")
        self.assertEqual([e["lot"] for e in report["expired"]], ["OLD"])
        self.assertEqual([e["lot"] for e in report["expiring"]], ["NEAR"])
        self.assertTrue(any(s["medication_id"] == "med-ad" for s in report["shortages"]))

    def test_auditor_sees_redacted_patients(self):
        self.catalog(controlled=False)
        pid = self.patient()
        self.receive(request_id="b", lot="L", expiry_date="2027-01-01", quantity=3)
        order = self.med.create_order(request_id="o", actor_id="doc", site_id="site-a",
                                      medication="med-ad", quantity=1, patient_id=pid)
        self.med.issue_order(request_id="i", actor_id="doc", order_id=order["resource_id"])
        view = self.med.redacted_audit_events(actor_id="aud")
        raw = json.dumps(view, ensure_ascii=False)
        self.assertNotIn(pid, raw)
        self.assertNotIn("患者", raw)
        # 医生仍可看到真实编号
        doctor_view = self.med.redacted_audit_events(actor_id="doc")
        self.assertIn(pid, json.dumps(doctor_view, ensure_ascii=False))

    def test_batch_trace_accounts_for_every_unit(self):
        self.catalog(controlled=False)
        pid = self.patient()
        batch_id = self.receive(request_id="b", lot="L", expiry_date="2027-01-01", quantity=5)
        order = self.med.create_order(request_id="o", actor_id="doc", site_id="site-a",
                                      medication="med-ad", quantity=2, patient_id=pid)
        self.med.issue_order(request_id="i", actor_id="doc", order_id=order["resource_id"])
        trace = self.med.batch_trace(actor_id="aud", batch_id=batch_id)
        self.assertEqual(trace["totals"]["issued"], 2)
        self.assertEqual(trace["on_hand"], 3)
        # 审计视角下患者被脱敏
        self.assertTrue(trace["destinations"][0]["patient_redacted"])


class CrossOrganizationAccessTest(MedicationTestBase):
    def _second_org(self):
        self.domain.register_organization(request_id="org2", actor_id="root",
                                          organization_id="o2", name="其他机构")
        self.domain.register_actor(request_id="doc2", actor_id="root", new_actor_id="doc2",
                                   display_name="他站医生", role="medical_officer",
                                   organization_id="o2")
        self.domain.register_actor(request_id="ph2", actor_id="root", new_actor_id="ph2",
                                   display_name="他站药剂师", role="pharmacist",
                                   organization_id="o2")
        self.domain.register_actor(request_id="log2", actor_id="root", new_actor_id="log2",
                                   display_name="他站后勤", role="logistics",
                                   organization_id="o2")
        self.domain.register_site(request_id="sitec", actor_id="root", site_id="site-c",
                                  organization_id="o2", name="丙站", timezone_name="UTC")

    def test_other_organization_cannot_touch_batch_and_order(self):
        self._second_org()
        self.catalog(controlled=False)
        batch_id = self.receive(request_id="b", lot="L", expiry_date="2027-01-01", quantity=3)
        pid = self.patient()
        order = self.med.create_order(request_id="o", actor_id="doc", site_id="site-a",
                                      medication="med-ad", quantity=1, patient_id=pid)
        # 同角色的他组织操作者即便知道 ID 也不能触及本站批次/医嘱
        with self.assertRaises(PermissionDenied):
            self.med.issue_order(request_id="ix", actor_id="doc2", order_id=order["resource_id"])
        with self.assertRaises(PermissionDenied):
            self.med.amend_order(request_id="ax", actor_id="doc2",
                                 order_id=order["resource_id"], kind="note", value="x")
        with self.assertRaises(PermissionDenied):
            self.med.record_loss(request_id="lx", actor_id="log2", batch_id=batch_id,
                                 quantity=1, reason="越权")
        with self.assertRaises(PermissionDenied):
            self.med.destroy_batch(request_id="dx", actor_id="ph2", batch_id=batch_id,
                                   reason="越权销毁")
        with self.assertRaises(PermissionDenied):
            self.med.batch_trace(actor_id="ph2", batch_id=batch_id)


if __name__ == "__main__":
    unittest.main()
