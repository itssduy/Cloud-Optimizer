"""Plain-language advice for each finding, built from its saved evidence.

This only describes what to do. Actions are separate (actions.py, lifecycle.py).
"""


def gib(n):
    return f"{n / 1024**3:.1f} GiB"


def benefit(finding):
    savings = finding.get("estimated_monthly_savings")
    if not savings:
        return {"monthly": None, "note": finding.get("savings_note") or "no estimate yet"}
    return {"monthly": savings, "note": finding.get("savings_note"), "estimated": True}


def ec2_idle(f):
    e = f["evidence"]
    return {
        "observed": (f"Average CPU was {e['average']}% over {e.get('window') or str(e['period_days']) + ' days'} "
                     f"({e['datapoints']} of {e['expected_datapoints']} hourly datapoints)."),
        "why": "Running instances are billed every hour, even when they do very little.",
        "next_step": "Check with whoever owns it. If it's not needed, stop it, or move it to a smaller type.",
        "limitations": [
            "Low CPU doesn't prove it's unused. It might be busy on memory, network, or rare jobs.",
            "Stopping it keeps the EBS volumes, and those are still billed.",
        ],
        "action": "ec2_stop",
    }


def s3_uploads(f):
    e = f["evidence"]
    return {
        "observed": (f"{e['stale_upload_count']} incomplete uploads older than {e['age_threshold_days']} days, "
                     f"the oldest started {e['oldest_initiated'][:10]}."),
        "why": "Parts of unfinished uploads are stored and billed, but don't show up as objects.",
        "next_step": f"Add a lifecycle rule that aborts incomplete uploads after {max(1, e['age_threshold_days'])} days.",
        "limitations": ["We don't know how much data the parts hold, so there's no savings number."],
        "charges": "No extra charge.",
        "action": "s3_apply_lifecycle",
    }


def s3_tiering(f):
    e = f["evidence"]
    return {
        "observed": f"{gib(e['standard_size_bytes'])} across {e['object_count']:,} objects in S3 Standard, with no transition rules.",
        "why": "Standard is the most expensive storage class for data that's rarely read.",
        "next_step": "Move objects into Intelligent-Tiering, which moves them to cheaper tiers when they aren't read.",
        "limitations": [
            "We can't see how often objects are read, so the benefit is unknown.",
            "Objects under 128 KiB aren't moved.",
        ],
        "charges": "Small monitoring fee per object, plus a one-time transition request charge.",
        "action": "s3_apply_lifecycle",
    }


BUILDERS = {"ec2_idle_cpu": ec2_idle, "s3_incomplete_uploads": s3_uploads, "s3_tiering_candidate": s3_tiering}


def advice(finding):
    build = BUILDERS.get(finding.get("rule"))
    if not build:
        return {"observed": finding.get("reason"), "why": None, "next_step": None,
                "limitations": ["No advice for this rule yet."], "action": None, "benefit": benefit(finding)}
    try:
        result = build(finding)
    except (KeyError, TypeError):
        return {"observed": finding.get("reason"), "why": None, "next_step": None,
                "limitations": ["Evidence is incomplete, so there's no detailed advice."],
                "action": None, "benefit": benefit(finding)}
    if finding.get("status") != "open":
        result["action"] = None
    result["benefit"] = benefit(finding)
    return result
