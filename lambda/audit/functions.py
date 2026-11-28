"""Unused Lambda functions: no invocations over the window."""
from datetime import datetime, timezone

from common import finding, metric, resource, result

WINDOW_DAYS = 7


def list_functions(lam):
    functions = []
    for page in lam.get_paginator("list_functions").paginate():
        functions.extend(page["Functions"])
    return functions


def modified_at(fn):
    # LastModified looks like 2026-11-20T10:15:00.000+0000 and is always UTC
    return datetime.strptime(fn["LastModified"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def check_functions(lam, cloudwatch, account_id, region, scan_id, now, days=WINDOW_DAYS, skip=()):
    """One result per function: "finding", "clean", or "ineligible".

    Missing invocation datapoints mean zero invocations, so there's no coverage check.
    Functions in `skip` (like this audit itself) are left out.
    """
    results = []
    for fn in list_functions(lam):
        name = fn["FunctionName"]
        if name in skip:
            continue
        r = resource(account_id, region, "lambda_function", name, name, "active", {
            "runtime": fn.get("Runtime"),
            "memory_mb": fn.get("MemorySize"),
            "last_modified": fn["LastModified"],
        })
        res = result(r)
        results.append(res)

        if (now - modified_at(fn)).total_seconds() < days * 86400:
            res["outcome"], res["note"] = "ineligible", "changed during the check window"
            continue

        evidence = metric(cloudwatch, "AWS/Lambda", "Invocations", {"FunctionName": name}, "Sum", now, days)
        evidence["invocations"] = int(sum(evidence.pop("values")))
        if evidence["invocations"] > 0:
            res["outcome"] = "clean"
            continue

        res["outcome"] = "finding"
        res["findings"].append(finding(
            r, "lambda_unused",
            f"Not invoked in the last {evidence['window']}.",
            {**evidence, "runtime": fn.get("Runtime"), "memory_mb": fn.get("MemorySize")},
            {"action": None,
             "text": "Check if anything still calls it. If not, delete it to reduce clutter and risk."},
            scan_id, now,
        ))
    return results
