# DynamoDB keys. See examples/ for what each item looks like.
#
# USER#<user_id>               PROFILE
# EMAIL#<email>                LOOKUP
# DEPLOYMENT                   CONFIG
# ACCT#<account_id>            SCAN#<started_at>#<scan_id>
# ACCT#<account_id>            RES#<type>#<region>#<resource_id>
# ACCT#<account_id>            FIND#<type>#<region>#<resource_id>#<rule>
# ACCT#<account_id>            COST#<date>
# ACCT#<account_id>            SAVINGS
# ACCT#<account_id>            ALERT#<date>#<alert_id>
# ACCT#<account_id>            ACTION#<requested_at>#<action_id>

SCAN_TTL_DAYS = 90
COST_TTL_DAYS = 400


def user_pk(user_id):
    return f"USER#{user_id}"


def email_pk(email):
    return f"EMAIL#{email.lower()}"


DEPLOYMENT_PK = "DEPLOYMENT"


def account_pk(account_id):
    return f"ACCT#{account_id}"


def scan_sk(started_at, scan_id):
    return f"SCAN#{started_at}#{scan_id}"


def resource_sk(resource_type, region, resource_id):
    return f"RES#{resource_type}#{region}#{resource_id}"


def finding_sk(resource_type, region, resource_id, rule):
    return f"FIND#{resource_type}#{region}#{resource_id}#{rule}"


def cost_sk(date):
    return f"COST#{date}"


def alert_sk(date, alert_id):
    return f"ALERT#{date}#{alert_id}"


def action_sk(requested_at, action_id):
    return f"ACTION#{requested_at}#{action_id}"
