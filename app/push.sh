#!/usr/bin/env bash
# builds the image and pushes it to ECR
# usage: ./push.sh [tag]
set -euo pipefail

cd "$(dirname "$0")"

REGION=${AWS_REGION:-us-east-1}
REPO=$(terraform -chdir=../infra output -raw ecr_repository_url)
CLUSTER=$(terraform -chdir=../infra output -raw ecs_cluster_name)
SERVICE=$(terraform -chdir=../infra output -raw ecs_service_name)
TAG=${1:-$(git rev-parse --short HEAD)}

aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${REPO%%/*}"

docker build --platform linux/amd64 -t "$REPO:$TAG" -t "$REPO:latest" .
docker push "$REPO:$TAG"
docker push "$REPO:latest"

echo "pushed $REPO:$TAG"

# the service runs :latest, so restart it to pick up the new image
aws ecs update-service --region "$REGION" --cluster "$CLUSTER" --service "$SERVICE" --force-new-deployment > /dev/null
echo "redeploying $SERVICE"
