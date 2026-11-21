#!/usr/bin/env bash
# Builds the app image and pushes it to ECR. Terraform runs this when the app code changes.
set -euo pipefail

# throwaway docker config so `docker login` works even if the local credential helper isn't set up
export DOCKER_CONFIG=$(mktemp -d)
trap 'rm -rf "$DOCKER_CONFIG"' EXIT
echo "{\"currentContext\": \"$(docker --config "$HOME/.docker" context show)\"}" > "$DOCKER_CONFIG/config.json"
for dir in contexts cli-plugins; do
  [ -d "$HOME/.docker/$dir" ] && ln -s "$HOME/.docker/$dir" "$DOCKER_CONFIG/$dir"
done

aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${REPO%%/*}"
docker build --platform linux/amd64 -t "$REPO:$TAG" "$APP_DIR"
docker push "$REPO:$TAG"
