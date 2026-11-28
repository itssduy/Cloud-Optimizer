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

# us-east-1 single-AZ MySQL/PostgreSQL/MariaDB on-demand $/hour. Multi-AZ is double.
RDS_HOURLY_USD = {
    "db.t3.micro": "0.017", "db.t3.small": "0.034", "db.t3.medium": "0.068", "db.t3.large": "0.136",
    "db.t4g.micro": "0.016", "db.t4g.small": "0.032", "db.t4g.medium": "0.065", "db.t4g.large": "0.129",
    "db.m5.large": "0.171", "db.m6g.large": "0.152", "db.r5.large": "0.25",
}
RDS_ENGINES = {"mysql", "postgres", "mariadb"}
RDS_STORAGE_GB_MONTH = {"gp2": "0.115", "gp3": "0.115"}

# us-east-1 EBS $/GB-month
EBS_GB_MONTH = {"gp3": "0.08", "gp2": "0.10", "io1": "0.125", "io2": "0.125",
                "st1": "0.045", "sc1": "0.015", "standard": "0.05"}

# ALB and NLB both cost $0.0225/hour before capacity units
ELB_HOURLY_USD = "0.0225"

ASSUMPTIONS = [
    "EC2: us-east-1 Linux on-demand price, stopped for a full month (730 hours)",
    "EC2: stopping only saves compute, EBS volumes are still billed",
    "S3: no estimate yet, we can't see access patterns or incomplete upload sizes",
    "RDS: us-east-1 on-demand instance price plus gp2/gp3 storage, if deleted",
    "EBS: us-east-1 storage price per GB-month, if deleted (snapshots cost extra)",
    "Load balancers: base hourly price only, capacity units not included",
    "Lambda: unused functions cost nothing, so there's no savings number",
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
    if finding["region"] != "us-east-1" and finding["rule"] in ("rds_idle", "ebs_unattached", "elb_idle", "elb_no_targets"):
        return None, f"no prices for {finding['region']}"
    if finding["rule"] == "rds_idle":
        e = finding["evidence"]
        price = RDS_HOURLY_USD.get(e.get("instance_class"))
        if price is None or e.get("engine") not in RDS_ENGINES:
            return None, f"no price for {e.get('engine')} on {e.get('instance_class')}"
        hourly = Decimal(price) * (2 if e.get("multi_az") else 1)
        storage = Decimal(RDS_STORAGE_GB_MONTH.get(e.get("storage_type"), "0")) * (e.get("storage_gb") or 0)
        return (money(hourly * HOURS_PER_MONTH + storage, True),
                f"{e['instance_class']} at ${hourly}/hour x {HOURS_PER_MONTH} hours plus {e.get('storage_gb')} GB storage")
    if finding["rule"] == "ebs_unattached":
        e = finding["evidence"]
        price = EBS_GB_MONTH.get(e["volume_type"])
        if price is None:
            return None, f"no price for {e['volume_type']} volumes"
        return money(Decimal(price) * e["size_gb"], True), f"{e['size_gb']} GB {e['volume_type']} at ${price}/GB-month"
    if finding["rule"] in ("elb_idle", "elb_no_targets"):
        return (money(Decimal(ELB_HOURLY_USD) * HOURS_PER_MONTH, True),
                f"${ELB_HOURLY_USD}/hour x {HOURS_PER_MONTH} hours, capacity units not included")
    if finding["rule"] == "lambda_unused":
        return None, "unused functions don't cost anything"
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
