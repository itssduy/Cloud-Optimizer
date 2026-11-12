from datetime import timedelta

CPU_THRESHOLD = 5.0
WINDOW_DAYS = 7
MIN_COVERAGE = 0.8
PERIOD_SECONDS = 3600


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def list_instances(ec2):
    instances = []
    for page in ec2.get_paginator("describe_instances").paginate():
        for reservation in page["Reservations"]:
            instances.extend(reservation["Instances"])
    return instances


def cpu_evidence(cloudwatch, instance_id, now, days):
    start = now - timedelta(days=days)
    points = cloudwatch.get_metric_statistics(
        Namespace="AWS/EC2",
        MetricName="CPUUtilization",
        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        StartTime=start,
        EndTime=now,
        Period=PERIOD_SECONDS,
        Statistics=["Average"],
    )["Datapoints"]
    expected = days * 24
    average = round(sum(p["Average"] for p in points) / len(points), 2) if points else None
    return {
        "metric": "CPUUtilization",
        "statistic": "Average",
        "average": average,
        "period_days": days,
        "datapoints": len(points),
        "expected_datapoints": expected,
        "window_start": iso(start),
        "window_end": iso(now),
    }


def resource_for(instance, account_id, region):
    tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
    return {
        "account_id": account_id,
        "resource_type": "ec2_instance",
        "region": region,
        "resource_id": instance["InstanceId"],
        "name": tags.get("Name", instance["InstanceId"]),
        "state": instance["State"]["Name"],
        "details": {
            "instance_type": instance["InstanceType"],
            "launch_time": iso(instance["LaunchTime"]),
            "lifecycle": instance.get("InstanceLifecycle", "on-demand"),
            "tags": tags,
        },
    }


def ineligible_reason(instance):
    if instance["State"]["Name"] != "running":
        return f"instance is {instance['State']['Name']}"
    if instance.get("InstanceLifecycle") == "spot":
        return "spot instance"
    if any(t["Key"] == "aws:autoscaling:groupName" for t in instance.get("Tags", [])):
        return "managed by an auto scaling group"
    return None


def check_instances(ec2, cloudwatch, account_id, region, scan_id, now,
                    threshold=CPU_THRESHOLD, days=WINDOW_DAYS, min_coverage=MIN_COVERAGE):
    """Returns one result per instance:

    outcome is "finding", "clean", "insufficient_data", or "ineligible".
    Only "clean" means we checked and it looks fine.
    """
    results = []
    for instance in list_instances(ec2):
        resource = resource_for(instance, account_id, region)
        result = {"resource": resource, "outcome": None, "note": None, "findings": []}
        results.append(result)

        reason = ineligible_reason(instance)
        if reason:
            result["outcome"], result["note"] = "ineligible", reason
            continue

        evidence = cpu_evidence(cloudwatch, instance["InstanceId"], now, days)
        evidence["threshold"] = threshold
        if evidence["datapoints"] < evidence["expected_datapoints"] * min_coverage:
            result["outcome"] = "insufficient_data"
            result["note"] = (f"only {evidence['datapoints']} of {evidence['expected_datapoints']} "
                              f"hourly CPU datapoints")
            continue

        if evidence["average"] >= threshold:
            result["outcome"] = "clean"
            continue

        result["outcome"] = "finding"
        result["findings"].append({
            "pk": f"ACCT#{account_id}",
            "sk": f"FIND#ec2_instance#{region}#{instance['InstanceId']}#ec2_idle_cpu",
            "type": "finding",
            "account_id": account_id,
            "resource_type": "ec2_instance",
            "region": region,
            "resource_id": instance["InstanceId"],
            "rule": "ec2_idle_cpu",
            "status": "open",
            "reason": (f"Average CPU was {evidence['average']}% over the last {days} days "
                       f"(threshold {threshold:g}%). Low CPU doesn't always mean it's unused."),
            "evidence": {**evidence, "instance_type": instance["InstanceType"]},
            "recommendation": {
                "action": "ec2_stop",
                "text": "Check if the instance is still needed. Stop it or move to a smaller type.",
            },
            "estimated_monthly_savings": None,
            "scan_id": scan_id,
            "first_detected_at": iso(now),
            "last_detected_at": iso(now),
            "resolved_at": None,
        })
    return results
