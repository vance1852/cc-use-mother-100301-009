import unittest
from datetime import datetime, timezone

from polar_station_foundation.clock import FixedClock
from polar_station_foundation.errors import (
    ConflictError, NotFoundError, PermissionDenied, ValidationError)
from polar_station_foundation.pharmacy import PharmacyService
from polar_station_foundation.storage import Database


class PharmacyTestBase(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = PharmacyService(
            self.database, FixedClock(datetime(2026, 10, 6, tzinfo=timezone.utc)))
        base = self.service.domains
        base.register_organization(request_id="org", actor_id="bootstrap",
                                   organization_id="o1", name="极地医疗")
        base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="adm",
                            display_name="管理员", role="admin", organization_id="o1")
        base.register_actor(request_id="doc", actor_id="adm", new_actor_id="doc",
                            display_name="医生", role="physician", organization_id="o1")
        base.register_actor(request_id="log", actor_id="adm", new_actor_id="log",
                            display_name="后勤", role="logistician", organization_id="o1")
        base.register_actor(request_id="aud", actor_id="adm", new_actor_id="aud",
                            display_name="审计", role="auditor", organization_id="o1")
        base.register_site(request_id="site1", actor_id="log", site_id="s1",
                           organization_id="o1", name="甲站", timezone_name="Asia/Shanghai")
        base.register_site(request_id="site2", actor_id="log", site_id="s2",
                           organization_id="o1", name="乙站", timezone_name="Asia/Shanghai")
        self.service.configure_site_storage(request_id="store1", actor_id="log",
                                            site_id="s1", storage_modes=["room"])
        self.service.configure_site_storage(request_id="store2", actor_id="log",
                                            site_id="s2", storage_modes=["room"])
        self.service.register_medication(
            request_id="med1", actor_id="doc", organization_id="o1", medication_id="m1",
            generic_name="阿莫西林", dosage_form="注射剂", strength="0.5g", unit="瓶",
            storage_required="room", indications=["细菌感染"])
        self.service.register_medication(
            request_id="med2", actor_id="doc", organization_id="o1", medication_id="m2",
            generic_name="头孢唑林", dosage_form="注射剂", strength="0.5g", unit="瓶",
            storage_required="room", indications=["细菌感染"])
        self.service.register_alias(request_id="alias1", actor_id="doc", organization_id="o1",
                                    medication_id="m1", alias="阿莫仙")
        self.service.register_substitute(request_id="sub1", actor_id="doc", organization_id="o1",
                                         medication_id="m1", substitute_id="m2")
        self.service.set_prescriber_profile(request_id="pp", actor_id="adm",
                                            prescriber_actor_id="doc", controlled_level_max=1)
        self.service.register_patient(request_id="pat1", actor_id="doc", site_id="s1",
                                      patient_id="p1", display_name="患者甲", allergies=[])
        self.service.register_patient(request_id="pat2", actor_id="doc", site_id="s1",
                                      patient_id="p2", display_name="患者乙", allergies=["阿莫西林"])

    def tearDown(self):
        self.database.close()

    def intake(self, request_id, ref, batch_number, quantity, expiry, site="s1"):
        return self.service.intake_batch(
            request_id=request_id, actor_id="log", site_id=site, medication_ref=ref,
            batch_number=batch_number, quantity=quantity, expiry_date=expiry)

    def rx(self, request_id, rx_id, **kwargs):
        params = {"actor_id": "doc", "site_id": "s1", "patient_id": "p1",
                  "medication_ref": "m1", "quantity": 1, "indication": "细菌感染"}
        params.update(kwargs)
        self.service.record_prescription(request_id=request_id, prescription_id=rx_id, **params)


class IdentityAndIntakeTest(PharmacyTestBase):
    def test_trade_name_and_generic_name_merge_to_one_identity(self):
        a = self.intake("in1", "阿莫仙", "B1", 5, "2027-01-01")
        b = self.intake("in2", "m1", "B2", 7, "2027-08-01")
        rows = self.database.connection.execute(
            "SELECT medication_id, SUM(quantity_on_hand) AS q FROM medication_batches GROUP BY medication_id"
        ).fetchall()
        self.assertEqual(1, len(rows))
        self.assertEqual("m1", rows[0]["medication_id"])
        self.assertEqual(12, rows[0]["q"])
        self.assertTrue(a["batch_id"] and b["batch_id"])

    def test_intake_rejects_storage_capability_gap(self):
        self.service.register_medication(
            request_id="medf", actor_id="doc", organization_id="o1", medication_id="mf",
            generic_name="疫苗", dosage_form="注射剂", strength="1ml", unit="支",
            storage_required="frozen", indications=["预防"])
        with self.assertRaises(PermissionDenied):
            self.intake("inf", "mf", "F1", 2, "2027-01-01")

    def test_expired_batch_cannot_be_intaken(self):
        with self.assertRaises(ValidationError):
            self.intake("inold", "m1", "OLD", 2, "2026-09-01")


class DecisionTest(PharmacyTestBase):
    def test_fefo_prefers_near_expiry_batch(self):
        self.intake("in1", "m1", "NEAR", 6, "2026-11-01")
        self.intake("in2", "m1", "FAR", 12, "2027-06-01")
        self.rx("rx1", "rx1", quantity=8)
        decision = self.service.evaluate_prescription("rx1")
        self.assertTrue(decision["allowed"])
        self.assertEqual("NEAR", decision["plan"][0]["batch_number"])
        self.assertEqual(6, decision["plan"][0]["quantity"])
        self.assertEqual("FAR", decision["plan"][1]["batch_number"])
        self.assertEqual(2, decision["plan"][1]["quantity"])

    def test_allergy_blocks_but_substitute_bypasses(self):
        self.intake("in1", "m1", "B1", 5, "2027-01-01")
        self.intake("in2", "m2", "B2", 5, "2027-03-01")
        self.rx("rx1", "rx1", patient_id="p2", quantity=1, allow_substitute=True)
        decision = self.service.evaluate_prescription("rx1")
        self.assertTrue(decision["allowed"])
        self.assertTrue(decision["substituted"])
        self.assertEqual("m2", decision["dispense_medication_id"])

    def test_indication_out_of_scope_blocks(self):
        self.intake("in1", "m1", "B1", 5, "2027-01-01")
        self.rx("rx1", "rx1", indication="普通感冒")
        self.assertTrue(self.service.evaluate_prescription("rx1")["blocked"])

    def test_prescriber_controlled_level_enforced(self):
        self.service.register_medication(
            request_id="medc", actor_id="doc", organization_id="o1", medication_id="mc",
            generic_name="肾上腺素", dosage_form="注射剂", strength="1mg", unit="支",
            storage_required="room", indications=["过敏休克"], controlled_level=2)
        self.intake("inc", "mc", "C1", 2, "2027-01-01")
        self.rx("rxc", "rxc", medication_ref="mc", indication="过敏休克")
        blockers = self.service.evaluate_prescription("rxc")["blockers"]
        self.assertTrue(any("管制药权限" in b for b in blockers))

    def test_emergency_reserve_requires_approval(self):
        self.service.configure_medication_policy(
            request_id="pol", actor_id="doc", site_id="s1", medication_id="m1",
            emergency_reserve=10, reorder_point=2)
        self.intake("in1", "m1", "B1", 12, "2027-01-01")
        self.rx("rx1", "rx1", quantity=3)
        decision = self.service.evaluate_prescription("rx1")
        self.assertIn("emergency_reserve", decision["approvals_required"])
        with self.assertRaises(PermissionDenied):
            self.service.dispense_prescription(request_id="disp1", actor_id="doc",
                                               prescription_id="rx1")

    def test_near_expiry_substitute_requires_approval(self):
        self.service.configure_medication_policy(
            request_id="pol", actor_id="doc", site_id="s1", medication_id="m1",
            emergency_reserve=99)
        self.intake("in1", "m1", "B1", 5, "2027-01-01")
        self.intake("in2", "m2", "B2", 5, "2026-12-01")
        self.rx("rx1", "rx1", quantity=2, allow_substitute=True)
        decision = self.service.evaluate_prescription("rx1")
        self.assertTrue(decision["substituted"])
        self.assertIn("near_expiry_substitute", decision["approvals_required"])


class DispensationTest(PharmacyTestBase):
    def test_same_prescription_deducts_only_once(self):
        self.intake("in1", "m1", "B1", 5, "2027-01-01")
        self.rx("rx1", "rx1", quantity=2)
        first = self.service.dispense_prescription(
            request_id="disp1", actor_id="doc", prescription_id="rx1")
        second = self.service.dispense_prescription(
            request_id="disp1", actor_id="doc", prescription_id="rx1")
        self.assertTrue(second["replayed"])
        self.assertEqual(first["dispensation_id"], second["dispensation_id"])
        on_hand = self.database.connection.execute(
            "SELECT quantity_on_hand FROM medication_batches WHERE batch_id=?",
            (first["decision"]["plan"][0]["batch_id"],)).fetchone()[0]
        self.assertEqual(3, on_hand)
        count = self.database.connection.execute(
            "SELECT COUNT(*) FROM dispensations WHERE prescription_id='rx1'").fetchone()[0]
        self.assertEqual(1, count)

    def test_approval_consumed_once(self):
        self.service.configure_medication_policy(
            request_id="pol", actor_id="doc", site_id="s1", medication_id="m1",
            emergency_reserve=10)
        self.intake("in1", "m1", "B1", 5, "2027-01-01")
        self.service.grant_approval(request_id="ap", actor_id="doc",
                                    request_kind="emergency_reserve", reason="急救",
                                    approval_id="AP1")
        self.rx("rx1", "rx1", quantity=2)
        self.service.dispense_prescription(request_id="d1", actor_id="doc",
                                           prescription_id="rx1", approval_id="AP1")
        self.rx("rx2", "rx2", quantity=1)
        with self.assertRaises(PermissionDenied):
            self.service.dispense_prescription(request_id="d2", actor_id="doc",
                                               prescription_id="rx2", approval_id="AP1")

    def test_reserve_then_dispense_and_release(self):
        self.intake("in1", "m1", "B1", 5, "2027-01-01")
        self.rx("rx1", "rx1", quantity=2)
        self.service.reserve_prescription(request_id="rsv", actor_id="doc",
                                          prescription_id="rx1")
        with self.assertRaises(ConflictError):
            self.service.reserve_prescription(request_id="rsv2", actor_id="doc",
                                              prescription_id="rx1")
        held = self.database.connection.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM reservations WHERE status='held'").fetchone()[0]
        self.assertEqual(2, held)
        self.service.dispense_prescription(request_id="d1", actor_id="doc",
                                           prescription_id="rx1")
        held = self.database.connection.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM reservations WHERE status='held'").fetchone()[0]
        self.assertEqual(0, held)
        self.rx("rx2", "rx2", quantity=1)
        self.service.reserve_prescription(request_id="rsv3", actor_id="doc",
                                          prescription_id="rx2")
        self.service.release_reservation(request_id="rel", actor_id="doc",
                                         prescription_id="rx2", reason="取消")
        held = self.database.connection.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM reservations WHERE status='held'").fetchone()[0]
        self.assertEqual(0, held)


class LedgerAndMovementTest(PharmacyTestBase):
    def test_returns_loss_destroy_and_ledger_replay(self):
        batch = self.intake("in1", "m1", "B1", 5, "2027-01-01")
        self.rx("rx1", "rx1", quantity=2)
        disp = self.service.dispense_prescription(
            request_id="d1", actor_id="doc", prescription_id="rx1")
        self.service.return_dispensed(request_id="ret", actor_id="doc",
                                      dispensation_id=disp["dispensation_id"],
                                      batch_id=batch["batch_id"], quantity=1, reason="未用")
        self.service.record_loss(request_id="loss", actor_id="log",
                                 batch_id=batch["batch_id"], quantity=1, reason="破损")
        self.service.destroy_batch(request_id="des", actor_id="log",
                                   batch_id=batch["batch_id"], quantity=3, reason="不合格")
        result = self.service.verify_ledger()
        self.assertTrue(result["valid"])
        self.assertEqual(0, self.database.connection.execute(
            "SELECT quantity_on_hand FROM medication_batches WHERE batch_id=?",
            (batch["batch_id"],)).fetchone()[0])

    def test_cross_site_transfer_requires_approval_and_creates_inbound_batch(self):
        batch = self.intake("in1", "m1", "B1", 5, "2027-01-01")
        with self.assertRaises(NotFoundError):
            self.service.transfer_batch(request_id="t0", actor_id="log",
                                        batch_id=batch["batch_id"], quantity=1,
                                        to_site_id="s2", approval_id="NOPE", reason="x")
        self.service.grant_approval(request_id="tap", actor_id="log",
                                    request_kind="cross_site_transfer", reason="缺口",
                                    approval_id="TAP")
        transfer = self.service.transfer_batch(
            request_id="t1", actor_id="log", batch_id=batch["batch_id"], quantity=2,
            to_site_id="s2", approval_id="TAP", reason="乙站缺口")
        with self.assertRaises(ConflictError):
            self.service.transfer_batch(
                request_id="t2", actor_id="log", batch_id=batch["batch_id"], quantity=1,
                to_site_id="s2", approval_id="TAP", reason="再调")
        received = self.service.receive_transfer(
            request_id="r1", actor_id="log", transfer_id=transfer["transfer_id"])
        self.assertEqual("received", received["status"])
        inbound = self.database.connection.execute(
            "SELECT quantity_on_hand, site_id FROM medication_batches WHERE batch_id=?",
            (received["batch_id"],)).fetchone()
        self.assertEqual(2, inbound["quantity_on_hand"])
        self.assertEqual("s2", inbound["site_id"])

    def test_transfer_cannot_touch_emergency_reserve(self):
        self.service.configure_medication_policy(
            request_id="pol", actor_id="doc", site_id="s1", medication_id="m1",
            emergency_reserve=5)
        batch = self.intake("in1", "m1", "B1", 5, "2027-01-01")
        self.service.grant_approval(request_id="tap", actor_id="log",
                                    request_kind="cross_site_transfer", reason="缺口",
                                    approval_id="TAP")
        with self.assertRaises(PermissionDenied):
            self.service.transfer_batch(
                request_id="t1", actor_id="log", batch_id=batch["batch_id"], quantity=1,
                to_site_id="s2", approval_id="TAP", reason="x")


class AmendmentRiskAuditTest(PharmacyTestBase):
    def test_amendment_is_append_only_and_dispensation_unchanged(self):
        self.intake("in1", "m1", "B1", 5, "2027-01-01")
        self.rx("rx1", "rx1", quantity=2)
        before = self.service.dispense_prescription(
            request_id="d1", actor_id="doc", prescription_id="rx1")
        self.service.amend_prescription(request_id="a1", actor_id="doc",
                                        prescription_id="rx1", note="补充",
                                        clinical_data={"bp": "120/80"})
        self.service.amend_prescription(request_id="a2", actor_id="doc",
                                        prescription_id="rx1", note="再补充")
        self.assertEqual(2, len(self.service.list_amendments("rx1")))
        row = self.database.connection.execute(
            "SELECT quantity FROM dispensations WHERE dispensation_id=?",
            (before["dispensation_id"],)).fetchone()
        self.assertEqual(2, row["quantity"])

    def test_shortage_and_expiry_report(self):
        self.service.configure_medication_policy(
            request_id="pol", actor_id="doc", site_id="s1", medication_id="m1",
            emergency_reserve=10, reorder_point=4)
        self.intake("in1", "m1", "NEAR", 2, "2026-11-01")
        risks = self.service.shortage_and_expiry_risks("s1")
        self.assertTrue(any(i["medication_id"] == "m1" for i in risks["shortage_risks"]))
        self.assertEqual("near_expiry", risks["expiry_risks"][0]["risk"])

    def test_auditor_view_redacts_patient_but_keeps_trace(self):
        batch = self.intake("in1", "m1", "B1", 5, "2027-01-01")
        self.rx("rx1", "rx1", quantity=2)
        self.service.dispense_prescription(request_id="d1", actor_id="doc",
                                           prescription_id="rx1")
        doctor = self.service.batch_destinations(actor_id="doc", batch_id=batch["batch_id"])
        auditor = self.service.batch_destinations(actor_id="aud", batch_id=batch["batch_id"])
        self.assertEqual("患者甲", doctor["dispensations"][0]["patient"]["display_name"])
        self.assertNotIn("display_name", auditor["dispensations"][0]["patient"])
        self.assertTrue(auditor["dispensations"][0]["patient"]["patient_ref"].startswith("pat-"))
        self.assertGreaterEqual(len(auditor["ledger"]), 2)


if __name__ == "__main__":
    unittest.main()
