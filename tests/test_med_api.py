import json
import unittest

from polar_station_foundation.api import route
from polar_station_foundation.service import DomainService
from polar_station_foundation.storage import Database


class MedApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(self.database)
        # 基础引导
        route(self.service, "POST", "/organizations",
              {"request_id": "org", "organization_id": "o1", "name": "机构"},
              {"X-Actor-Id": "bootstrap"})
        for rid, aid, role in [("root", "root", "admin"), ("doc", "doc", "medical_officer"),
                               ("nurse", "nurse", "nurse"), ("pharm", "pharm", "pharmacist"),
                               ("log", "log", "logistics"), ("aud", "aud", "auditor")]:
            route(self.service, "POST", "/actors",
                  {"request_id": "ac-" + rid, "new_actor_id": aid, "display_name": aid,
                   "role": role, "organization_id": "o1"},
                  {"X-Actor-Id": "bootstrap" if aid == "root" else "root"})
        route(self.service, "POST", "/sites",
              {"request_id": "site", "site_id": "s1", "organization_id": "o1",
               "name": "站", "timezone_name": "UTC"}, {"X-Actor-Id": "root"})

    def tearDown(self):
        self.database.close()

    def call(self, method, path, body=None, actor="doc"):
        return route(self.service, method, path, body or {}, {"X-Actor-Id": actor})

    def test_full_dispense_flow_over_http(self):
        status, payload = self.call("POST", "/medications", {
            "request_id": "m1", "medication_id": "med-ad", "generic_name": "肾上腺素",
            "form": "注射剂", "strength": "1mg", "route": "iv", "controlled": True,
            "indications": ["过敏性休克"], "trade_names": ["副肾素"]}, actor="pharm")
        self.assertEqual(201, status)

        status, payload = self.call("POST", "/patients", {
            "request_id": "p1", "patient_id": "pat-1", "site_id": "s1",
            "display_name": "患者"}, actor="doc")
        self.assertEqual(201, status)

        status, payload = self.call("POST", "/med-batches", {
            "request_id": "b1", "site_id": "s1", "medication": "副肾素", "lot": "L1",
            "expiry_date": "2027-01-01", "quantity": 3}, actor="log")
        self.assertEqual(201, status)

        status, payload = self.call("POST", "/med-requests/evaluate", {
            "site_id": "s1", "patient_id": "pat-1", "medication": "med-ad",
            "quantity": 1, "indication": "过敏性休克"})
        self.assertEqual(200, status)
        self.assertTrue(payload["allowed"])

        status, order = self.call("POST", "/med-orders", {
            "request_id": "o1", "site_id": "s1", "medication": "med-ad", "quantity": 1,
            "patient_id": "pat-1", "indication": "过敏性休克"})
        self.assertEqual(201, status)
        self.assertEqual("reserved", order["response"]["status"])

        status, issued = self.call("POST", "/med-orders/issue", {
            "request_id": "i1", "order_id": order["resource_id"]}, actor="nurse")
        self.assertEqual(200, status)
        self.assertEqual(1, issued["response"]["issued"])

        status, fetched = self.call("GET", f"/med-orders/{order['resource_id']}")
        self.assertEqual(200, status)
        self.assertEqual("issued", fetched["status"])

    def test_policy_denied_returns_structured_reasons(self):
        self.call("POST", "/medications", {
            "request_id": "m1", "medication_id": "med-ad", "generic_name": "肾上腺素",
            "form": "注射剂", "strength": "1mg", "route": "iv", "controlled": True},
            actor="pharm")
        self.call("POST", "/patients", {
            "request_id": "p1", "patient_id": "pat-1", "site_id": "s1",
            "display_name": "患者"}, actor="doc")
        self.call("POST", "/patients/allergies", {
            "request_id": "a1", "patient_id": "pat-1", "medication": "med-ad",
            "severity": "severe"}, actor="doc")
        self.call("POST", "/med-batches", {
            "request_id": "b1", "site_id": "s1", "medication": "med-ad", "lot": "L1",
            "expiry_date": "2027-01-01", "quantity": 3}, actor="log")
        status, payload = self.call("POST", "/med-orders", {
            "request_id": "o1", "site_id": "s1", "medication": "med-ad", "quantity": 1,
            "patient_id": "pat-1", "is_emergency": True})
        self.assertEqual(422, status)
        self.assertEqual("policy_denied", payload["error"])
        self.assertTrue(any(r["code"] == "severe_allergy" for r in payload["reasons"]))

    def test_redacted_audit_hides_patient_for_auditor(self):
        self.call("POST", "/medications", {
            "request_id": "m1", "medication_id": "med-x", "generic_name": "X",
            "form": "inj", "strength": "1", "route": "iv"}, actor="pharm")
        self.call("POST", "/patients", {
            "request_id": "p1", "patient_id": "pat-secret", "site_id": "s1",
            "display_name": "私密患者"}, actor="doc")
        status, payload = self.call("GET", "/med-audit-events", actor="aud")
        self.assertEqual(200, status)
        raw = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("pat-secret", raw)
        self.assertNotIn("私密患者", raw)


if __name__ == "__main__":
    unittest.main()
