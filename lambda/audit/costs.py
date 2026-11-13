from datetime import timedelta
from decimal import Decimal

HOURS_PER_MONTH = 730
COST_TTL_DAYS = 400

# us-east-1 Linux on-demand $/hour, from the AWS EC2 pricing page.
# Types not listed here get no estimate instead of a guess.
EC2_HOURLY_USD = {
    "t2.micro": "0.0116", "t2.small": "0.023", "t2.medium": "0.0464", "t2.large": "0.0928",
    "t3.nano": "0.0052", "t3.micro": "0.0104", "t3.small": "0.0208", "t3.medium": "0.0416",
    "t3.large": "0.0832", "t3.xlarge": "0.1664",
    "t4g.micro": "0.0084", "t4g.small": "0.0168", "t4g.medium": "0.0336", "t4g.large": "0.0672",
    "m5.large": "0.096", "m5.xlarge": "0.192", "m6i.large": "0.096", "m6i.xlarge": "0.192",
    "c5.large": "0.085", "c5.xlarge": "0.17", "r5.large": "0.126",
}

ASSUMPTIONS = [
    "EC2: us-east-1 Linux on-demand price, stopped for a full month (730 hours)",
    "EC2: stopping only saves compute, EBS volumes are still billed",
    "S3: no estimate yet, we can't see access patterns or incomplete upload sizes",
    "Estimates are potential savings, not money already saved",
]


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def money(amount, estimated):
    return {"amount": Decimal(amount).quantize(Decimal("0.01")), "currency": "USD", "estimated": estimated}


def fetch_daily_costs(ce, account_id, now, days=30):
    """Daily unblended cost per service from Cost Explorer for the last `days` full days.

    Cost Explorer updates about once a day and the last day or two are estimates,
    so each day keeps the "estimated" flag. Days Cost Explorer doesn't return get
    no item, which means missing, not $0.
    """
    end = now.date()
    start = end - timedelta(days=days)
    kwargs = {
        "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost"],
        "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
        "Filter": {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [account_id]}},
    }
    items = []
    while True:
        page = ce.get_cost_and_usage(**kwargs)
        for day in page["ResultsByTime"]:
            estimated = day.get("Estimated", False)
            by_service = {}
            for group in day["Groups"]:
                cost = group["Metrics"]["UnblendedCost"]
                if cost["Unit"] != "USD":
                    raise ValueError(f"expected USD, got {cost['Unit']}")
                by_service[group["Keys"][0]] = money(cost["Amount"], estimated)
            total = sum((s["amount"] for s in by_service.values()), Decimal("0"))
            date = day["TimePeriod"]["Start"]
            items.append({
                "pk": f"ACCT#{account_id}",
                "sk": f"COST#{date}",
                "type": "daily_cost",
                "account_id": account_id,
                "date": date,
                "total": money(total, estimated),
                "by_service": by_service,
                "source": "cost_explorer",
                "fetched_at": iso(now),
                "expires_at": int((now + timedelta(days=COST_TTL_DAYS)).timestamp()),
            })
        if not page.get("NextPageToken"):
            return items
        kwargs["NextPageToken"] = page["NextPageToken"]


def estimate(finding):
    """Returns (money or None, note explaining it)."""
    if finding["rule"] == "ec2_idle_cpu":
        instance_type = finding["evidence"].get("instance_type")
        price = EC2_HOURLY_USD.get(instance_type)
        if finding["region"] != "us-east-1" or price is None:
            return None, f"no price for {instance_type} in {finding['region']}"
        monthly = Decimal(price) * HOURS_PER_MONTH
        return money(monthly, True), f"{instance_type} at ${price}/hour x {HOURS_PER_MONTH} hours"
    if finding["resource_type"] == "s3_bucket":
        return None, "S3 savings depend on access patterns we can't see"
    return None, "no estimate for this rule"


def calculate_savings(account_id, findings, now):
    """Fills in each open finding's estimate and returns the SAVINGS summary item.

    Each finding is a different resource + rule, so nothing is counted twice.
    """
    total = Decimal("0")
    missing = []
    open_findings = [f for f in findings if f["status"] == "open"]
    for f in open_findings:
        amount, note = estimate(f)
        f["estimated_monthly_savings"] = amount
        f["savings_note"] = note
        if amount is None:
            missing.append({"resource_id": f["resource_id"], "rule": f["rule"], "reason": note})
        else:
            total += amount["amount"]

    return {
        "pk": f"ACCT#{account_id}",
        "sk": "SAVINGS",
        "type": "savings",
        "account_id": account_id,
        "total_monthly": money(total, True),
        "finding_count": len(open_findings),
        "missing_estimate_count": len(missing),
        "missing_estimates": missing,
        "assumptions": ASSUMPTIONS,
        "calculated_at": iso(now),
    }
