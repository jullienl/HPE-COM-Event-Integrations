# Deploy the cloud relay to AWS — end-to-end runbook (GitHub Issues target)

A step-by-step guide to stand up the **cloud relay + on-prem shim** on
AWS and take it for a first real-world spin using the **GitHub Issues** adapter:
a COM *server health CRITICAL* opens an issue, and the matching *recovery* closes
it.

This is the AWS counterpart of
[Deploy-Cloud-Relay-to-Azure.md](Deploy-Cloud-Relay-to-Azure.md) — same shape,
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
| Relay | AWS App Runner (public HTTPS) | `public.ecr.aws/jullienl/com-event-relay` | Answers COM handshake, validates the shared secret, enqueues events. |
| Queue | Amazon SQS | — | Durable buffer between receive and deliver. |
| Shim | Anywhere with **outbound** internet (your laptop/VM/on-prem) | `ghcr.io/jullienl/com-event-shim` | Drains the queue, forwards to GitHub. **No inbound ports.** |

> **IAM instead of connection strings.** Unlike Azure's send/listen SAS keys, SQS
> access is granted by **IAM**: the App Runner relay assumes an **instance role**
> allowed only `sqs:SendMessage`, and the shim uses an identity allowed only
> `sqs:ReceiveMessage`/`DeleteMessage`/`ChangeMessageVisibility`. That's the same
> least-privilege split, expressed the AWS way.

---

## 0. Prerequisites

- **AWS CLI v2** configured for the target account:
  ```powershell
  aws configure          # or: aws sso login
  aws sts get-caller-identity --query "{acct:Account, arn:Arn}" --output table
  ```
- Permission to create **SQS queues** and **IAM roles/users** (admin or equivalent).
- **Docker** on the machine that will run the **shim**. The relay needs no local Docker — App Runner pulls its image.
- A **GitHub repository** you can create issues in (a throwaway repo is ideal).
- Rights to create a **GitHub Personal Access Token** (see step 1).
- Access to configure a **COM webhook** in the HPE GreenLake / Compute Ops Management console.

> Region note: this runbook uses `eu-west-1`. Override `$REGION` if you prefer another region (App Runner is not available in every region — check availability first).

---

## 1. Prepare the GitHub target

1. Create (or pick) a repository, e.g. `your-org/com-lab-issues`.
2. Create a **fine-grained PAT**: GitHub → *Settings → Developer settings →
   Personal access tokens → Fine-grained tokens → Generate new token*.
   - **Repository access:** *Only select repositories* → pick your repo.
   - **Permissions → Repository permissions → Issues: Read and write.**
   - (Classic PAT alternative: the `repo` scope also works.)
3. Copy the token — you'll pass it to the **shim** later as `GITHUB_TOKEN`
   (nothing GitHub-related is configured on the relay; the relay never talks to GitHub).

The GitHub adapter reads: `GITHUB_REPO` (required, `owner/repo`), `GITHUB_TOKEN`
(required), and optionally `GITHUB_API_URL` (GitHub Enterprise Server) and
`GITHUB_LABELS` (extra labels on new issues).

---

## 2. Set your working variables (PowerShell)

Run these in the `pwsh` terminal; later steps reuse them.

```powershell
$REGION = "eu-west-1"
$QUEUE  = "com-events"
$APP    = "com-event-relay"
$IMAGE  = "public.ecr.aws/jullienl/com-event-relay:latest"
$HDR    = "x-shim-secret"                                             # shared-secret header name

# 32-byte (64 hex char) shared secret, no openssl needed on Windows:
$SECRET = -join ((1..32) | ForEach-Object { '{0:x2}' -f (Get-Random -Maximum 256) })
$SECRET   # copy this — COM will send it on every POST
```

> Keep `$SECRET` safe. You'll paste it into the COM webhook definition in step 7.

---

## 3. Create the SQS queue (+ an optional dead-letter queue)

```powershell
# Main queue
$QUEUE_URL = aws sqs create-queue --queue-name $QUEUE --region $REGION `
  --query QueueUrl --output text

# (Recommended) a dead-letter queue for poison messages, wired via a redrive policy
$DLQ_URL = aws sqs create-queue --queue-name "$QUEUE-dlq" --region $REGION `
  --query QueueUrl --output text
$DLQ_ARN = aws sqs get-queue-attributes --queue-url $DLQ_URL `
  --attribute-names QueueArn --region $REGION --query "Attributes.QueueArn" --output text

$redrive = (@{ deadLetterTargetArn = $DLQ_ARN; maxReceiveCount = "5" } | ConvertTo-Json -Compress)
aws sqs set-queue-attributes --queue-url $QUEUE_URL --region $REGION `
  --attributes "RedrivePolicy=$redrive"

# Capture the main queue's ARN for the IAM policies below
$QUEUE_ARN = aws sqs get-queue-attributes --queue-url $QUEUE_URL `
  --attribute-names QueueArn --region $REGION --query "Attributes.QueueArn" --output text

"Queue URL : $QUEUE_URL"
"Queue ARN : $QUEUE_ARN"
```

> The shim maps malformed JSON to `dead_letter`, and SQS moves a message to the
> DLQ after `maxReceiveCount` failed receives. Without a redrive policy those
> messages are dropped instead of captured — hence the DLQ above.

---

## 4. Create the IAM instance role for the relay (send-only)

App Runner runs the relay **as** this role; it grants only `sqs:SendMessage` (plus
a cheap `GetQueueAttributes` used by `/readyz`).

```powershell
# Trust policy: App Runner tasks may assume this role
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

aws iam create-role --role-name com-relay-apprunner `
  --assume-role-policy-document file://apprunner-trust.json | Out-Null

# Permissions: send to the one queue only
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

aws iam put-role-policy --role-name com-relay-apprunner `
  --policy-name sqs-send --policy-document file://apprunner-send.json

$ROLE_ARN = aws iam get-role --role-name com-relay-apprunner --query Role.Arn --output text
"Instance role ARN : $ROLE_ARN"
```

---

## 5. Deploy the relay to App Runner

```powershell
# Source configuration: public ECR image + env vars (the relay reads these)
@"
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
        "SHARED_SECRET_HEADER": "$HDR"
      }
    }
  },
  "AutoDeploymentsEnabled": false
}
"@ | Set-Content -Encoding ascii apprunner-src.json

$SERVICE_ARN = aws apprunner create-service `
  --service-name $APP --region $REGION `
  --source-configuration file://apprunner-src.json `
  --instance-configuration "InstanceRoleArn=$ROLE_ARN" `
  --health-check-configuration "Protocol=HTTP,Path=/healthz,Interval=10,Timeout=5,HealthyThreshold=1,UnhealthyThreshold=5" `
  --query Service.ServiceArn --output text

# Wait until it's RUNNING (a few minutes)
aws apprunner wait service-running --service-arn $SERVICE_ARN --region $REGION

$FQDN = aws apprunner describe-service --service-arn $SERVICE_ARN --region $REGION `
  --query Service.ServiceUrl --output text

"Webhook URL : https://$FQDN/com/webhook"
"Secret hdr  : $HDR = $SECRET"
```

> App Runner keeps **at least one provisioned instance** and terminates TLS for
> you, so the single COM POST (COM never retries) is always answered fast — no
> scale-to-zero cold-start to worry about.

**Private image?** The `public.ecr.aws/...` image needs no credentials. For a
private ECR repo, add an **access role** (`AuthenticationConfiguration.AccessRoleArn`)
that allows `ecr:GetDownloadUrlForLayer` etc. in the source configuration.

**Shortcut (bash):** the same provisioning is scripted in
[deploy/aws/deploy-relay-aws.sh](../deploy/aws/deploy-relay-aws.sh) — run it from
CloudShell or WSL/Git Bash with `INSTANCE_ROLE_ARN=<role> AWS_REGION=... ./deploy-relay-aws.sh`.
It creates the queue + service but assumes you already made the instance role
(step 4) and does **not** create a DLQ.

---

## 6. Verify the relay before wiring COM

**Liveness / readiness** (readiness returns `503` until the queue is reachable):

```powershell
curl.exe -s "https://$FQDN/healthz"
curl.exe -s "https://$FQDN/readyz"
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

## 7. Configure the COM webhook

In the HPE GreenLake / Compute Ops Management console, create a webhook:

- **Destination URL:** `https://<FQDN>/com/webhook`  (from step 5)
- **Custom header:** name `x-shim-secret` (your `$HDR`), value = `$SECRET`.
  COM authenticates with a **static header only** — this is that header.
- **Event filter (`eventFilter`):** scope it to servers so the lab stays quiet,
  e.g. server health changes. Filtering happens **server-side at COM**; the relay
  forwards whatever COM sends.

COM will first call `GET` (the handshake in step 6) and only enable the webhook
once it echoes the challenge over public HTTPS with a valid certificate — App
Runner provides that TLS automatically.

> Keep the webhook **healthy**: COM disables a webhook after **10 consecutive
> non-2xx** responses. The relay returns `202` as soon as the event is queued, so
> a slow/broken GitHub target never affects webhook health — that decoupling is
> the whole point of the queue.

---

## 8. Create the shim's IAM identity (receive-only) and run it

The shim uses the **default boto3 credential chain**. On ECS/EC2 give it a
**task/instance role**; for a laptop test, create a small **IAM user** limited to
consuming this one queue.

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

Now run the shim on any machine with **outbound** internet. It needs the queue
URL, region, the receive-only credentials, and your GitHub details. Nothing
inbound is opened.

```powershell
docker run --rm --name com-event-shim `
  -e QUEUE_BACKEND=sqs `
  -e "SQS_QUEUE_URL=$QUEUE_URL" `
  -e "AWS_REGION=$REGION" `
  -e "AWS_ACCESS_KEY_ID=$AWS_ID" `
  -e "AWS_SECRET_ACCESS_KEY=$AWS_SECRET" `
  -e TARGETS=github `
  -e GITHUB_REPO=your-org/com-lab-issues `
  -e GITHUB_TOKEN=<your-fine-grained-PAT> `
  -e SERVER_MONITORS=health `
  ghcr.io/jullienl/com-event-shim:latest
```

Key env vars (full list in [shim/.env.example](../shim/.env.example)):

| Var | Value here | Notes |
|-----|-----------|-------|
| `QUEUE_BACKEND` | `sqs` | Must match the relay. |
| `SQS_QUEUE_URL` | `$QUEUE_URL` | The main queue (not the DLQ). |
| `AWS_REGION` | `$REGION` | Region of the queue. |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | com-shim keys | Or use an ECS/EC2 role and omit these. |
| `TARGETS` | `github` | One name, or comma-separated to fan out (`github,slack`). |
| `GITHUB_REPO` / `GITHUB_TOKEN` | your repo + PAT | Token needs **Issues: read/write**. |
| `SERVER_MONITORS` | `health` | Watch server health (default). Add `power`/`connection`/`subscription` to watch more. |
| `DEDUP_TTL_SECONDS` | `3600` (default) | Suppresses duplicate/redelivered events within the window. |

> **Credentials hygiene:** injecting keys as env vars is fine for a quick test;
> for anything longer-lived prefer an **IAM role** (ECS task role / EC2 instance
> profile) so there are no static keys. The shim's file-or-env secret helper also
> supports `GITHUB_TOKEN_FILE` if you'd rather mount the PAT than pass it inline.

The shim logs each message it drains, the events it normalises, and the forward
result. Leave it running for the end-to-end test.

---

## 9. End-to-end test

**Path A — real COM event.** Trigger (or wait for) a server health change that
matches your `eventFilter`. Within a few seconds you should see:
1. Relay logs a `202` for the POST.
2. Shim logs the drained message → `forwarded to GitHub`.
3. A new **issue** appears in your repo, labelled `com:server:<serial>:health`.
When the server returns to healthy, COM sends a *clear*; the shim finds the open
issue by that label and **closes** it (with a recovery comment).

**Path B — synthetic event (no waiting).** Post a sample server snapshot straight
at the relay to exercise the whole chain: a `raise` snapshot (unhealthy) opens an
issue, a `clear` snapshot (healthy) closes it. Generate the exact raw COM payloads
the repo ships as fixtures (run from the repo root, in your Python venv):

```powershell
# Write clean raw-payload JSON files (uses the shipped sample builder)
python -c "import sys, json; sys.path.insert(0,'com-event-core/examples'); import dump_payloads as d; open('raise.json','w',encoding='utf-8').write(json.dumps(d._build_payload('server','raise')))"
python -c "import sys, json; sys.path.insert(0,'com-event-core/examples'); import dump_payloads as d; open('clear.json','w',encoding='utf-8').write(json.dumps(d._build_payload('server','clear')))"

curl.exe -s -o NUL -w "%{http_code}`n" -X POST "https://$FQDN/com/webhook" `
  -H "content-type: application/json" -H "$HDR`: $SECRET" --data "@raise.json"
# → issue opens

curl.exe -s -o NUL -w "%{http_code}`n" -X POST "https://$FQDN/com/webhook" `
  -H "content-type: application/json" -H "$HDR`: $SECRET" --data "@clear.json"
# → same issue closes
```

> First *clear* can occasionally race GitHub's search index (~1s lag); if the
> issue isn't found the message is abandoned and redelivered, and the retry
> closes it — no lost events.

---

## 10. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| COM won't enable the webhook | Handshake failed | Confirm `GET /com/webhook` echoes the challenge over **public HTTPS** (step 6). Check the URL has no typo and ends in `/com/webhook`. |
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
# App Runner application logs go to CloudWatch Logs
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

## 11. Tear down

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
