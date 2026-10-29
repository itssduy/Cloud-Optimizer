from datetime import datetime, timedelta, timezone

import boto3

REGION = "us-east-1"
CPU_THRESHOLD = 5
HOURS = 24
INSTANCE_ID = None #put instance ID here 
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

# Stop one instance if an ID was provided
if INSTANCE_ID:
    ec2.stop_instances(InstanceIds=[INSTANCE_ID])
    print(f"Stop request sent for {INSTANCE_ID}")

# Find and optionally stop running instances with low CPU
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

if low_cpu_instances:
    answer = input("Stop these low-CPU instances? Type yes to continue: ")
    if answer.lower() == "yes":
        ec2.stop_instances(InstanceIds=low_cpu_instances)
        print("Stop request sent.")
