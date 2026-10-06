import unittest

from polar_station_foundation.pharmacy_acceptance import run


class PharmacyAcceptanceTest(unittest.TestCase):
    def test_offline_pharmacy_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["audit_redacted"])
        self.assertTrue(result["single_deduction"])
        self.assertTrue(result["emergency_requires_approval"])
        self.assertTrue(result["emergency_blocked_without_approval"])
        self.assertTrue(result["emergency_allowed_with_approval"])
        self.assertTrue(result["transfer_received"])
        self.assertTrue(result["ledger_valid"])
        self.assertEqual("BN-2026-11", result["fefo_first_batch"])
        self.assertGreaterEqual(result["near_expiry_alerts"], 1)


if __name__ == "__main__":
    unittest.main()
