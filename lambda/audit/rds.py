"""Idle RDS databases: no connections at all over the window."""
from common import MIN_COVERAGE, finding, metric, resource, result

WINDOW_DAYS = 7


def list_databases(rds):
    dbs = []
    for page in rds.get_paginator("describe_db_instances").paginate():
        dbs.extend(page["DBInstances"])
    return dbs


def check_databases(rds, cloudwatch, account_id, region, scan_id, now, days=WINDOW_DAYS):
    """One result per DB instance: "finding", "clean", "insufficient_data", or "ineligible"."""
    results = []
    for db in list_databases(rds):
        db_id = db["DBInstanceIdentifier"]
        r = resource(account_id, region, "rds_instance", db_id, db_id, db["DBInstanceStatus"], {
            "instance_class": db["DBInstanceClass"],
            "engine": db["Engine"],
            "multi_az": db.get("MultiAZ", False),
            "storage_gb": db.get("AllocatedStorage"),
            "storage_type": db.get("StorageType"),
        })
        res = result(r)
        results.append(res)

        if db["DBInstanceStatus"] != "available":
            res["outcome"], res["note"] = "ineligible", f"database is {db['DBInstanceStatus']}"
            continue
        if db["Engine"].startswith("aurora"):
            res["outcome"], res["note"] = "ineligible", "Aurora instances aren't checked yet"
            continue

        evidence = metric(cloudwatch, "AWS/RDS", "DatabaseConnections",
                          {"DBInstanceIdentifier": db_id}, "Maximum", now, days)
        values = evidence.pop("values")
        evidence["max_connections"] = max(values) if values else None
        if evidence["datapoints"] < evidence["expected_datapoints"] * MIN_COVERAGE:
            res["outcome"] = "insufficient_data"
            res["note"] = f"only {evidence['datapoints']} of {evidence['expected_datapoints']} connection datapoints"
            continue
        if evidence["max_connections"] > 0:
            res["outcome"] = "clean"
            continue

        res["outcome"] = "finding"
        res["findings"].append(finding(
            r, "rds_idle",
            f"No connections to this database in the last {evidence['window']}.",
            {**evidence, **r["details"]},
            {"action": None,
             "text": "Confirm nobody uses it, take a final snapshot, then stop or delete it."},
            scan_id, now,
        ))
    return results
