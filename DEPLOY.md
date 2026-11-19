# Deploying Cloud Optimizer

Cloud Optimizer runs inside your own AWS account and scans that same account. These are the steps we used to deploy it.

## What you need

- An AWS account and CLI credentials for it (`aws sts get-caller-identity` should show the right account)
- Terraform 1.15.8+
- Docker
- [uv](https://docs.astral.sh/uv/) for running the app and tests locally

Everything goes in `us-east-1` by default. Change `aws_region` in `infra/terraform.tfvars` if you need another region.

## Deploy

```bash
cd infra
terraform init
terraform apply            # creates ECR, DynamoDB, ECS, the audit Lambda, IAM roles, etc.

cd ../app
./push.sh                  # builds the image and pushes it to ECR

cd ../infra
echo 'app_desired_count = 1' > terraform.tfvars
terraform apply            # starts the app
```

Terraform state is kept locally in `infra/terraform.tfstate`. Only one person should run `apply` against the same account. See `backend.tf.example` if you want shared state in S3.

There's no load balancer, so the app is reached on the task's public IP, port 8000. To find it:

```bash
TASK=$(aws ecs list-tasks --cluster cloud-optimizer-dev --service-name cloud-optimizer-dev-app --query 'taskArns[0]' --output text)
ENI=$(aws ecs describe-tasks --cluster cloud-optimizer-dev --tasks $TASK --query "tasks[0].attachments[0].details[?name=='networkInterfaceId'].value" --output text)
aws ec2 describe-network-interfaces --network-interface-ids $ENI --query 'NetworkInterfaces[0].Association.PublicIp' --output text
```

The IP changes every time the app redeploys. `http://<ip>:8000/health/` should return `ok`.

## First login

Go to `http://<ip>:8000/setup/` right away and create the admin account. That page only works until the first account exists, so do it before sharing the URL.

After that, admins add teammates on the Users page with a temporary password. Admins can disable people, change roles, and reset passwords. There's no email, so a forgotten password is reset by an admin.

- `member` can see everything and start/stop instances or apply S3 rules
- `admin` can also manage users

## Scans

The audit Lambda runs every day at 06:00 UTC. It scans EC2 and S3 in the deployment region, pulls the last 30 days of spending from Cost Explorer, and checks for spending spikes. It never changes resources by itself.

Run one now instead of waiting:

```bash
aws lambda invoke --function-name cloud-optimizer-dev-audit --payload '{}' --cli-binary-format raw-in-base64-out out.json && cat out.json
```

Reloading the dashboard only shows saved results. It doesn't start a scan.

Logs are in CloudWatch under `/aws/lambda/cloud-optimizer-dev-audit` (audit) and `/ecs/cloud-optimizer-dev` (app). A scan shows `partial` when part of it failed (the error list says what) and the status page shows overdue if there's been no scan in 26 hours.

What the scan can and can't tell you:

- EC2 needs 7 days of CPU metrics before it can flag an idle instance. New instances show "not enough data".
- S3 storage metrics only update about once a day, so new buckets take a day to show up properly.
- The last day or two of Cost Explorer data is an estimate and can change.
- Savings are estimates of what you could save, not money already saved. S3 has no dollar estimate yet.

## Updating the app

```bash
cd app && ./push.sh
```

This pushes a new image and restarts the app.

## Backups

DynamoDB point-in-time recovery is on, so the table can be restored to any second in the last 35 days. To restore into a new table:

```bash
aws dynamodb restore-table-to-point-in-time \
  --source-table-name cloud-optimizer-dev \
  --target-table-name cloud-optimizer-dev-restored \
  --restore-date-time 2026-11-19T20:00:00Z
```

Then point the app and Lambda at the restored table (change the table name in Terraform, or swap the data back), and delete whichever table you don't need.

## Permissions

- The app can read/write its own DynamoDB table, describe/start/stop EC2 instances, and read/update S3 lifecycle rules.
- The audit Lambda is read-only on EC2, CloudWatch, S3, and Cost Explorer, plus its own table.
- Both are limited to the account they're deployed in.

## Known limits

- HTTP only, no HTTPS, because there's no load balancer yet. Don't reuse real passwords.
- Port 8000 is open to the internet. Only people with an account can log in, but anyone can reach the login page.
- One account and one region per deployment.
- Only EC2 and S3 for now.

## Cleanup

```bash
cd infra
terraform destroy
```

This deletes everything Cloud Optimizer created, including the table with all its data and the ECR images. It doesn't touch the instances and buckets it was scanning.

Lifecycle rules applied through the app stay on those buckets after cleanup. They're named `cloud-optimizer-*` if you want to remove them by hand.

To just pause scans without deleting anything, set `enable_audit_schedule = false` in `terraform.tfvars` and run `terraform apply`. Setting `app_desired_count = 0` stops the app and most of the cost.
