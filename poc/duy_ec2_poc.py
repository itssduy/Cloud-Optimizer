import boto3

ec2 = boto3.client("ec2", region_name="us-east-1")

# List EC2 instances
response = ec2.describe_instances()

for reservation in response["Reservations"]:
    for instance in reservation["Instances"]:
        print(instance["InstanceId"], instance["State"]["Name"])
