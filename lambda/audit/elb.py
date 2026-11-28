"""Idle load balancers: no registered targets, or no traffic over the window."""
from common import finding, iso, metric, resource, result

WINDOW_DAYS = 7
TRAFFIC_METRIC = {"application": "RequestCount", "network": "NewFlowCount"}


def list_load_balancers(elbv2):
    lbs = []
    for page in elbv2.get_paginator("describe_load_balancers").paginate():
        lbs.extend(page["LoadBalancers"])
    return lbs


def target_count(elbv2, lb_arn):
    try:
        groups = elbv2.describe_target_groups(LoadBalancerArn=lb_arn)["TargetGroups"]
    except elbv2.exceptions.TargetGroupNotFoundException:
        groups = []
    total = 0
    for group in groups:
        health = elbv2.describe_target_health(TargetGroupArn=group["TargetGroupArn"])
        total += len(health["TargetHealthDescriptions"])
    return total


def check_load_balancers(elbv2, cloudwatch, account_id, region, scan_id, now, days=WINDOW_DAYS):
    """One result per ALB/NLB: "finding", "clean", or "ineligible"."""
    results = []
    for lb in list_load_balancers(elbv2):
        kind = lb["Type"]
        r = resource(account_id, region, "load_balancer", lb["LoadBalancerName"], lb["LoadBalancerName"],
                     lb["State"]["Code"], {"type": kind, "scheme": lb.get("Scheme"), "dns_name": lb.get("DNSName")})
        res = result(r)
        results.append(res)

        if kind not in TRAFFIC_METRIC:
            res["outcome"], res["note"] = "ineligible", f"{kind} load balancers aren't checked"
            continue
        if (now - lb["CreatedTime"]).total_seconds() < days * 86400:
            res["outcome"], res["note"] = "ineligible", "created during the check window"
            continue

        targets = target_count(elbv2, lb["LoadBalancerArn"])
        if targets == 0:
            res["outcome"] = "finding"
            res["findings"].append(finding(
                r, "elb_no_targets",
                "This load balancer has no registered targets, so it can't serve traffic but is still billed.",
                {"type": kind, "registered_targets": 0, "checked_at": iso(now)},
                {"action": None, "text": "If it's left over from an old app, delete it."},
                scan_id, now,
            ))
            continue

        # CloudWatch names a load balancer by the end of its ARN, e.g. app/web/50dc6c495c0c9188
        dimension = lb["LoadBalancerArn"].split(":loadbalancer/", 1)[1]
        evidence = metric(cloudwatch, f"AWS/{'ApplicationELB' if kind == 'application' else 'NetworkELB'}",
                          TRAFFIC_METRIC[kind], {"LoadBalancer": dimension}, "Sum", now, days)
        evidence["requests"] = int(sum(evidence.pop("values")))
        if evidence["requests"] > 0:
            res["outcome"] = "clean"
            continue

        res["outcome"] = "finding"
        res["findings"].append(finding(
            r, "elb_idle",
            f"No traffic in the last {evidence['window']}.",
            {**evidence, "type": kind, "registered_targets": targets},
            {"action": None, "text": "Check if anything still points at it. If not, delete it."},
            scan_id, now,
        ))
    return results
