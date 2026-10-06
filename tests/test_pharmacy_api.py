import unittest

from polar_station_foundation.api import route
from polar_station_foundation.pharmacy import PharmacyService
from polar_station_foundation.storage import Database


class PharmacyApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = PharmacyService(self.database)
        base = self.service.domains
        base.register_organization(request_id="org", actor_id="bootstrap",
                                   organization_id="o1", name="机构")
        base.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="adm",
                            display_name="管理员", role="admin", organization_id="o1")
        base.register_actor(request_id="doc", actor_id="adm", new_actor_id="doc",
                            display_name="医生", role="physician", organization_id="o1")
        base.register_actor(request_id="log", actor_id="adm", new_actor_id="log",
                            display_name="后勤", role="logistician", organization_id="o1")
        base.register_site(request_id="sit", actor_id="log", site_id="s1",
                           organization_id="o1", name="站", timezone_name="x")

    def tearDown(self):
        self.database.close()

    def _json(self, method, path, body=None, actor="log"):
        return route(self.service, method, path, body or {}, {"X-Actor-Id": actor})

    def test_medication_and_batch_flow_over_http(self):
        status, payload = self._json("POST", "/site-storage", {
            "request_id": "sto", "site_id": "s1", "storage_modes": ["room"]})
        self.assertEqual(201, status)
        status, payload = self._json("POST", "/medications", {
            "request_id": "med", "organization_id": "o1", "medication_id": "m1",
            "generic_name": "阿莫西林", "dosage_form": "注射剂", "strength": "0.5g",
            "unit": "瓶", "storage_required": "room", "indications": ["细菌感染"]}, actor="doc")
        self.assertEqual(201, status)
        status, payload = self._json("POST", "/batches", {
            "request_id": "bat", "site_id": "s1", "medication_ref": "m1",
            "batch_number": "B1", "quantity": 5, "expiry_date": "2027-01-01"})
        self.assertEqual(201, status)
        self.assertTrue(payload["batch_id"])
        status, payload = self._json("GET", "/site-inventory?site_id=s1", None)
        self.assertEqual(200, status)
        self.assertEqual(5, payload["items"][0]["on_hand"])

    def test_idempotent_replay_returns_200(self):
        self._json("POST", "/site-storage", {"request_id": "sto", "site_id": "s1",
                                             "storage_modes": ["room"]})
        status, _ = self._json("POST", "/site-storage", {"request_id": "sto", "site_id": "s1",
                                                         "storage_modes": ["room"]})
        self.assertEqual(200, status)

    def test_missing_actor_is_rejected_for_protected_route(self):
        status, payload = route(self.service, "POST", "/batches", {
            "request_id": "bat", "site_id": "s1", "medication_ref": "m1",
            "batch_number": "B1", "quantity": 5, "expiry_date": "2027-01-01"})
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
