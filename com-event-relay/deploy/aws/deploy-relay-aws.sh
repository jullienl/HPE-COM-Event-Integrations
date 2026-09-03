#!/usr/bin/env bash
#
# Deploy the COM cloud relay to AWS App Runner (managed public HTTPS + TLS).
# Provisions an SQS queue, then deploys the relay container pointed at it.
# Prints the webhook URL at the end.
#
# Prerequisites: AWS CLI configured (aws configure), an ECR/public image, and
# an IAM role for App Runner allowing sqs:SendMessage to the queue.
# Usage: ./deploy-relay-aws.sh
#
set -euo pipefail

# ---- Config (override via env before running) -----------------------------
REGION="${AWS_REGION:-eu-west-1}"
QUEUE="${QUEUE:-com-events}"
APP_NAME="${APP_NAME:-com-event-relay}"
IMAGE="${IMAGE:-public.ecr.aws/jullienl/com-event-relay:latest}"
SHARED_SECRET_HEADER="${SHARED_SECRET_HEADER:-x-shim-secret}"
SECRET="${SECRET:-$(openssl rand -hex 32)}"
INSTANCE_ROLE_ARN="${INSTANCE_ROLE_ARN:?Set INSTANCE_ROLE_ARN to an IAM role ARN allowing sqs:SendMessage}"

echo ">> Using REGION=$REGION QUEUE=$QUEUE APP=$APP_NAME"

# ---- SQS queue ------------------------------------------------------------
QUEUE_URL="$(aws sqs create-queue --queue-name "$QUEUE" --region "$REGION" \
  --query QueueUrl --output text)"

# ---- App Runner service ---------------------------------------------------
# Note: for private images use an access role; this assumes a public image.
cat > /tmp/apprunner-src.json <<JSON
{
  "ImageRepository": {
    "ImageIdentifier": "$IMAGE",
    "ImageRepositoryType": "ECR_PUBLIC",
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
