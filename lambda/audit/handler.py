# placeholder until the real audit is written

import json
import os


def lambda_handler(event, context):
    print(json.dumps({
        "message": "audit ran",
        "source": event.get("source"),
        "table": os.environ.get("DYNAMODB_TABLE_NAME"),
    }))
    return {"status": "ok"}
