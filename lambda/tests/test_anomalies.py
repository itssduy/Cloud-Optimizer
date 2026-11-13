import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from audit import anomalies

NOW = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)
ACCOUNT = "123456789012"


def cost(day, services, estimated=False):
    by_service = {s: {"amount": Decimal(str(a)), "currency": "USD", "estimated": estimated}
                  for s, a in services.items()}
    total = sum((v["amount"] for v in by_service.values()), Decimal("0"))
    return {"date": day.isoformat(), "total": {"amount": total, "currency": "USD", "estimated": estimated},
            "by_service": by_service, "source": "cost_explorer", "fetched_at": "2026-11-12T06:00:00Z"}


def history(last_day, days, ec2=1.0, s3=1.0):
    return [cost(last_day - timedelta(days=i), {"EC2": ec2, "S3": s3}) for i in range(1, days + 1)]


class AnomalyTests(unittest.TestCase):
    def check(self, costs, **kwargs):
        return anomalies.check_spending(ACCOUNT, costs, NOW, **kwargs)

    def test_spike_is_an_alert(self):
        today = date(2026, 11, 11)
        r = self.check(history(today, 14) + [cost(today, {"EC2": 6.0, "S3": 1.0}, estimated=True)])
        self.assertEqual(r["outcome"], "alert")
        a = r["alert"]
        self.assertEqual(a["sk"], "ALERT#2026-11-11#total")
        self.assertEqual(a["actual"]["amount"], Decimal("7.00"))
        self.assertEqual(a["expected"]["amount"], Decimal("2.00"))
        self.assertTrue(a["actual"]["estimated"])
        self.assertEqual(a["top_services"][0]["service"], "EC2")
        self.assertIn("250% above", a["explanation"])
        self.assertIn("still an estimate", a["explanation"])

    def test_normal_day(self):
        today = date(2026, 11, 11)
        r = self.check(history(today, 14) + [cost(today, {"EC2": 1.2, "S3": 1.0})])
        self.assertEqual(r["outcome"], "normal")
        self.assertIsNone(r["alert"])

    def test_small_dollar_jump_is_ignored(self):
        today = date(2026, 11, 11)
        r = self.check(history(today, 14, ec2=0.1, s3=0.1) + [cost(today, {"EC2": 0.9, "S3": 0.1})])
        self.assertEqual(r["outcome"], "normal")

    def test_not_enough_history(self):
        today = date(2026, 11, 11)
        r = self.check(history(today, 3) + [cost(today, {"EC2": 50})])
        self.assertEqual(r["outcome"], "insufficient_history")
        self.assertIsNone(r["alert"])

    def test_stale_data(self):
        old = date(2026, 11, 1)
        r = self.check(history(old, 14) + [cost(old, {"EC2": 50})])
        self.assertEqual(r["outcome"], "stale")

    def test_no_data(self):
        self.assertEqual(self.check([])["outcome"], "no_data")

    def test_gaps_in_history_still_count_present_days(self):
        today = date(2026, 11, 11)
        partial = history(today, 14)[::2]
        r = self.check(partial + [cost(today, {"EC2": 9, "S3": 1})])
        self.assertEqual(r["outcome"], "alert")
        self.assertEqual(r["alert"]["baseline_days"], 7)

    def test_repeat_run_gives_same_key(self):
        today = date(2026, 11, 11)
        data = history(today, 14) + [cost(today, {"EC2": 6.0, "S3": 1.0})]
        self.assertEqual(self.check(data)["alert"]["sk"], self.check(data)["alert"]["sk"])


if __name__ == "__main__":
    unittest.main()
