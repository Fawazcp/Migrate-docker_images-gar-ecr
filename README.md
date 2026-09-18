# Migrating 1TB of Docker Images from Google Artifact Registry to AWS ECR: A Step-by-Step Guide

*How I moved roughly a thousand-plus container images across clouds without losing a single tag, using skopeo, a throwaway EC2 box, and zero long-lived credentials.*

> A note before you start: I'm sharing the exact approach and commands I used in my own proof-of-concept. Cloud CLI syntax, AMI availability, and pricing change over time — I've called out the specific spots below where you should double-check current values against the official AWS and GCP docs before running anything against a production registry.

---

## Why I wrote this

I had around 1TB of Docker images sitting in Google Artifact Registry (GAR), organized as one repository containing a folder per service, each folder holding every version of that service's image under its own tags (things like `2026-09-10_14-00-00-dev`, `-qa`, `-stage` — a typical promotion pattern where one build gets multiple tags as it moves through environments).

The goal: recreate the same structure in AWS ECR — one private ECR repository per service, matching names, every tag preserved exactly as it existed in GAR — without manually pulling and pushing a thousand-plus images by hand.

This post walks through the whole thing: the tool choice, the environment setup (including why I ended up running the migration from an AWS EC2 instance instead of a GCP VM), the migration script itself, the problems I actually hit along the way, and what it cost.

---

## The approach: why not just `docker pull` + `docker push`?

The obvious first instinct — `docker pull` from GAR, `docker tag`, `docker push` to ECR — has two problems at any real scale:

1. **Multi-architecture images break.** `docker pull`/`docker push` only pull the manifest for your local machine's platform. If an image is a multi-arch manifest list (`linux/amd64` + `linux/arm64`, say), a plain pull/push silently drops the architectures that don't match whatever machine you're running the migration from.
2. **It's slower and heavier.** Every image gets fully unpacked onto local disk, then repacked, instead of streaming registry-to-registry.

Instead, I used **[skopeo](https://github.com/containers/skopeo)**, a tool built specifically for registry-to-registry operations without needing a local Docker daemon at all:

```bash
skopeo copy --all docker://SOURCE_REF docker://DEST_REF
```

The `--all` flag is the important part — it copies every platform in a multi-arch manifest list, not just the one matching the local machine. It also copies faster, since it can stream blobs directly between registries.

📸 *Screenshot suggestion: your GAR repository browser showing one service's package with multiple tags (e.g. `-dev`, `-qa`, `-stage`) pointing at different digests — this is the structure the migration needs to preserve.*

---

## Architecture: what actually talks to what

At a high level:

- A short-lived **EC2 instance** in AWS is the migration host — it runs `skopeo`, `gcloud`, and `aws` CLI, plus the Python migration script.
- On the **GCP side**, the instance authenticates as a dedicated **read-only service account** (not your personal `gcloud auth login` identity).
- On the **AWS side**, the instance authenticates via an **EC2 instance IAM role** (no `aws configure`, no static access keys stored anywhere).
- The script lists every (image, tag) pair in the GAR repository, creates a matching ECR repository per service if one doesn't already exist, and copies each tag across with `skopeo`.

📸 *Screenshot suggestion: a simple box diagram — EC2 instance in the middle, arrow labeled "service account key" from GCP/GAR, arrow labeled "instance role" from AWS/IAM, arrow labeled "skopeo copy --all" going from GAR to ECR.*

A question I had going in was whether it's better to run this from a GCP VM or an AWS EC2 instance, since the images have to cross clouds either way. It turns out **it doesn't matter for cost or speed** — GCP bills egress once, at the point data leaves GCP's network, regardless of which cloud is on the receiving end. What *does* change is credential management: running from EC2 means AWS-side auth is a role, not a key you have to protect. That's the whole reason for this choice — not performance.

---

## Prerequisites

You'll need:

- **Tools** on the migration instance: `gcloud`, `aws` CLI, `skopeo`, Python 3.
- **GCP permissions**: `roles/artifactregistry.reader` on the source repository (or project), granted to a dedicated service account.
- **AWS permissions**: an IAM policy covering ECR login, repo creation/description, and image push/pull/verify. Here's the policy I used — replace the region and account ID placeholders with your real values:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ECRAuth",
      "Effect": "Allow",
      "Action": "ecr:GetAuthorizationToken",
      "Resource": "*"
    },
    {
      "Sid": "ECRRepoManage",
      "Effect": "Allow",
      "Action": [
        "ecr:CreateRepository",
        "ecr:DescribeRepositories"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ECRPushPullAndVerify",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage",
        "ecr:PutImage",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload"
      ],
      "Resource": "arn:aws:ecr:<region>:<account-id>:repository/*"
    }
  ]
}
```

`ecr:CreateRepository` and `ecr:DescribeRepositories` need `Resource: "*"` since a not-yet-created repository has no ARN to scope to yet.

---

## Step 1: Create a read-only GCP service account

On the GCP side, create a service account scoped to read-only access on the source repository, then generate a JSON key for it. This key is the only GCP credential that will leave your GCP project.

```bash
gcloud iam service-accounts create gar-ecr-migration-reader \
  --project=<your-gcp-project-id> \
  --display-name="Read-only GAR access for ECR migration"

gcloud artifacts repositories add-iam-policy-binding <your-gar-repo> \
  --project=<your-gcp-project-id> \
  --location=<your-gcp-region> \
  --member="serviceAccount:gar-ecr-migration-reader@<your-gcp-project-id>.iam.gserviceaccount.com" \
  --role="roles/artifactregistry.reader"

gcloud iam service-accounts keys create gar-ecr-migration-reader-key.json \
  --iam-account=gar-ecr-migration-reader@<your-gcp-project-id>.iam.gserviceaccount.com
```

Prefer clicking through instead? In the Cloud Console: **IAM & Admin → Service Accounts → Create Service Account**, grant it **Artifact Registry Reader** on the repository, then **Keys → Add Key → Create new key → JSON**.

📸 *Screenshot suggestion: the Cloud Console "Create Service Account" screen, and the IAM role-binding screen showing "Artifact Registry Reader" granted to the new service account.*

Treat the downloaded key file as a secret. It gets copied to the EC2 instance in a later step, and should be deleted from both places once the migration is verified.

---

## Step 2: Create an AWS IAM role and instance profile

This is what replaces `aws configure` and static access keys entirely. The EC2 instance launches with an instance profile attached, and the AWS CLI on that instance automatically picks up temporary, auto-rotating credentials from the instance metadata service — nothing is stored on disk.

Trust policy (save as `ec2-trust-policy.json`):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "ec2.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

```bash
aws iam create-role \
  --role-name gar-ecr-migration-role \
  --assume-role-policy-document file://ec2-trust-policy.json

aws iam put-role-policy \
  --role-name gar-ecr-migration-role \
  --policy-name gar-ecr-migration-ecr-access \
  --policy-document file://ecr-migration-policy.json

aws iam create-instance-profile \
  --instance-profile-name gar-ecr-migration-profile

aws iam add-role-to-instance-profile \
  --instance-profile-name gar-ecr-migration-profile \
  --role-name gar-ecr-migration-role
```

(`ecr-migration-policy.json` is the ECR policy JSON from the Prerequisites section above, with your real region and account ID filled in.)

📸 *Screenshot suggestion: the IAM console showing the new role, its trust relationship tab, and the instance profile attached to it.*

---

## Step 3: Launch the EC2 migration instance

I used a Debian/Ubuntu-based AMI rather than Amazon Linux — `skopeo` installs cleanly via `apt-get` on Ubuntu, and I didn't verify its availability/version through Amazon Linux's package manager, so I'd rather not guess there. Attach the instance profile created above at launch time.

```bash
aws ec2 run-instances \
  --image-id ami-xxxxxxxxxxxxxxxxx \
  --instance-type t3.medium \
  --key-name your-ec2-keypair \
  --iam-instance-profile Name=gar-ecr-migration-profile \
  --region <your-aws-region> \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=gar-ecr-migration}]'
```

Replace the AMI ID with a current Ubuntu/Debian AMI for your region — AMI IDs are region- and release-specific and change over time, so look up the current one rather than reusing an old value from a blog post (including this one).

Once it's running, grab its public IP and SSH in:

```bash
aws ec2 describe-instances --instance-ids i-xxxxxxxxxxxxxxxxx \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text

ssh -i your-ec2-keypair.pem admin@<public-ip>   # "admin" on Debian AMIs, "ubuntu" on Ubuntu AMIs
```

📸 *Screenshot suggestion: the EC2 console showing the running instance with the IAM instance profile column visible, confirming it's attached.*

---

## Step 4: Install the tools

```bash
sudo apt-get update
sudo apt-get install -y skopeo python3 python3-pip unzip curl tmux

# AWS CLI v2
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip awscliv2.zip
sudo ./aws/install

# gcloud CLI
curl -O https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-linux-x86_64.tar.gz
tar -xf google-cloud-cli-linux-x86_64.tar.gz
./google-cloud-sdk/install.sh
exec -l $SHELL
```

I'm not fully certain the AWS CLI and gcloud download URLs above will still be current by the time you read this — double-check them against the official AWS CLI and Google Cloud SDK install docs before running.

---

## Step 5: Authenticate — no long-lived keys anywhere

Copy the GCP service account key you generated in Step 1 to the instance (`scp` it alongside the script — see Step 6), then:

```bash
gcloud auth activate-service-account --key-file=gar-ecr-migration-reader-key.json
gcloud config set project <your-gcp-project-id>

aws sts get-caller-identity
```

No `aws configure`, no AWS access key or secret anywhere on this box. `aws sts get-caller-identity` should return the EC2 instance role's ARN — that's your confirmation the instance profile is attached and working.

📸 *Screenshot suggestion: terminal output of `aws sts get-caller-identity`, with the returned ARN visible, showing it's the role — not a personal IAM user.*

---

## Step 6: The migration script

Transfer the script and the service account key to the instance:

```bash
scp -i your-ec2-keypair.pem migrate_gar_to_ecr.py gar-ecr-migration-reader-key.json admin@<public-ip>:~/
```

Here's what the script actually does, at a high level:

- **Discovers everything first.** It calls `gcloud artifacts docker images list <repo> --include-tags --format=json` at the *repository* level, so every service/package folder underneath is picked up automatically — you don't list services one by one.
- **Creates ECR repos up front, sequentially**, before any parallel copying starts. This avoids two worker threads racing to create the same not-yet-existing repository at once.
- **Copies with `skopeo copy --all`**, wrapped in a retry loop (3 attempts with backoff) for transient network errors.
- **Verifies and resumes via digest comparison.** Before copying, it checks whether the destination tag already has the same raw manifest digest as the source; if so, it's skipped. This makes the whole run safely resumable — if it dies partway through (or you kill it on purpose), re-running just picks up where it left off instead of re-copying everything.
- **Parallelizes** across a configurable number of workers (`--workers`, default 8) using a thread pool, with a lock around the shared auth/token-refresh logic so threads don't race to re-login at once.
- **Optionally deduplicates tags that share a digest** (`--dedupe-tags`) — for a dev/qa/stage promotion pattern where multiple tags point at the same image, it copies the actual image data from GAR exactly once, then adds the remaining tags as fast *registry-side* operations directly on ECR (source and destination are both the ECR repo, so ECR's own blob-check finds everything already present and only a small manifest gets transferred). This can meaningfully cut transfer time and egress cost when a lot of tags share digests, which is common in a promotion-style tagging scheme.

Useful flags:

```
--dry-run                 List what would be migrated, copy nothing
--yes                     Skip the confirmation prompt
--services svc1,svc2      Restrict to specific services
--tag-prefix 2026-09      Only migrate tags starting with this prefix
--limit 5                 Stop after this many (image,tag) pairs — good for a first test
--workers 12              Concurrency (start moderate — very high concurrency can trigger
                           registry-side rate limiting; raise gradually and watch the logs)
--dedupe-tags             Enable the tag-dedup behavior described above
--aws-account-id / --aws-region / --gcp-location / --gcp-project / --gcp-repo
                           Override the values hardcoded at the top of the script
```

---

## Step 7: Dry run, then a small real test, then the full run

I'd strongly recommend this order rather than jumping straight to a full migration:

```bash
# 1. See exactly what it would do — nothing is copied
python3 migrate_gar_to_ecr.py --dry-run

# 2. Migrate a small handful of pairs for real, to sanity-check end to end
python3 migrate_gar_to_ecr.py --yes --limit 5

# 3. Run inside tmux so it survives an SSH disconnect over a multi-hour transfer
tmux new -s migration
python3 migrate_gar_to_ecr.py --yes --dedupe-tags --workers 12
# detach with Ctrl+b, d — reattach later with: tmux attach -t migration
```

📸 *Screenshot suggestion: terminal output of the dry run, showing the "WOULD COPY" / "WOULD RETAG (ECR-side)" lines for a few services.*

📸 *Screenshot suggestion: terminal output of the full run in progress, showing the `[n] service:tag -> succeeded/skipped` progress lines.*

---

## Step 8: Verify

Spot-check a service by comparing raw manifest digests between GAR and ECR directly (this is exactly what the script does internally for its own resumability check):

```bash
skopeo inspect --authfile ~/.skopeo_auth_gar_ecr.json --raw \
  docker://<gcp-location>-docker.pkg.dev/<project>/<repo>/<service>:<tag> | sha256sum

skopeo inspect --authfile ~/.skopeo_auth_gar_ecr.json --raw \
  docker://<account-id>.dkr.ecr.<region>.amazonaws.com/<service>:<tag> | sha256sum
```

Matching hashes confirm the image content — including all architectures in a multi-arch manifest — is identical on both sides. It's also worth checking the migration log under `logs/migration_*.log` for any `failed` entries at the end of the run.

📸 *Screenshot suggestion: the ECR console repository list showing repos matching your GAR service names, and one repo's tag list showing all the preserved tags.*

---

## Issues I actually hit (and how I fixed them)

| Issue | What happened | Fix |
|---|---|---|
| `docker push` / `skopeo copy`: connection refused | Repeated, intermittent connection-refused errors against both GCP and AWS endpoints during concurrent transfers | Traced to a shared/ephemeral network limitation specific to Google **Cloud Shell** — the same commands worked fine from a dedicated VM. If you hit this, get off Cloud Shell for anything beyond a quick test. |
| `skopeo login` fails with `mkdir /run/containers: permission denied` | Rootless container tooling defaults to a credentials path under `/run/containers`, which needs root | Pass an explicit, user-writable `--authfile` path to every skopeo command (the script does this automatically via `SKOPEO_AUTHFILE`) |
| `403 Forbidden: trying to reuse blob ... at destination` | Usually a sign the AWS IAM policy's `<region>`/`<account-id>` placeholders were left as literal text instead of being substituted | Run `aws sts get-caller-identity` and review the actual attached policy JSON for un-substituted placeholders |

This table reflects an environment that evolved over the course of the POC: Cloud Shell was tried first and ruled out for the network reason above; a dedicated GCP VM validated the script end-to-end; this post documents running it from AWS EC2 instead, for the credential-management reasons covered earlier — not because the GCP VM had a problem. The `--authfile` fix applies the same way regardless of which cloud the instance is in, since it's really about running skopeo as a non-root user.

---

## Cleanup and cost

Once the migration is verified, tear everything down:

```bash
# Terminate the EC2 instance
aws ec2 terminate-instances --instance-ids i-xxxxxxxxxxxxxxxxx

# Remove the IAM role / instance profile
aws iam remove-role-from-instance-profile \
  --instance-profile-name gar-ecr-migration-profile \
  --role-name gar-ecr-migration-role
aws iam delete-instance-profile --instance-profile-name gar-ecr-migration-profile
aws iam delete-role-policy \
  --role-name gar-ecr-migration-role \
  --policy-name gar-ecr-migration-ecr-access
aws iam delete-role --role-name gar-ecr-migration-role

# Revoke/delete the GCP service account key
gcloud iam service-accounts keys list \
  --iam-account=gar-ecr-migration-reader@<your-gcp-project-id>.iam.gserviceaccount.com
gcloud iam service-accounts keys delete <KEY_ID> \
  --iam-account=gar-ecr-migration-reader@<your-gcp-project-id>.iam.gserviceaccount.com
```

Don't delete the service account itself if you expect to re-run or extend the migration later — just rotate/delete its key between sessions.

**On cost:** for roughly 1TB, I estimated approximately $120–125 as a one-time GCP egress charge (billed once at the GCP network boundary, regardless of which cloud hosts the receiving instance), plus approximately $0.10/GB/month ongoing for ECR storage (roughly $100/month for 1TB stored). These are approximate figures based on published pricing at the time I researched them, not a guarantee for your account — check current rates for your specific regions and any committed-use discounts before budgeting. The EC2 instance itself is billed hourly while running and is cheap relative to the egress/storage numbers above, as long as you terminate it promptly.

---

## Closing thoughts

The two ideas that mattered most here: skopeo for registry-to-registry transfer instead of docker pull/push (so multi-arch images survive intact), and digest-based verification for free resumability (so a multi-hour transfer of ~1TB isn't a single point of failure). Everything else — the EC2 instance, the service account, the IAM role — is really just about making sure nothing sensitive has to live on disk longer than it needs to.

If you try this yourself and hit something not covered here, I'd genuinely like to hear about it — drop a comment below.
