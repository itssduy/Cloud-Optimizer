from datetime import datetime, timedelta, timezone

import boto3

REGION = "us-east-1"
CPU_THRESHOLD = 5
HOURS = 24
session = boto3.Session(region_name=REGION)
ec2 = session.client("ec2")
cloudwatch = session.client("cloudwatch")

# List EC2 instances
instances = []

for page in ec2.get_paginator("describe_instances").paginate():
    for reservation in page["Reservations"]:
        instances.extend(reservation["Instances"])

print("EC2 Instances:")
for instance in instances:
    print(instance["InstanceId"], instance["State"]["Name"])

# Find running instances with low CPU
end = datetime.now(timezone.utc)
start = end - timedelta(hours=HOURS)
low_cpu_instances = []

for instance in instances:
    if instance["State"]["Name"] != "running":
        continue

    result = cloudwatch.get_metric_statistics(
        Namespace="AWS/EC2",
        MetricName="CPUUtilization",
        Dimensions=[{"Name": "InstanceId", "Value": instance["InstanceId"]}],
        StartTime=start,
        EndTime=end,
        Period=3600,
        Statistics=["Average"],
    )

    points = result["Datapoints"]
    if points:
        average_cpu = sum(point["Average"] for point in points) / len(points)
        if average_cpu < CPU_THRESHOLD:
            low_cpu_instances.append(instance["InstanceId"])
            print(f"{instance['InstanceId']} has low CPU: {average_cpu:.2f}%")

# Choose one low-CPU instance and confirm before stopping it
if low_cpu_instances:
    instance_id = input("Instance ID to stop (press Enter to skip): ").strip()
    if instance_id in low_cpu_instances:
        answer = input(f"Stop {instance_id}? Type yes to continue: ")
        if answer.lower() == "yes":
            ec2.stop_instances(InstanceIds=[instance_id])
            print(f"Stop request sent for {instance_id}")
