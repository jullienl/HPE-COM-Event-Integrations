#!/usr/bin/env bash
#
# Deploy the COM cloud relay to AWS App Runner (managed public HTTPS + TLS).
# Mirrors the CI-published GHCR image into your private ECR (App Runner cannot
# pull from GHCR), provisions an SQS queue, then deploys the relay pointed at it.
# Prints the webhook URL at the end.
#
# Prerequisites: AWS CLI configured (aws configure), Docker (for the image
# mirror), an IAM instance role for App Runner allowing sqs:SendMessage, and an
# App Runner ECR access role (AWSAppRunnerServicePolicyForECRAccess).
# Usage: IMAGE=<ecr-uri> ACCESS_ROLE_ARN=<role> INSTANCE_ROLE_ARN=<role> ./deploy-relay-aws.sh
#
set -euo pipefail

# ---- Config (override via env before running) -----------------------------
REGION="${AWS_REGION:-eu-west-1}"
QUEUE="${QUEUE:-com-events}"
APP_NAME="${APP_NAME:-com-event-relay}"
GHCR_IMAGE="${GHCR_IMAGE:-ghcr.io/jullienl/com-event-relay:latest}"   # upstream (published by CI)
IMAGE="${IMAGE:?Set IMAGE to your private ECR URI, e.g. <acct>.dkr.ecr.<region>.amazonaws.com/com-event-relay:latest}"
SHARED_SECRET_HEADER="${SHARED_SECRET_HEADER:-x-shim-secret}"
SECRET="${SECRET:-$(openssl rand -hex 32)}"
INSTANCE_ROLE_ARN="${INSTANCE_ROLE_ARN:?Set INSTANCE_ROLE_ARN to an IAM role ARN allowing sqs:SendMessage}"
ACCESS_ROLE_ARN="${ACCESS_ROLE_ARN:?Set ACCESS_ROLE_ARN to an App Runner ECR access role ARN}"

echo ">> Using REGION=$REGION QUEUE=$QUEUE APP=$APP_NAME"

# ---- Mirror GHCR image into private ECR -----------------------------------
# App Runner can pull only from ECR/ECR Public, so copy the public GHCR image
# into the ECR repo referenced by $IMAGE. buildx imagetools copies the full
# multi-arch manifest directly (no local pull).
REGISTRY="${IMAGE%%/*}"                       # <acct>.dkr.ecr.<region>.amazonaws.com
ECR_REPO="${IMAGE#*/}"; ECR_REPO="${ECR_REPO%%:*}"   # repository name
aws ecr create-repository --repository-name "$ECR_REPO" --region "$REGION" >/dev/null 2>&1 || true
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"
docker buildx imagetools create --tag "$IMAGE" "$GHCR_IMAGE"

# ---- SQS queue ------------------------------------------------------------
QUEUE_URL="$(aws sqs create-queue --queue-name "$QUEUE" --region "$REGION" \
  --query QueueUrl --output text)"

# ---- App Runner service ---------------------------------------------------
cat > /tmp/apprunner-src.json <<JSON
{
  "ImageRepository": {
    "ImageIdentifier": "$IMAGE",
    "ImageRepositoryType": "ECR",
    "ImageConfiguration": {
      "Port": "8080",
      "RuntimeEnvironmentVariables": {
        "QUEUE_BACKEND": "sqs",
        "SQS_QUEUE_URL": "$QUEUE_URL",
        "AWS_REGION": "$REGION",
        "COM_SHARED_SECRET": "$SECRET",
        "SHARED_SECRET_HEADER": "$SHARED_SECRET_HEADER"
      }
    }
  },
  "AuthenticationConfiguration": { "AccessRoleArn": "$ACCESS_ROLE_ARN" },
  "AutoDeploymentsEnabled": false
}
JSON

SERVICE_ARN="$(aws apprunner create-service \
  --service-name "$APP_NAME" \
  --region "$REGION" \
  --source-configuration file:///tmp/apprunner-src.json \
  --instance-configuration "InstanceRoleArn=$INSTANCE_ROLE_ARN" \
  --query Service.ServiceArn --output text)"

echo ">> Waiting for App Runner service to become RUNNING..."
aws apprunner wait service-running --service-arn "$SERVICE_ARN" --region "$REGION" 2>/dev/null || true

FQDN="$(aws apprunner describe-service --service-arn "$SERVICE_ARN" --region "$REGION" \
  --query Service.ServiceUrl --output text)"

echo
echo "==================================================================="
echo " Relay deployed."
echo " Webhook URL:  https://$FQDN/com/webhook"
echo " Shared secret ($SHARED_SECRET_HEADER): $SECRET"
echo " SQS queue URL: $QUEUE_URL"
echo "==================================================================="
echo " Configure the COM webhook with the URL and the shared-secret header."
