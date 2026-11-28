"""Unattached EBS volumes: disks that aren't attached to any instance."""
from common import finding, iso, resource, result, tags_of


def list_volumes(ec2):
    volumes = []
    for page in ec2.get_paginator("describe_volumes").paginate():
        volumes.extend(page["Volumes"])
    return volumes


def check_volumes(ec2, account_id, region, scan_id, now):
    """One result per volume: "finding" if it's unattached, otherwise "clean"."""
    results = []
    for v in list_volumes(ec2):
        tags = tags_of(v.get("Tags"))
        r = resource(account_id, region, "ebs_volume", v["VolumeId"], tags.get("Name", v["VolumeId"]), v["State"], {
            "size_gb": v["Size"],
            "volume_type": v["VolumeType"],
            "created": iso(v["CreateTime"]),
            "attached_to": [a["InstanceId"] for a in v.get("Attachments", [])],
        })
        res = result(r, "clean")
        results.append(res)
        if v["State"] != "available":
            continue

        res["outcome"] = "finding"
        res["findings"].append(finding(
            r, "ebs_unattached",
            f"{v['Size']} GiB {v['VolumeType']} volume isn't attached to any instance, but it's still billed.",
            {"size_gb": v["Size"], "volume_type": v["VolumeType"], "created": iso(v["CreateTime"]),
             "checked_at": iso(now)},
            {"action": None,
             "text": "If nobody needs the data, snapshot it and delete the volume."},
            scan_id, now,
        ))
    return results
