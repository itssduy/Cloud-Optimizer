"""Example items for every record type, so the frontend can build against them.

Regenerate the JSON files after changing schema.py:

    uv run python -m cloud_optimizer.schema_examples
"""

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from . import schema as s

EXAMPLES_DIR = Path(__file__).parent / "examples"

USER = "u_7f3a9c21"
ACCOUNT = "123456789012"
SCAN = "scan_4b8e1d0a"
REGION = "us-east-1"
INSTANCE = "i-0abc123def4567890"
BUCKET = "acme-old-logs"


def t(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def build_examples():
    return {
        "user": s.user_item(
            USER, "duy@example.com", "Duy",
            password_hash="pbkdf2_sha256$1000000$salt$hash", github_id=None,
            created_at=t("2026-11-01T18:20:00Z"),
        ),
        "email_lookup": s.email_lookup_item("duy@example.com", USER),
        "connection": s.connection_item(
            USER, ACCOUNT,
            role_arn=f"arn:aws:iam::{ACCOUNT}:role/CloudOptimizerAccess",
            external_id="co-2f9d6b7e4a1c",
            status="connected",
            verified_at=t("2026-11-01T18:25:00Z"),
            last_scan_at=t("2026-11-06T06:00:00Z"),
            created_at=t("2026-11-01T18:24:00Z"),
            updated_at=t("2026-11-06T06:03:12Z"),
        ),
        "scan_completed": s.scan_item(
            USER, ACCOUNT, SCAN,
            started_at=t("2026-11-06T06:00:00Z"),
            completed_at=t("2026-11-06T06:03:12Z"),
            status="completed", resource_count=5, finding_count=2,
        ),
        "scan_partial": s.scan_item(
            USER, ACCOUNT, "scan_91c0e7f2",
            started_at=t("2026-11-05T06:00:00Z"),
            completed_at=t("2026-11-05T06:02:40Z"),
            status="partial", resource_count=3, finding_count=1,
            errors=[{"service": "s3", "region": REGION, "message": "AccessDenied on ListBuckets"}],
        ),
        "resource_ec2": s.resource_item(
            USER, ACCOUNT, "ec2_instance", REGION, INSTANCE,
            name="old-staging-api", state="running",
            details={"instance_type": "t3.medium", "launch_time": "2026-08-14T15:02:00Z",
                     "platform": "linux", "tags": {"env": "staging"}},
            scan_id=SCAN,
            first_seen_at=t("2026-11-01T18:30:00Z"), last_seen_at=t("2026-11-06T06:01:05Z"),
        ),
        "resource_s3": s.resource_item(
            USER, ACCOUNT, "s3_bucket", REGION, BUCKET,
            name=BUCKET, state="active",
            details={"object_count": 48210, "size_bytes": 412000000000,
                     "storage_classes": {"STANDARD": 412000000000}, "has_lifecycle_rules": False},
            scan_id=SCAN,
            first_seen_at=t("2026-11-01T18:30:00Z"), last_seen_at=t("2026-11-06T06:01:40Z"),
        ),
        "finding_ec2_idle": s.finding_item(
            USER, ACCOUNT, "ec2_instance", REGION, INSTANCE,
            rule="ec2_idle_cpu",
            reason="Average CPU was 1.2% over the last 7 days (threshold 5%).",
            evidence={"metric": "CPUUtilization", "statistic": "Average", "average": 1.2,
                      "threshold": 5, "period_days": 7, "datapoints": 168, "expected_datapoints": 168},
            recommendation={"action": "ec2_stop",
                            "text": "Stop the instance if it isn't needed, or downsize it."},
            estimated_monthly_savings=s.money(30.37, estimated=True),
            scan_id=SCAN,
            first_detected_at=t("2026-11-02T06:01:10Z"), last_detected_at=t("2026-11-06T06:01:05Z"),
        ),
        "finding_s3_no_lifecycle": s.finding_item(
            USER, ACCOUNT, "s3_bucket", REGION, BUCKET,
            rule="s3_no_lifecycle",
            reason="412 GB in STANDARD with no lifecycle rules.",
            evidence={"size_bytes": 412000000000, "storage_class": "STANDARD",
                      "has_lifecycle_rules": False},
            recommendation={"action": "s3_apply_lifecycle",
                            "text": "Move objects older than 30 days to Standard-IA."},
            estimated_monthly_savings=None,
            scan_id=SCAN,
            first_detected_at=t("2026-11-06T06:01:40Z"), last_detected_at=t("2026-11-06T06:01:40Z"),
        ),
        "daily_cost": s.daily_cost_item(
            USER, ACCOUNT, "2026-11-05",
            total=s.money(4.82, estimated=True),
            by_service={"Amazon Elastic Compute Cloud - Compute": s.money(3.10, estimated=True),
                        "Amazon Simple Storage Service": s.money(1.72, estimated=True)},
            fetched_at=t("2026-11-06T06:02:30Z"),
        ),
        "savings": s.savings_item(
            USER, ACCOUNT,
            total_monthly=s.money(30.37, estimated=True),
            finding_count=2, missing_estimate_count=1,
            assumptions=["On-demand Linux price in us-east-1, 730 hours/month",
                         "Stopped instances only save compute, not EBS storage"],
            calculated_at=t("2026-11-06T06:03:00Z"),
        ),
        "alert": s.alert_item(
            USER, ACCOUNT, "alert_5d2c8e10", "2026-11-05",
            actual=s.money(4.82, estimated=True), expected=s.money(2.10),
            baseline_days=14,
            explanation="Spend was 130% above the 14-day average, mostly from EC2.",
            top_services=[{"service": "Amazon Elastic Compute Cloud - Compute",
                           "actual": s.money(3.10, estimated=True), "expected": s.money(0.95)}],
            created_at=t("2026-11-06T06:03:05Z"),
        ),
        "action_ec2_stop": s.action_item(
            USER, ACCOUNT, "act_e1f04b77", "ec2_stop", "ec2_instance", REGION, INSTANCE,
            change={"before": {"state": "running"}, "after": {"state": "stopped"}},
            requested_by=USER,
            requested_at=t("2026-11-06T14:12:00Z"),
            request_status="sent", outcome="verified",
            outcome_checked_at=t("2026-11-06T14:13:30Z"),
        ),
    }


def to_json(value):
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"can't serialize {type(value)}")


def write_examples():
    EXAMPLES_DIR.mkdir(exist_ok=True)
    for name, item in build_examples().items():
        path = EXAMPLES_DIR / f"{name}.json"
        path.write_text(json.dumps(item, indent=2, default=to_json) + "\n")


if __name__ == "__main__":
    write_examples()
