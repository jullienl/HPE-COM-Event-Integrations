#!/usr/bin/env bash
#
# Deploy the COM cloud relay to Azure Container Apps (managed public HTTPS + TLS).
# Provisions Service Bus + queue + send policy, stores the shared secret, and
# runs the relay container. Prints the webhook URL at the end.
#
# Prerequrisites: az CLI logged in (az login), an active subscription selected.
# Usage: ./deploy-relay-azure.sh
#
set -euo pipefail

# ---- Config (override via env before running) -----------------------------
RG="${RG:-rg-com-relay}"
LOC="${LOC:-westeurope}"
SB_NS="${SB_NS:-sbcomrelay$RANDOM}"
QUEUE="${QUEUE:-com-events}"
ACA_ENV="${ACA_ENV:-aca-com-relay}"
APP_NAME="${APP_NAME:-com-event-relay}"
IMAGE="${IMAGE:-ghcr.io/jullienl/com-event-relay:latest}"
SHARED_SECRET_HEADER="${SHARED_SECRET_HEADER:-x-shim-secret}"
SECRET="${SECRET:-$(openssl rand -hex 32)}"

echo ">> Using RG=$RG LOC=$LOC SB_NS=$SB_NS QUEUE=$QUEUE APP=$APP_NAME"

# ---- Resource group -------------------------------------------------------
az group create --name "$RG" --location "$LOC" -o none

# ---- Service Bus + queue + send-only policy -------------------------------
az servicebus namespace create --resource-group "$RG" --name "$SB_NS" \
  --location "$LOC" --sku Standard -o none
az servicebus queue create --resource-group "$RG" --namespace-name "$SB_NS" \
  --name "$QUEUE" -o none
az servicebus queue authorization-rule create --resource-group "$RG" \
  --namespace-name "$SB_NS" --queue-name "$QUEUE" --name relay-send --rights Send -o none

SB_SEND_CONN="$(az servicebus queue authorization-rule keys list \
  --resource-group "$RG" --namespace-name "$SB_NS" --queue-name "$QUEUE" \
  --name relay-send --query primaryConnectionString -o tsv)"

# ---- Container Apps environment + app -------------------------------------
az extension add --name containerapp --upgrade -o none 2>/dev/null || true
az containerapp env create --resource-group "$RG" --name "$ACA_ENV" \
  --location "$LOC" -o none

az containerapp create \
  --resource-group "$RG" --name "$APP_NAME" --environment "$ACA_ENV" \
  --image "$IMAGE" \
  --ingress external --target-port 8080 \
  --secrets "sb-conn=$SB_SEND_CONN" "com-secret=$SECRET" \
  --env-vars \
    "QUEUE_BACKEND=servicebus" \
    "SERVICE_BUS_CONNECTION=secretref:sb-conn" \
    "COM_SHARED_SECRET=secretref:com-secret" \
    "SHARED_SECRET_HEADER=$SHARED_SECRET_HEADER" \
    "QUEUE_NAME=$QUEUE" \
  -o none

FQDN="$(az containerapp show --resource-group "$RG" --name "$APP_NAME" \
  --query properties.configuration.ingress.fqdn -o tsv)"

echo
echo "==================================================================="
echo " Relay deployed."
echo " Webhook URL:  https://$FQDN/com/webhook"
echo " Shared secret ($SHARED_SECRET_HEADER): $SECRET"
echo " Service Bus:  $SB_NS / queue '$QUEUE'"
echo "==================================================================="
echo " Configure the COM webhook with the URL and the shared-secret header."
