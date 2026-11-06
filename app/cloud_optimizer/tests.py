import json
from decimal import Decimal

from django.test import SimpleTestCase

from . import schema as s
from .schema_examples import EXAMPLES_DIR, build_examples, to_json


class SchemaTests(SimpleTestCase):
    def test_example_files_match_schema(self):
        for name, item in build_examples().items():
            path = EXAMPLES_DIR / f"{name}.json"
            expected = json.loads(json.dumps(item, default=to_json))
            self.assertEqual(json.loads(path.read_text()), expected, f"{name}.json is out of date")

    def test_account_items_share_one_partition(self):
        examples = build_examples()
        pk = s.account_pk("u_7f3a9c21", "123456789012")
        for name in ("scan_completed", "resource_ec2", "finding_ec2_idle", "daily_cost",
                     "savings", "alert", "action_ec2_stop"):
            self.assertEqual(examples[name]["pk"], pk, name)

    def test_repeat_scan_gives_same_keys(self):
        a = build_examples()["finding_ec2_idle"]
        b = build_examples()["finding_ec2_idle"]
        self.assertEqual((a["pk"], a["sk"]), (b["pk"], b["sk"]))

    def test_missing_savings_is_none_not_zero(self):
        self.assertIsNone(s.money(None))
        self.assertEqual(s.money(0)["amount"], Decimal("0.00"))

    def test_bad_status_rejected(self):
        with self.assertRaises(ValueError):
            s.connection_item("u", "1", "arn", "ext", status="bogus")

    def test_for_dynamo_converts_floats(self):
        self.assertEqual(s.for_dynamo({"a": [1.5, {"b": 2.25}]}), {"a": [Decimal("1.5"), {"b": Decimal("2.25")}]})

    def test_email_is_lowercased(self):
        self.assertEqual(s.email_lookup_item("Duy@Example.com", "u")["pk"], "EMAIL#duy@example.com")
