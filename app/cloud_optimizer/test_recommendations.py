import json
from pathlib import Path

from django.test import SimpleTestCase

from .recommendations import advice

EXAMPLES = Path(__file__).parent / "examples"


def example(name):
    return json.loads((EXAMPLES / f"{name}.json").read_text())


class RecommendationTests(SimpleTestCase):
    def test_ec2_idle(self):
        a = advice(example("finding_ec2_idle"))
        self.assertIn("1.2%", a["observed"])
        self.assertIn("168 of 168", a["observed"])
        self.assertEqual(a["action"], "ec2_stop")
        self.assertEqual(a["benefit"]["monthly"]["amount"], 30.37)
        self.assertTrue(any("EBS" in x for x in a["limitations"]))

    def test_s3_uploads(self):
        a = advice(example("finding_s3_incomplete_uploads"))
        self.assertIn("3 incomplete uploads", a["observed"])
        self.assertEqual(a["action"], "s3_apply_lifecycle")
        self.assertIsNone(a["benefit"]["monthly"])

    def test_s3_tiering_has_charges_and_no_made_up_savings(self):
        a = advice(example("finding_s3_tiering"))
        self.assertIn("GiB", a["observed"])
        self.assertIn("monitoring fee", a["charges"])
        self.assertIsNone(a["benefit"]["monthly"])

    def test_missing_estimate_is_not_zero(self):
        f = example("finding_ec2_idle")
        f["estimated_monthly_savings"] = None
        f["savings_note"] = "no price for x9.mega in us-east-1"
        b = advice(f)["benefit"]
        self.assertIsNone(b["monthly"])
        self.assertIn("x9.mega", b["note"])

    def test_resolved_finding_has_no_action(self):
        f = example("finding_ec2_idle")
        f["status"] = "resolved"
        self.assertIsNone(advice(f)["action"])

    def test_unknown_rule_and_broken_evidence(self):
        self.assertIsNone(advice({"rule": "something_new", "reason": "x"})["action"])
        f = example("finding_ec2_idle")
        f["evidence"] = {}
        self.assertIsNone(advice(f)["action"])
