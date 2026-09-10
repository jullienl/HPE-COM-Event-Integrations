# Deploy the relay on AWS + run the on-prem shim — end-to-end runbook (GitHub Issues target)

A step-by-step guide to stand up the **cloud relay + on-prem shim** on
AWS and take it for a first real-world spin using the **GitHub Issues** adapter:
a COM *server health CRITICAL* opens an issue, and the matching *recovery* closes
it.

This is the AWS counterpart of
[Deploy-End-to-End-to-Azure.md](Deploy-End-to-End-to-Azure.md) — same shape,
AWS services (App Runner + SQS) instead of Azure (Container Apps + Service Bus).

```
COM ──webhook──►  [ RELAY on AWS App Runner ]──►  Amazon SQS queue
(cloud, public)      GET handshake + x-shim-secret        │
                                                          │ (outbound-only)
                              [ SHIM near you ]──drains────┘
                                     │
                                     └──►  GitHub Issues  (open on raise / close on clear)
```

**What you deploy where**

| Piece | Where | Image | Role |
|-------|-------|-------|------|
| Relay | AWS App Runner (public HTTPS) | your private **ECR** repo (mirrored from `ghcr.io/jullienl/com-event-relay`) | Answers COM handshake, validates the shared secret, enqueues events. |
| Queue | Amazon SQS | — | Durable buffer between receive and deliver. |
| Shim | Anywhere with **outbound** internet (your laptop/VM/on-prem) | `ghcr.io/jullienl/com-event-shim` | Drains the queue, forwards to GitHub. **No inbound ports.** |

> **Why mirror the relay image into ECR?** CI publishes the relay to **GHCR**
> (`ghcr.io/jullienl/com-event-relay`), but **App Runner can only pull from ECR /
> ECR Public** — it cannot pull from GHCR. So step 5 copies the published GHCR
> image into a private **ECR** repo in your account **once**, and App Runner pulls
> it from there. The **shim** has no such limit: plain `docker run` pulls the
> GHCR image directly.

> **IAM instead of connection strings.** Unlike Azure's send/listen SAS keys, SQS
> access is granted by **IAM**: the App Runner relay assumes an **instance role**
> allowed only `sqs:SendMessage`, and the shim uses an identity allowed only
> `sqs:ReceiveMessage`/`DeleteMessage`/`ChangeMessageVisibility`. That's the same
> least-privilege split, expressed the AWS way.

---

## Contents

- [0. Prerequisites](#0-prerequisites)
- [1. Prepare the target application](#1-prepare-the-target-application)
- [2. Set your working variables, then choose how to deploy](#2-set-your-working-variables-then-choose-how-to-deploy)
- [3. Verify the relay before wiring COM](#3-verify-the-relay-before-wiring-com)
- [4. Configure the COM webhook](#4-configure-the-com-webhook)
- [5. Create the shim's IAM identity (receive-only) and run it](#5-create-the-shims-iam-identity-receive-only-and-run-it)
- [6. End-to-end test](#6-end-to-end-test)
- [7. Troubleshooting](#7-troubleshooting)
- [8. Tear down](#8-tear-down)

---

## 0. Prerequisites

- **This repo cloned locally** — needed **only** for the deploy **script**
  (Option A), the **synthetic-event test** (step 6, Path B), or running the shim
  **directly with Python** (step 5). The **Docker** shim and the manual
  walkthrough with a **real COM event** don't need it (they pull the published
  image and read env vars only):
  ```powershell
  git clone https://github.com/jullienl/HPE-COM-Event-Integrations.git
  cd HPE-COM-Event-Integrations
  ```
- **AWS CLI v2 installed**, then configured for the target account. If `aws` isn't
  already on the machine, install it first (see
  [Install the AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)):
  - **Windows:** `winget install -e --id Amazon.AWSCLI` (or the MSI from the link).
  - **macOS:** `brew install awscli` (or the official `.pkg` installer).
  - **Linux:** `curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip && unzip awscliv2.zip && sudo ./aws/install`.

  Then configure credentials and confirm the account:
  ```powershell
  aws configure          # or: aws sso login
  aws sts get-caller-identity --query "{acct:Account, arn:Arn}" --output table
  ```
- Permission to create **SQS queues**, **IAM roles/users**, and an **ECR repository** (admin or equivalent).
- **Docker** (with Buildx — bundled with Docker Desktop) on the machine you run this from: it's needed to **mirror the relay image into ECR** (step 3.1) and to run the **shim**.
- A **GitHub repository** you can create issues in (a throwaway repo is ideal).
- Rights to create a **GitHub Personal Access Token** (see step 1).
- Access to configure a **COM webhook** in the HPE GreenLake / Compute Ops Management console.

> Region note: this runbook uses `eu-west-1`. Override `$REGION` if you prefer another region (App Runner is not available in every region — check availability first).

---

## 1. Prepare the target application

This runbook uses **GitHub Issues** as the example target, so the steps below
create a repository and a token for it. **Any other target** (ServiceNow, Slack,
Jira, a generic webhook, …) works the same way — only the credential differs:
follow **that application's own documentation** to generate the API token / key /
webhook URL it needs, then pass it to the shim via that adapter's env vars (see
[shim/.env.example](../shim/.env.example) for every adapter's variables).

For the GitHub example:

1. Create (or pick) a repository, e.g. `your-org/com-issues`.
2. Create a **fine-grained PAT**: GitHub → *Settings → Developer settings →
   Personal access tokens → Fine-grained tokens → Generate new token*.
   - **Repository access:** *Only select repositories* → pick your repo.
   - **Permissions → Repository permissions → Issues: Read and write.**
   - (Classic PAT alternative: the `repo` scope also works.)
3. Copy the token — you'll pass it to the **shim** later (step 5) as `GITHUB_TOKEN`
   (nothing GitHub-related is configured on the relay; the relay never talks to GitHub).

The GitHub adapter reads: `GITHUB_REPO` (required, `owner/repo`), `GITHUB_TOKEN`
(required), and optionally `GITHUB_API_URL` (GitHub Enterprise Server) and
`GITHUB_LABELS` (extra labels on new issues).

---

## 2. Set your working variables, then choose how to deploy

First set the variables every later step **and** the deploy script reuse — run
these in a **PowerShell** terminal (locally, or the **PowerShell** option in AWS CloudShell):

```powershell
$REGION = "eu-west-1"                                                # AWS region to deploy into (App Runner isn't in every region — check first)
$QUEUE  = "com-events"                                               # SQS queue name; the relay and shim must both use this value
$APP    = "com-event-relay"                                          # App Runner service name for the relay
$HDR    = "x-shim-secret"                                            # HTTP header COM sends carrying the shared secret (auth on every POST)

$ACCOUNT    = aws sts get-caller-identity --query Account --output text                    # your 12-digit AWS account ID
$GHCR_IMAGE = "ghcr.io/jullienl/com-event-relay:latest"               # upstream image published by CI (App Runner can't pull this directly)
$ECR_REPO   = "com-event-relay"                                       # your private ECR repo name (created in step 3.1)
$IMAGE      = "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO}:latest"   # the ECR image App Runner actually pulls (mirror target)

# 32-byte (64 hex char) shared secret, no openssl needed on Windows:
$SECRET = -join ((1..32) | ForEach-Object { '{0:x2}' -f (Get-Random -Maximum 256) })
$SECRET   # copy this — COM will send it on every POST
```

> Keep `$SECRET` safe. You'll paste it into the COM webhook definition in step 4.

Now provision the relay **one of two ways** — the COM/target wiring afterwards
(steps 3–6) is identical either way:

### Option A — Scripted (fastest)

The script mirrors the image + creates the queue + service, but **not** the IAM
roles. So first create those two roles from **Option B** below —
**step 2 (instance role)** and **step 3.2 (ECR access role)**. The instance-role
policy asks for `$QUEUE_ARN`; the script creates the queue itself, so use its
predictable ARN:

```powershell
$QUEUE_ARN = "arn:aws:sqs:${REGION}:${ACCOUNT}:${QUEUE}"   # queue doesn't exist yet — the script creates it
```

Then from **AWS CloudShell** or **WSL/Git Bash** on Windows, in your clone:

```bash
cd com-event-relay/deploy/aws

# Run it — pass your ECR image URI + the two role ARNs (Option B steps 2 and 3.2)
IMAGE=<acct>.dkr.ecr.<region>.amazonaws.com/com-event-relay:latest \
INSTANCE_ROLE_ARN=<role-from-Option-B-step-2> \
ACCESS_ROLE_ARN=<role-from-Option-B-step-3.2> \
AWS_REGION=eu-west-1 \
bash deploy-relay-aws.sh
```

It needs **Docker** running (for the mirror) and does **not** create a DLQ — add
Option B step 1's redrive policy if you want one. It prints the **Webhook URL** and
generated **shared secret** at the end (copy both for COM in step 4). Then jump to
**step 3 (verify)**.

### Option B — Manual walkthrough (recommended for a first deploy)

Run the sub-steps below by hand to understand each resource, then continue to
**step 3 (verify)**.

#### 1. Create the SQS queue (+ an optional dead-letter queue)

```powershell
# Main queue — the durable buffer between relay (send) and shim (receive)
$QUEUE_URL = aws sqs create-queue --queue-name $QUEUE --region $REGION `
  --query QueueUrl --output text

# (Recommended) a dead-letter queue for poison messages, wired via a redrive policy
$DLQ_URL = aws sqs create-queue --queue-name "$QUEUE-dlq" --region $REGION `
  --query QueueUrl --output text
# The DLQ's ARN — SQS identifies the redrive target by ARN, not URL
$DLQ_ARN = aws sqs get-queue-attributes --queue-url $DLQ_URL `
  --attribute-names QueueArn --region $REGION --query "Attributes.QueueArn" --output text

# Redrive policy: after maxReceiveCount failed receives, move the message to the DLQ
$redrive = (@{ deadLetterTargetArn = $DLQ_ARN; maxReceiveCount = "5" } | ConvertTo-Json -Compress)
# Attach the redrive policy to the main queue
aws sqs set-queue-attributes --queue-url $QUEUE_URL --region $REGION `
  --attributes "RedrivePolicy=$redrive"

# Capture the main queue's ARN — the IAM policies below scope permissions to this exact queue
$QUEUE_ARN = aws sqs get-queue-attributes --queue-url $QUEUE_URL `
  --attribute-names QueueArn --region $REGION --query "Attributes.QueueArn" --output text

"Queue URL : $QUEUE_URL"   # used by relay/shim as SQS_QUEUE_URL (the endpoint to send/receive)
"Queue ARN : $QUEUE_ARN"   # used in the IAM role policies (which resource the role may act on)
```

> The shim maps malformed JSON to `dead_letter`, and SQS moves a message to the
> DLQ after `maxReceiveCount` failed receives. Without a redrive policy those
> messages are dropped instead of captured — hence the DLQ above.

#### 2. Create the IAM instance role for the relay (send-only)

App Runner runs the relay **as** this role; it grants only `sqs:SendMessage` (plus
a cheap `GetQueueAttributes` used by `/readyz`).

```powershell
# Trust policy: who may assume this role — here, App Runner's task runtime
@'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "tasks.apprunner.amazonaws.com" },
    "Action": "sts:AssumeRole"
  }]
}
'@ | Set-Content -Encoding ascii apprunner-trust.json

# Create the (empty) role with that trust policy
aws iam create-role --role-name com-relay-apprunner `
  --assume-role-policy-document file://apprunner-trust.json | Out-Null

# Permissions policy: allow send + a cheap attribute read, on THIS queue only
@"
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["sqs:SendMessage", "sqs:GetQueueAttributes"],
    "Resource": "$QUEUE_ARN"
  }]
}
"@ | Set-Content -Encoding ascii apprunner-send.json

# Attach the permissions policy to the role (inline policy named 'sqs-send')
aws iam put-role-policy --role-name com-relay-apprunner `
  --policy-name sqs-send --policy-document file://apprunner-send.json

# Capture the role ARN — passed as InstanceRoleArn when creating the service (step 3.3)
$ROLE_ARN = aws iam get-role --role-name com-relay-apprunner --query Role.Arn --output text
"Instance role ARN : $ROLE_ARN"
```

#### 3. Deploy the relay to App Runner

##### 3.1 Mirror the published image into your private ECR

App Runner can pull only from ECR / ECR Public (never GHCR), so copy the
CI-published GHCR image into a private ECR repo in your account. `buildx
imagetools create` copies the **full multi-arch manifest** directly (no local
pull, so architecture is always correct):

```powershell
# Create the ECR repo (ignore the error if it already exists)
aws ecr create-repository --repository-name $ECR_REPO --region $REGION 2>$null | Out-Null

# Log Docker in to your ECR registry
aws ecr get-login-password --region $REGION | `
  docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"

# Copy GHCR -> ECR (the GHCR image is public, so no GHCR login is needed)
docker buildx imagetools create --tag $IMAGE $GHCR_IMAGE
```

##### 3.2 Create the App Runner ECR access role

App Runner assumes this role to **pull** from your private ECR (distinct from the
instance role in step 2, which the running relay uses to send to SQS):

```powershell
# Trust policy: App Runner's BUILD/pull runtime may assume this role (note: build.apprunner, not tasks.apprunner)
@'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "build.apprunner.amazonaws.com" },
    "Action": "sts:AssumeRole"
  }]
}
'@ | Set-Content -Encoding ascii apprunner-ecr-trust.json

# Create the role with that trust policy
aws iam create-role --role-name com-relay-ecr-access `
  --assume-role-policy-document file://apprunner-ecr-trust.json 2>$null | Out-Null

# Attach the AWS-managed policy that grants ECR pull permissions
aws iam attach-role-policy --role-name com-relay-ecr-access `
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess

# Capture the role ARN — passed as AccessRoleArn in the source config (step 3.3)
$ACCESS_ROLE_ARN = aws iam get-role --role-name com-relay-ecr-access --query Role.Arn --output text
"ECR access role ARN : $ACCESS_ROLE_ARN"
```

##### 3.3 Create the App Runner service

```powershell
# Source configuration: private ECR image + access role + env vars (the relay reads these)
@"
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
        "SHARED_SECRET_HEADER": "$HDR"
      }
    }
  },
  "AuthenticationConfiguration": { "AccessRoleArn": "$ACCESS_ROLE_ARN" },
  "AutoDeploymentsEnabled": false
}
"@ | Set-Content -Encoding ascii apprunner-src.json

# Create the App Runner service:
#   --source-configuration : the image + env config written above
#   --instance-configuration: role the RUNNING relay uses (SQS send, from step 2)
#   --health-check         : App Runner probes /healthz to decide the service is healthy
$SERVICE_ARN = aws apprunner create-service `
  --service-name $APP --region $REGION `
  --source-configuration file://apprunner-src.json `
  --instance-configuration "InstanceRoleArn=$ROLE_ARN" `
  --health-check-configuration "Protocol=HTTP,Path=/healthz,Interval=10,Timeout=5,HealthyThreshold=1,UnhealthyThreshold=5" `
  --query Service.ServiceArn --output text

# Wait until it's RUNNING (a few minutes)
aws apprunner wait service-running --service-arn $SERVICE_ARN --region $REGION

# Fetch the public hostname App Runner assigned to the service
$FQDN = aws apprunner describe-service --service-arn $SERVICE_ARN --region $REGION `
  --query Service.ServiceUrl --output text

"Webhook URL : https://$FQDN/com/webhook"   # give this URL to COM (step 4)
"Secret hdr  : $HDR = $SECRET"               # COM sends this header/value on every POST
```

> App Runner keeps **at least one provisioned instance** and terminates TLS for
> you, so the single COM POST (COM never retries) is always answered fast — no
> scale-to-zero cold-start to worry about.

**Updating the relay later.** When CI publishes a new GHCR image, re-run the
mirror (step 3.1) to copy `:latest` into ECR, then trigger a fresh App Runner
deployment:
`aws apprunner start-deployment --service-arn $SERVICE_ARN --region $REGION`.

**Shortcut (bash):** prefer not to run these steps by hand? Use the
[deploy-relay-aws.sh](../deploy/aws/deploy-relay-aws.sh) script from
[Option A](#option-a--scripted-fastest) in step 2.

---

## 3. Verify the relay before wiring COM

Run these from the **same PowerShell terminal** you used in step 2 — they reuse
`$FQDN`, `$HDR` and `$SECRET` from there. If you deployed via **Option A** (the
script) or opened a fresh terminal, set them first from the values the script /
step 2 printed:

```powershell
$FQDN   = "<the App Runner URL host from step 2>"  # e.g. xxxxxxxx.eu-west-1.awsapprunner.com
$HDR    = "x-shim-secret"                          # the header name COM sends
$SECRET = "<the shared secret from step 2>"        # the 64-hex secret printed at deploy time
```

> **`curl.exe` vs `curl`:** the commands below use `curl.exe`, which is correct on
> **Windows PowerShell** (there plain `curl` is an alias for `Invoke-WebRequest`).
> In **AWS CloudShell** the shell runs on **Linux**, so use plain **`curl`**
> (drop the `.exe`) — `curl.exe` won't be found there.

**Liveness / readiness** (readiness returns `503` until the queue is reachable):

```powershell
curl.exe -s "https://$FQDN/healthz"   # should return {"status":"ok"}
curl.exe -s "https://$FQDN/readyz"    # should return {"status":"ready"}
```

**Handshake** — COM proves it owns the endpoint via a `GET` with a challenge
header; the relay echoes it back as `{"verification": "<token>"}`:

```powershell
curl.exe -s "https://$FQDN/com/webhook" `
  -H "x-compute-ops-mgmt-verification-challenge: hello123"
# → {"verification": "hello123"}
```

**Auth check** — a POST without the secret must be rejected `401`; with the
correct header it returns `202` and lands a message on the queue:

```powershell
# Expect 401 (no secret)
curl.exe -s -o NUL -w "%{http_code}`n" -X POST "https://$FQDN/com/webhook" `
  -H "content-type: application/json" -d '{}'

# Expect 202 (valid secret) — enqueues a (minimal) test message
curl.exe -s -o NUL -w "%{http_code}`n" -X POST "https://$FQDN/com/webhook" `
  -H "content-type: application/json" -H "$HDR`: $SECRET" -d '{\"id\":\"ping\"}'
```

> The relay validates the secret with a constant-time compare and caps the body
> at `MAX_BODY_BYTES` (default 256 KB → `413` if exceeded).

---

## 4. Configure the COM webhook

> **Webhooks are created via the COM API, not the console UI.** The GreenLake /
> Compute Ops Management console does **not** expose webhook creation today, so
> you register the webhook with a `POST` to the COM webhooks API. The easiest way
> is the ready-made **"Create webhook"** requests in my public Postman collection:
>
> [Lionel Jullien's public workspace → HPE Compute Ops Management (v2) → Webhooks](https://www.postman.com/jullienl/lionel-jullien-s-public-workspace/)
>
> Fork/import that collection, set your COM API token, and run one of the
> **Create webhook** calls with the values below.
>
> **New to COM's API or Postman?** The collection's **Overview** page includes an
> **initial setup guide** that walks you through creating an HPE GreenLake API
> client, getting an access token, and configuring the Postman environment — do
> that first, then come back and run the **Create webhook** call.
>
> For a deeper walkthrough of COM webhooks — how to create one, the available
> event **filter** options, and the resources they cover — see the blog post
> [Implementing webhooks with COM](https://jullienl.github.io/Implementing-webhooks-with-COM/).

Register a webhook with these settings:

- **Destination URL:** `https://<FQDN>/com/webhook`  (from step 2)
- **Custom header:** name `x-shim-secret` (your `$HDR`), value = `$SECRET`.
  COM authenticates with a **static header only** — this is that header.
- **Event filter (`eventFilter`):** scope it to servers so only relevant events
  flow, e.g. server health changes. Filtering happens **server-side at COM**; the
  relay forwards whatever COM sends. **The resource type you filter on must match
  the shim's `SERVER_MONITORS` (step 5):** this runbook watches the **server**
  resource, so filter on server events. If you filter on a different resource
  type, the shim's server-condition logic won't apply.

A typical **Create webhook** call looks like this (replace the destination host
with your relay `<FQDN>` from step 2 and the secret with `$SECRET` from step 2):

```http
POST https://<COM-API-base-URL>/compute-ops-mgmt/<webhooks-API-version>/webhooks
```
```json
{
    "name": "AWS Relay - Webhook event for servers that get unhealthy",
    "destination": "https://xxxxxxxxxxxx.eu-west-1.awsapprunner.com/com/webhook",
    "state": "ENABLED",
    "eventFilter": "type eq 'compute-ops/server' and old/hardware/health/summary eq 'OK' and changed/hardware/health/summary eq True",
    "headers": {
        "x-shim-secret": "<the shared secret from step 2>"
    }
}
```

**Create a second webhook for the recovery (clear).** A COM webhook only fires for
the transition its `eventFilter` selects, so the one above delivers *only* the
**raise** (health left `OK`). To have the shim **close** the GitHub issue when the
server becomes healthy again, register a **second** webhook pointing at the **same**
relay URL with the same secret header, filtering on the **opposite** transition
(health returned to `OK`):

```http
POST https://<COM-API-base-URL>/compute-ops-mgmt/<webhooks-API-version>/webhooks
```
```json
{
    "name": "AWS Relay - Webhook event for servers that recover",
    "destination": "https://xxxxxxxxxxxx.eu-west-1.awsapprunner.com/com/webhook",
    "state": "ENABLED",
    "eventFilter": "type eq 'compute-ops/server' and new/hardware/health/summary eq 'OK' and changed/hardware/health/summary eq True",
    "headers": {
        "x-shim-secret": "<the shared secret from step 2>"
    }
}
```

The difference is `old/...` (raise: was `OK`, so it **left** `OK`) vs `new/...`
(clear: is now `OK`, so it **returned** to `OK`). Both events carry the same
`correlation_key` (`server:<serial>:health`), so the clear closes exactly the
issue the raise opened. **Without the clear webhook, issues open but never close.**

COM will first call `GET` (the handshake in step 3) and only enable the webhook
once it echoes the challenge over public HTTPS with a valid certificate — App
Runner provides that TLS automatically.

**Verify the webhook was created and enabled.** In the same Postman collection,
run the **Get webhooks** call (or `GET https://<COM-API-base-URL>/compute-ops-mgmt/<webhooks-API-version>/webhooks`).
Your webhook should report:

```json
"state": "ENABLED",
"status": "ACTIVE"
```

`ENABLED` means the handshake succeeded; `ACTIVE` means COM will deliver events
to it. If you instead see `DISABLED` / `ERROR`, the handshake or recent deliveries
failed — recheck the destination URL, the certificate, and the relay health
(step 3), then re-run the create/enable call.

> Keep the webhook **healthy**: COM disables a webhook after **10 consecutive
> non-2xx** responses. The relay returns `202` as soon as the event is queued, so
> a slow/broken GitHub target never affects webhook health — that decoupling is
> the whole point of the queue.

> **Multiple webhooks, one relay/target.** The relay exposes a single URL and just
> **enqueues whatever COM sends** — it doesn't care about resource type. So you can
> register **several webhooks, each with a different `eventFilter`/resource type**
> (e.g. one on `server`, one on `alert`), **all pointing at this same relay URL**
> with the same secret header. They funnel into the same queue → same shim → same
> target. The shim's normaliser dispatches on each payload's `type`
> (`.../server` → server conditions, `.../alert` → alert, anything else → a
> generic mapping — nothing is dropped). (The shim's `SERVER_MONITORS` setting,
> introduced in step 5, only tunes the `server` branch — an `alert` webhook is
> unaffected by it.)

---

## 5. Create the shim's IAM identity (receive-only) and run it

The shim uses the **default boto3 credential chain**. On ECS/EC2 give it a
**task/instance role**; for a laptop test, create a small **IAM user** limited to
consuming this one queue.

First create the receive-only identity (used by both the test and production
runs below):

> **Egress firewall ports.** The shim opens only these **outbound** connections
> (no inbound rule is ever needed):
>
> | Destination | Protocol | Port |
> |-------------|----------|------|
> | Amazon SQS (`sqs.<region>.amazonaws.com`) | HTTPS | **443** |
> | GitHub API (`api.github.com`) | HTTPS | **443** |
>
> SQS is a plain HTTPS (REST) API, so the shim only needs **outbound 443** — to
> the regional SQS endpoint and to your target (GitHub here, or any other adapter).

```powershell
# Least-privilege consume policy for the one queue
@"
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:GetQueueAttributes"
    ],
    "Resource": "$QUEUE_ARN"
  }]
}
"@ | Set-Content -Encoding ascii shim-consume.json

aws iam create-user --user-name com-shim | Out-Null
aws iam put-user-policy --user-name com-shim --policy-name sqs-consume `
  --policy-document file://shim-consume.json

$KEY = aws iam create-access-key --user-name com-shim `
  --query "AccessKey.{id:AccessKeyId, secret:SecretAccessKey}" --output json | ConvertFrom-Json
$AWS_ID     = $KEY.id
$AWS_SECRET = $KEY.secret
"Access key created for com-shim (store securely)."
```

Then run the shim. Two ways, depending on your goal:

- **[5a — Test run](#5a--test-run-laptop)** — a quick, throwaway `docker run` on
  your laptop to validate the end-to-end flow.
- **[5b — Production run](#5b--production-run)** — a long-lived, auto-restarting
  workload with a persistent de-dup volume.

Both use the **same image and the same env vars** (listed in
[Env vars reference](#env-vars-reference) below); they differ only in how the
container is launched and where state lives.

### 5a — Test run (laptop)

> **Prerequisite.** **Docker Desktop** running, in **Linux containers** mode —
> verify with `docker version` (a **Server** section must print; if only the
> Client shows, the daemon isn't started). Start Docker Desktop and wait for the
> tray whale to go steady before `docker run`.
>
> **No Docker?** Run it straight with Python from your clone instead:
> `cd com-event-relay/shim` → `pip install -r requirements.txt` →
> `pip install ../../com-event-core` → set the env vars → `python worker.py`.

Needs **outbound** internet only — the queue URL, region, the receive-only
credentials, and your GitHub details. `--rm` throws the container away on stop
and there's **no volume**, so the de-dup store is ephemeral — fine for a test:

```powershell
docker run --rm --name com-event-shim `
  -e QUEUE_BACKEND=sqs `
  -e "SQS_QUEUE_URL=$QUEUE_URL" `
  -e "AWS_REGION=$REGION" `
  -e "AWS_ACCESS_KEY_ID=$AWS_ID" `
  -e "AWS_SECRET_ACCESS_KEY=$AWS_SECRET" `
  -e TARGETS=github `
  -e GITHUB_REPO=your-org/com-issues `
  -e GITHUB_TOKEN=<your-fine-grained-PAT> `
  -e SERVER_MONITORS=health `
  ghcr.io/jullienl/com-event-shim:latest
```

The shim logs each message it drains, the events it normalises, and the forward
result. Leave it running for the end-to-end test (step 6).

> **Lost your variables (new shell / lost AWS CLI)?** The queue and IAM user still
> exist — re-fetch the queue URL/ARN (they're persistent). After `aws configure`:
> ```powershell
> $REGION    = "eu-west-1"
> $QUEUE     = "com-events"
> $QUEUE_URL = aws sqs get-queue-url --queue-name $QUEUE --region $REGION --query QueueUrl -o text
> $QUEUE_ARN = aws sqs get-queue-attributes --queue-url $QUEUE_URL --region $REGION `
>   --attribute-names QueueArn --query "Attributes.QueueArn" --output text
> ```
> But the IAM **secret access key is shown only once at creation** and cannot be
> retrieved. If you lost it, mint a new one (delete the old key first to stay
> within the 2-key limit):
> ```powershell
> aws iam create-access-key --user-name com-shim `
>   --query "AccessKey.{id:AccessKeyId, secret:SecretAccessKey}" --output json
> ```

### 5b — Production run

Run the shim as a **long-lived workload**. Any of these hosts works — same image,
same env vars:

- **AWS App Runner / ECS / EKS** — and **prefer an ECS/EC2 task/instance role**
  over the static `com-shim` keys (drop `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`
  and let the default credential chain pick up the role).
- **Any on-prem / self-managed Kubernetes cluster** — a `Deployment` that maps the
  env vars below to the container's `env`, with the queue credentials / GitHub
  token in a `Secret`, and a `PersistentVolumeClaim` mounted at `/data` (see
  de-dup persistence below).
- A **systemd** service on a VM.

For a plain Docker host, drop `--rm`, add `--restart unless-stopped`, and mount a
named volume at `/data` so the de-dup store survives restarts/upgrades (Docker
auto-creates the `com-dedup` volume on first use — no pre-create needed):

```powershell
docker run -d --name com-event-shim --restart unless-stopped `
  -v com-dedup:/data `
  -e QUEUE_BACKEND=sqs `
  -e "AWS_ACCESS_KEY_ID=$AWS_ID" `
  -e "AWS_SECRET_ACCESS_KEY=$AWS_SECRET" `
  -e "AWS_REGION=$REGION" `
  -e "SQS_QUEUE_URL=$QUEUE_URL" `
  -e TARGETS=github `
  -e GITHUB_REPO=your-org/com-issues `
  -e GITHUB_TOKEN=<your-fine-grained-PAT> `
  -e SERVER_MONITORS=health `
  ghcr.io/jullienl/com-event-shim:latest
```

> **Credentials hygiene:** injecting keys as env vars is fine for a quick test;
> for anything longer-lived prefer an **IAM role** (ECS task role / EC2 instance
> profile) so there are no static keys. The shim's file-or-env secret helper also
> supports `GITHUB_TOKEN_FILE` if you'd rather mount the PAT than pass it inline.

> **Secrets from files (vault) — recommended for production.** Sensitive values
> the shim resolves through its own secret helper — **`GITHUB_TOKEN` and any
> adapter token** — also accept a **`<NAME>_FILE`** form: point it at a file and
> the shim reads the secret from there (trailing newline stripped) instead of the
> plain env var. File-backed secrets don't leak via `docker inspect`,
> `/proc/<pid>/environ`, or child processes, and any vault projects secrets **as
> files**.
>
> **AWS credentials are the exception — don't use `_FILE` for them.** The SQS
> client uses **boto3's** credential chain, which does *not* understand the `_FILE`
> convention. For AWS access use an **IAM role** (ECS task role / EC2 instance
> profile) and **drop the static keys entirely** — boto3 picks up the role
> automatically:
>
> ```powershell
> # IAM role for AWS (no static keys) + vault-projected GitHub token via *_FILE:
> docker run -d --name com-event-shim --restart unless-stopped `
>   -v com-dedup:/data `
>   -v /run/secrets:/run/secrets:ro `
>   -e QUEUE_BACKEND=sqs `
>   -e "AWS_REGION=$REGION" `
>   -e "SQS_QUEUE_URL=$QUEUE_URL" `
>   -e TARGETS=github `
>   -e GITHUB_REPO=your-org/com-issues `
>   -e GITHUB_TOKEN_FILE=/run/secrets/github-token `
>   -e SERVER_MONITORS=health `
>   ghcr.io/jullienl/com-event-shim:latest
> ```
>
> On **EKS** mount an AWS Secrets Manager secret via the **Secrets Store CSI
> driver** (or a plain `Secret`) for the adapter tokens and set the matching
> `_FILE` vars, and grant AWS access with an **IRSA role** (no keys); on **ECS**
> use a task role plus a secret volume the same way. Full step-by-step wiring
> (incl. systemd `LoadCredential` and Vault Agent) is in the relay README's
> [Secrets management](../README.md#secrets-management).

> **De-dup persistence.** The shim keeps a small SQLite de-dup store at
> `DEDUP_DB_PATH` (the image defaults it to `/data/dedup.db`, a writable dir);
> `/data` is a declared volume. Rows expire after `DEDUP_TTL_SECONDS` (default
> `3600`), so the store stays tiny (kilobytes–megabytes). On **ECS/EKS** back
> `/data` with a volume (EFS mount / `PersistentVolumeClaim`) mounted at `/data`.
> If you **don't** persist `/data`, the only effect of a restart is that the
> in-flight de-dup window is lost — a redelivered event could produce a duplicate
> item until the TTL re-populates; nothing is corrupted.

### Env vars reference

Key env vars (full list in [shim/.env.example](../shim/.env.example)):

| Var | Value here | Notes |
|-----|-----------|-------|
| `QUEUE_BACKEND` | `sqs` | Must match the relay. |
| `SQS_QUEUE_URL` | `$QUEUE_URL` | The main queue (not the DLQ). |
| `AWS_REGION` | `$REGION` | Region of the queue. |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | com-shim keys | Or use an ECS/EC2 role and omit these. |
| `TARGETS` | `github` | One name, or comma-separated to fan out (`github,slack`). |
| `GITHUB_REPO` / `GITHUB_TOKEN` | your repo + PAT | `GITHUB_REPO` is the **`owner/repo` slug only** (e.g. `jullienl/HPE-COM-Event-Integrations-HOL`), **not** a URL. Token needs **Issues: read/write**. |
| `SERVER_MONITORS` | `health` | Watch server health (default). Add more as a **comma-separated** list — `SERVER_MONITORS=health,power,connection,subscription`. Only applies when the webhook's `eventFilter` targets the **server** resource type (step 4). **Each monitor you add needs a matching COM webhook** targeting this same relay (e.g. adding `power` requires a webhook with a **power** `eventFilter` pointing at the same relay URL) — the shim only sees the events COM is configured to send. **A condition you *don't* list is simply not monitored** — no item ever opens or closes for it and no error is raised (e.g. without `power`, a powered-off server never opens an item and powering back on never closes one). |
| `DEDUP_TTL_SECONDS` | `3600` (default) | Suppresses duplicate/redelivered events within the window. |

The shim logs each message it drains, the events it normalises, and the forward
result. Leave it running for the end-to-end test.

---

## 6. End-to-end test

**Path A — real COM event.** Trigger (or wait for) a server health change that
matches your `eventFilter`. Within a few seconds you should see:
1. Relay logs a `202` for the POST.
2. Shim logs the drained message → `forwarded to GitHub`.
3. A new **issue** appears in your repo, labelled `com:server:<serial>:health`.
When the server returns to healthy, COM sends a *clear*; the shim finds the open
issue by that label and **closes** it (with a recovery comment).

**Path B — synthetic event (no waiting).** Post a sample server snapshot straight
at the relay to exercise the whole chain: a `raise` snapshot (unhealthy) opens an
issue, a `clear` snapshot (healthy) closes it. The two fixtures are just **static
JSON** — write them with your shell (no Python needed), then POST them. Pick the
block for your shell.

> **These are `server` *health* payloads — not `alert` payloads.** The fixtures
> use `type: compute-ops-mgmt/server` with a `hardware/health` block, so they
> exercise the **server health** condition. That means this test is only valid
> when the shim runs with `SERVER_MONITORS=health` (step 5) — the default — and
> mirrors a **compute/server** webhook (step 4, `type eq 'compute-ops/server'`),
> **not** an `alert` webhook. An `alert` payload has a completely different shape
> and normalises down a different branch, so it would neither open nor close an
> issue via the health condition. If you changed `SERVER_MONITORS` to something
> without `health`, this snapshot is (correctly) ignored — post a fixture for a
> condition you *do* monitor instead.

**PowerShell (Windows):**

```powershell
# 1. Write the two fixtures — a raise (health CRITICAL) and a clear (health OK).
@'
{
  "type": "compute-ops-mgmt/server",
  "id": "P28948-B21+CZ2311004G",
  "name": "ESX-node-01",
  "operation": "Updated",
  "updatedAt": "2025-01-01T10:00:00Z",
  "hardware": {
    "serialNumber": "CZ2311004G",
    "productId": "P28948-B21",
    "model": "ProLiant DL360 Gen11",
    "bmc": { "ip": "10.0.0.5" },
    "health": { "summary": "CRITICAL", "fans": "OK", "powerSupplies": "CRITICAL", "memory": "OK" }
  }
}
'@ | Set-Content -Encoding ascii raise.json

@'
{
  "type": "compute-ops-mgmt/server",
  "id": "P28948-B21+CZ2311004G",
  "name": "ESX-node-01",
  "operation": "Updated",
  "updatedAt": "2025-01-01T10:00:00Z",
  "hardware": {
    "serialNumber": "CZ2311004G",
    "productId": "P28948-B21",
    "model": "ProLiant DL360 Gen11",
    "bmc": { "ip": "10.0.0.5" },
    "health": { "summary": "OK", "fans": "OK", "powerSupplies": "OK" }
  }
}
'@ | Set-Content -Encoding ascii clear.json

# 2. POST the fixtures at the relay ($FQDN/$HDR/$SECRET are the deploy variables
#    from step 2/3 — re-set them if this is a fresh shell).
curl.exe -s -o NUL -w "%{http_code}`n" -X POST "https://$FQDN/com/webhook" `
  -H "content-type: application/json" -H "$HDR`: $SECRET" --data "@raise.json"   # → issue opens
curl.exe -s -o NUL -w "%{http_code}`n" -X POST "https://$FQDN/com/webhook" `
  -H "content-type: application/json" -H "$HDR`: $SECRET" --data "@clear.json"   # → same issue closes
```

**Linux/macOS:**

```bash
# 1. Write the two fixtures — a raise (health CRITICAL) and a clear (health OK).
cat > raise.json <<'JSON'
{
  "type": "compute-ops-mgmt/server",
  "id": "P28948-B21+CZ2311004G",
  "name": "ESX-node-01",
  "operation": "Updated",
  "updatedAt": "2025-01-01T10:00:00Z",
  "hardware": {
    "serialNumber": "CZ2311004G",
    "productId": "P28948-B21",
    "model": "ProLiant DL360 Gen11",
    "bmc": { "ip": "10.0.0.5" },
    "health": { "summary": "CRITICAL", "fans": "OK", "powerSupplies": "CRITICAL", "memory": "OK" }
  }
}
JSON

cat > clear.json <<'JSON'
{
  "type": "compute-ops-mgmt/server",
  "id": "P28948-B21+CZ2311004G",
  "name": "ESX-node-01",
  "operation": "Updated",
  "updatedAt": "2025-01-01T10:00:00Z",
  "hardware": {
    "serialNumber": "CZ2311004G",
    "productId": "P28948-B21",
    "model": "ProLiant DL360 Gen11",
    "bmc": { "ip": "10.0.0.5" },
    "health": { "summary": "OK", "fans": "OK", "powerSupplies": "OK" }
  }
}
JSON

# 2. POST the fixtures at the relay ($FQDN/$HDR/$SECRET are the deploy variables
#    from step 2/3 — re-set them if this is a fresh shell).
curl -s -o /dev/null -w "%{http_code}\n" -X POST "https://$FQDN/com/webhook" \
  -H "content-type: application/json" -H "$HDR: $SECRET" --data "@raise.json"    # → issue opens
curl -s -o /dev/null -w "%{http_code}\n" -X POST "https://$FQDN/com/webhook" \
  -H "content-type: application/json" -H "$HDR: $SECRET" --data "@clear.json"    # → same issue closes
```

> These fixtures are just static JSON copied from the project's shipped sample
> builder. If you'd rather **generate** them (or see how they *normalise* into
> `CanonicalEvent`s) with Python, run `python com-event-core/examples/dump_payloads.py
> server raise` from a venv (`pip install ./com-event-core`) — it prints the raw
> COM payload plus the resulting events.

**What you should see.** The `raise` opens a GitHub issue and the `clear` closes
the **same** issue:

<a href="../../docs/images/com-event-path-b-github-issue.png"><img src="../../docs/images/com-event-path-b-github-issue.png" alt="Path B result: a GitHub issue titled 'Server ESX-node-01 health CRITICAL', labelled com:server:CZ2311004G:health, opened on the raise and closed as completed with a 'Resolved by COM clear event' comment" width="900" /></a>

The title (**Server ESX-node-01 health CRITICAL**), the
**`com:server:CZ2311004G:health`** label (the correlation key that ties the raise
to the clear), the body listing the non-OK component (`powerSupplies=CRITICAL`),
the **"Resolved by COM clear event"** comment, and the **Closed as completed**
state all come straight from the two fixtures above.

> First *clear* can occasionally race GitHub's search index (~1s lag); if the
> issue isn't found the message is abandoned and redelivered, and the retry
> closes it — no lost events.

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| COM won't enable the webhook | Handshake failed | Confirm `GET /com/webhook` echoes the challenge over **public HTTPS** (step 3). Check the URL has no typo and ends in `/com/webhook`. |
| `curl` to `/healthz` hangs / **stream timeout** / 0 bytes (but TLS connects) | Relay container **crashed on boot** — TCP+TLS reach the service but the app exited before binding `:8080`, so nothing answers | Check the application logs (below): a Python traceback / `ModuleNotFoundError` or a missing required env var means the app never started. Confirm the service `Status` is `RUNNING` and `Port=8080`. Fix the cause, re-mirror the image if needed, then deploy again. |
| Relay returns `401` | Wrong/missing header | Header **name** must equal `SHARED_SECRET_HEADER` (`x-shim-secret`) and value must equal `$SECRET`. |
| Relay returns `413` | Body too large | Raise `MAX_BODY_BYTES` on the relay service if you genuinely send large payloads. |
| Relay returns `503` / `/readyz` fails | Queue unreachable or role missing send | Confirm the instance role has `sqs:SendMessage` on the queue ARN and `SQS_QUEUE_URL`/`AWS_REGION` are correct. |
| App Runner stuck `CREATE_FAILED` | Bad image id, port, or role | Check the ECR image identifier, `Port=8080`, and that the instance role trust policy names `tasks.apprunner.amazonaws.com`. |
| Shim exits immediately | Missing required env | It fails fast if `SQS_QUEUE_URL` is unset (sqs backend). Check the queue URL + credentials. |
| Shim: `AccessDenied` on receive | IAM policy too narrow | The consume policy needs `ReceiveMessage`, `DeleteMessage`, `ChangeMessageVisibility`, `GetQueueAttributes` on the queue ARN. |
| No GitHub issue appears | Token/repo/scope | Verify `GITHUB_REPO=owner/repo` and the PAT has **Issues: read/write** on that repo. Watch the shim logs for the forward error. |
| Issue opens but never closes | Clear not delivered / label mismatch | Confirm a *clear* event actually fired; the shim matches the open issue by its `com:<correlation_key>` label. |
| Webhook shows WARNING/ERROR in COM | Repeated non-2xx from the relay | The relay should return `202` fast; if you see `5xx`, fix the queue/role first — sustained failures **disable** the webhook. |

Handy log/inspection commands:

```powershell
# 1. Is the service actually running? (Status, and the URL host)
aws apprunner describe-service --service-arn $SERVICE_ARN --region $REGION `
  --query "Service.{status:Status, url:ServiceUrl, port:SourceConfiguration.ImageRepository.ImageConfiguration.Port}" --output table

# 2. The real story — the container's own application logs (boot errors / tracebacks)
aws logs tail "/aws/apprunner/$APP/*/application" --region $REGION --since 15m

# 3. System/platform events (image pull, health-check, deployment failures)
aws logs tail "/aws/apprunner/$APP/*/service" --region $REGION --since 15m

# 4. Recent deployment / status transitions
aws apprunner list-operations --service-arn $SERVICE_ARN --region $REGION `
  --query "OperationSummaryList[].{type:Type, status:Status, started:StartedAt}" --output table

# Follow the application logs live
aws logs tail "/aws/apprunner/$APP/*/application" --region $REGION --follow

# Messages waiting / in flight on the queue
aws sqs get-queue-attributes --queue-url $QUEUE_URL --region $REGION `
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible `
  --query Attributes --output table

# Anything captured in the dead-letter queue
aws sqs get-queue-attributes --queue-url $DLQ_URL --region $REGION `
  --attribute-names ApproximateNumberOfMessages --query Attributes --output table
```

---

## 8. Tear down

```powershell
docker rm -f com-event-shim 2>$null

# App Runner service
aws apprunner delete-service --service-arn $SERVICE_ARN --region $REGION | Out-Null

# Queues
aws sqs delete-queue --queue-url $QUEUE_URL --region $REGION
aws sqs delete-queue --queue-url $DLQ_URL  --region $REGION

# IAM (delete inline policies + keys first, then the principals)
aws iam delete-role-policy --role-name com-relay-apprunner --policy-name sqs-send
aws iam delete-role --role-name com-relay-apprunner
aws iam delete-user-policy --user-name com-shim --policy-name sqs-consume
aws iam delete-access-key --user-name com-shim --access-key-id $AWS_ID
aws iam delete-user --user-name com-shim

# Local scratch files
Remove-Item apprunner-trust.json, apprunner-send.json, apprunner-src.json, shim-consume.json, raise.json, clear.json -ErrorAction SilentlyContinue
```

---

### Where this maps in the code

- Relay app + endpoints: [relay/app.py](../relay/app.py) (`/com/webhook`, `/healthz`, `/readyz`)
- Relay config: [relay/.env.example](../relay/.env.example)
- SQS publisher/consumer: [relay/core/queue/sqs.py](../relay/core/queue/sqs.py) · [shim/core/queue/sqs.py](../shim/core/queue/sqs.py)
- Shim loop: [shim/worker.py](../shim/worker.py) · config: [shim/.env.example](../shim/.env.example)
- GitHub adapter: [../../com-event-core/com_event_core/adapters/github.py](../../com-event-core/com_event_core/adapters/github.py)
- AWS provisioning script: [deploy/aws/deploy-relay-aws.sh](../deploy/aws/deploy-relay-aws.sh)
