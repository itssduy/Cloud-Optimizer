import unittest
from datetime import datetime, timezone
from decimal import Decimal

from audit import costs

NOW = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)
ACCOUNT = "123456789012"


def day(date, services, estimated=False):
    return {
        "TimePeriod": {"Start": date, "End": date},
        "Estimated": estimated,
        "Groups": [{"Keys": [name], "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": "USD"}}}
                   for name, amount in services.items()],
    }


class FakeCE:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get_cost_and_usage(self, **kwargs):
        self.calls.append(kwargs)
        return self.pages[len(self.calls) - 1]


def ec2_finding(instance_type, region="us-east-1", status="open"):
    return {"rule": "ec2_idle_cpu", "resource_type": "ec2_instance", "resource_id": "i-1",
            "region": region, "status": status, "evidence": {"instance_type": instance_type}}


class CostTests(unittest.TestCase):
    def test_daily_costs(self):
        ce = FakeCE([{"ResultsByTime": [
            day("2026-11-10", {"Amazon EC2": "3.104", "Amazon S3": "1.72"}),
            day("2026-11-11", {"Amazon EC2": "0"}, estimated=True),
        ]}])
        items = costs.fetch_daily_costs(ce, ACCOUNT, NOW)
        self.assertEqual(ce.calls[0]["TimePeriod"], {"Start": "2026-10-13", "End": "2026-11-12"})
        self.assertEqual(ce.calls[0]["Filter"]["Dimensions"]["Values"], [ACCOUNT])
        first, second = items
        self.assertEqual(first["sk"], "COST#2026-11-10")
        self.assertEqual(first["total"]["amount"], Decimal("4.82"))
        self.assertFalse(first["total"]["estimated"])
        # a real $0 day stays $0, and recent days are flagged as estimates
        self.assertEqual(second["total"]["amount"], Decimal("0.00"))
        self.assertTrue(second["total"]["estimated"])

    def test_missing_days_have_no_item(self):
        ce = FakeCE([{"ResultsByTime": [day("2026-11-10", {"Amazon EC2": "1"})]}])
        self.assertEqual([i["date"] for i in costs.fetch_daily_costs(ce, ACCOUNT, NOW)], ["2026-11-10"])

    def test_reads_every_page(self):
        ce = FakeCE([
            {"ResultsByTime": [day("2026-11-09", {"Amazon EC2": "1"})], "NextPageToken": "next"},
            {"ResultsByTime": [day("2026-11-10", {"Amazon EC2": "2"})]},
        ])
        self.assertEqual(len(costs.fetch_daily_costs(ce, ACCOUNT, NOW)), 2)
        self.assertEqual(ce.calls[1]["NextPageToken"], "next")

    def test_non_usd_is_rejected(self):
        page = day("2026-11-10", {"Amazon EC2": "1"})
        page["Groups"][0]["Metrics"]["UnblendedCost"]["Unit"] = "EUR"
        with self.assertRaises(ValueError):
            costs.fetch_daily_costs(FakeCE([{"ResultsByTime": [page]}]), ACCOUNT, NOW)

    def test_ec2_estimate(self):
        f = ec2_finding("t3.medium")
        summary = costs.calculate_savings(ACCOUNT, [f], NOW)
        self.assertEqual(f["estimated_monthly_savings"]["amount"], Decimal("30.37"))
        self.assertEqual(summary["total_monthly"]["amount"], Decimal("30.37"))
        self.assertEqual(summary["missing_estimate_count"], 0)

    def test_unknown_type_and_s3_have_no_estimate(self):
        odd = ec2_finding("x9.mega")
        s3 = {"rule": "s3_tiering_candidate", "resource_type": "s3_bucket", "resource_id": "b",
              "region": "us-east-1", "status": "open", "evidence": {}}
        summary = costs.calculate_savings(ACCOUNT, [odd, s3], NOW)
        self.assertIsNone(odd["estimated_monthly_savings"])
        self.assertIn("x9.mega", odd["savings_note"])
        self.assertIsNone(s3["estimated_monthly_savings"])
        self.assertEqual(summary["missing_estimate_count"], 2)
        self.assertEqual(summary["total_monthly"]["amount"], Decimal("0.00"))

    def test_other_region_has_no_estimate(self):
        f = ec2_finding("t3.medium", region="eu-west-1")
        costs.calculate_savings(ACCOUNT, [f], NOW)
        self.assertIsNone(f["estimated_monthly_savings"])

    def test_resolved_findings_are_not_counted(self):
        summary = costs.calculate_savings(ACCOUNT, [ec2_finding("t3.medium", status="resolved")], NOW)
        self.assertEqual(summary["finding_count"], 0)
        self.assertEqual(summary["total_monthly"]["amount"], Decimal("0.00"))


if __name__ == "__main__":
    unittest.main()
