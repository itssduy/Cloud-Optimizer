from datetime import timedelta

from botocore.exceptions import ClientError

UPLOAD_AGE_DAYS = 7
MIN_TIERING_BYTES = 10 * 1024**3  # 10 GiB
MIN_AVG_OBJECT_BYTES = 128 * 1024  # Intelligent-Tiering doesn't tier objects under 128 KiB


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def list_buckets(s3):
    buckets = []
    for page in s3.get_paginator("list_buckets").paginate():
        buckets.extend(page["Buckets"])
    return buckets


def bucket_region(s3, name):
    # us-east-1 buckets come back as None
    return s3.get_bucket_location(Bucket=name)["LocationConstraint"] or "us-east-1"


def lifecycle_rules(s3, name):
    try:
        return s3.get_bucket_lifecycle_configuration(Bucket=name)["Rules"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchLifecycleConfiguration":
            return []
        raise


def stale_uploads(s3, name, now, days):
    cutoff = now - timedelta(days=days)
    stale = []
    for page in s3.get_paginator("list_multipart_uploads").paginate(Bucket=name):
        stale.extend(u for u in page.get("Uploads", []) if u["Initiated"] < cutoff)
    return stale


def latest_metric(cloudwatch, name, metric, storage_type, now):
    points = cloudwatch.get_metric_statistics(
        Namespace="AWS/S3",
        MetricName=metric,
        Dimensions=[{"Name": "BucketName", "Value": name}, {"Name": "StorageType", "Value": storage_type}],
        StartTime=now - timedelta(days=3),
        EndTime=now,
        Period=86400,
        Statistics=["Average"],
    )["Datapoints"]
    if not points:
        return None, None
    latest = max(points, key=lambda p: p["Timestamp"])
    return int(latest["Average"]), iso(latest["Timestamp"])


def finding(account_id, region, bucket, rule, reason, evidence, recommendation, scan_id, now):
    return {
        "pk": f"ACCT#{account_id}",
        "sk": f"FIND#s3_bucket#{region}#{bucket}#{rule}",
        "type": "finding",
        "account_id": account_id,
        "resource_type": "s3_bucket",
        "region": region,
        "resource_id": bucket,
        "rule": rule,
        "status": "open",
        "reason": reason,
        "evidence": evidence,
        "recommendation": recommendation,
        "estimated_monthly_savings": None,
        "scan_id": scan_id,
        "first_detected_at": iso(now),
        "last_detected_at": iso(now),
        "resolved_at": None,
    }


def check_bucket(s3, cloudwatch, account_id, region, scan_id, now, name, upload_age_days,
                 min_tiering_bytes):
    resource = {
        "account_id": account_id,
        "resource_type": "s3_bucket",
        "region": region,
        "resource_id": name,
        "name": name,
        "state": "active",
        "details": {},
    }
    result = {"resource": resource, "outcome": None, "note": None, "findings": []}

    rules = lifecycle_rules(s3, name)
    enabled = [r for r in rules if r.get("Status") == "Enabled"]
    aborts_uploads = any("AbortIncompleteMultipartUpload" in r for r in enabled)
    has_transitions = any(r.get("Transitions") for r in enabled)

    size, size_at = latest_metric(cloudwatch, name, "BucketSizeBytes", "StandardStorage", now)
    count, _ = latest_metric(cloudwatch, name, "NumberOfObjects", "AllStorageTypes", now)
    resource["details"] = {
        "standard_size_bytes": size,
        "object_count": count,
        "metrics_at": size_at,
        "has_lifecycle_rules": bool(rules),
    }

    if not aborts_uploads:
        stale = stale_uploads(s3, name, now, upload_age_days)
        if stale:
            oldest = min(u["Initiated"] for u in stale)
            result["findings"].append(finding(
                account_id, region, name, "s3_incomplete_uploads",
                f"{len(stale)} incomplete multipart uploads older than {upload_age_days} days. "
                "You pay for their parts but they aren't visible as objects.",
                {"stale_upload_count": len(stale), "oldest_initiated": iso(oldest),
                 "age_threshold_days": upload_age_days, "checked_at": iso(now)},
                {"action": "s3_apply_lifecycle",
                 "text": f"Add a lifecycle rule to abort incomplete uploads after {upload_age_days} days.",
                 "proposed_rule": {"ID": "cloud-optimizer-abort-uploads", "Status": "Enabled",
                                   "Filter": {},
                                   "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": upload_age_days}}},
                scan_id, now,
            ))

    missing_metrics = size is None or count is None
    if not has_transitions and not missing_metrics and count > 0:
        avg = size // count
        if size >= min_tiering_bytes and avg >= MIN_AVG_OBJECT_BYTES:
            result["findings"].append(finding(
                account_id, region, name, "s3_tiering_candidate",
                f"{size / 1024**3:.1f} GiB in S3 Standard with no transition rules. "
                "We can't see how often it's read, so Intelligent-Tiering is the safe option.",
                {"standard_size_bytes": size, "object_count": count, "avg_object_bytes": avg,
                 "metrics_at": size_at, "min_size_bytes": min_tiering_bytes,
                 "access_pattern": "unknown"},
                {"action": "s3_apply_lifecycle",
                 "text": "Move objects to Intelligent-Tiering. It has a small monitoring fee per object.",
                 "proposed_rule": {"ID": "cloud-optimizer-tiering", "Status": "Enabled",
                                   "Filter": {"ObjectSizeGreaterThan": MIN_AVG_OBJECT_BYTES},
                                   "Transitions": [{"Days": 0, "StorageClass": "INTELLIGENT_TIERING"}]}},
                scan_id, now,
            ))

    if result["findings"]:
        result["outcome"] = "finding"
    elif missing_metrics and not has_transitions:
        result["outcome"] = "insufficient_data"
        result["note"] = "no S3 storage metrics yet (they update about once a day)"
    else:
        result["outcome"] = "clean"
    return result


def check_buckets(s3, cloudwatch, account_id, region, scan_id, now,
                  upload_age_days=UPLOAD_AGE_DAYS, min_tiering_bytes=MIN_TIERING_BYTES):
    """Returns one result per bucket in the region.

    outcome is "finding", "clean", "insufficient_data", or "error".
    Buckets in other regions are skipped. Nothing is changed in S3.
    """
    results = []
    for bucket in list_buckets(s3):
        name = bucket["Name"]
        try:
            if bucket_region(s3, name) != region:
                continue
            results.append(check_bucket(s3, cloudwatch, account_id, region, scan_id, now, name,
                                        upload_age_days, min_tiering_bytes))
        except ClientError as e:
            results.append({
                "resource": {"account_id": account_id, "resource_type": "s3_bucket", "region": region,
                             "resource_id": name, "name": name, "state": "unknown", "details": {}},
                "outcome": "error",
                "note": f"{e.response['Error']['Code']}: {e.response['Error'].get('Message', '')}",
                "findings": [],
            })
    return results
