import unittest

from polar_station_foundation.med_acceptance import run


class MedicationAcceptanceTest(unittest.TestCase):
    def test_medication_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["issue_once"])
        self.assertTrue(result["fefo_first_lot"])
        self.assertTrue(result["emergency_required_approval"])
        self.assertTrue(result["allergy_blocked"])
        self.assertTrue(result["late_amendment_warning"])
        self.assertTrue(result["issued_fact_preserved"])
        self.assertTrue(result["transfer_order_reserved"])
        self.assertTrue(result["reconcile_a"])
        self.assertTrue(result["reconcile_b"])
        self.assertTrue(result["auditor_patient_hidden"])


if __name__ == "__main__":
    unittest.main()
