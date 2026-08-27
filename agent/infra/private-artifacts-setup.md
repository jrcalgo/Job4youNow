# Provisioning runbook — private-user-artifacts S3 bucket + IAM split

Sets up the SECOND of the two S3 buckets the privacy boundary requires (see
[agent/app/models/artifacts.py](../app/models/artifacts.py)'s `ArtifactBucket`)
— a bucket used ONLY for private user data (augmented resumes, private text
responses/reports), never shared with the job-search artifact bucket
[telegram/infra/aurora-setup.md](../../telegram/infra/aurora-setup.md)
already provisions. Run these once, in order, with the AWS CLI. Assumes
you've already run `aws configure` (or otherwise have credentials + a
default region) and, ideally, already provisioned Aurora per the doc above
— this bucket is independent of Aurora, but the agent app needs both.

```bash
export AWS_REGION=us-east-1          # same region as your Aurora cluster
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export PRIVATE_BUCKET=job4younow-private-user-artifacts-$ACCOUNT_ID
```

## 1. Create the bucket, locked down exactly like the job-artifacts bucket

Same baseline hardening as the job-artifacts bucket — public access fully
blocked, encrypted at rest. This bucket gets no principal attached beyond
the two policies below; it is never shared with any other feature.

```bash
aws s3api create-bucket --bucket "$PRIVATE_BUCKET" --region "$AWS_REGION" \
  $( [ "$AWS_REGION" != "us-east-1" ] && echo --create-bucket-configuration LocationConstraint="$AWS_REGION" )
aws s3api put-public-access-block --bucket "$PRIVATE_BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket "$PRIVATE_BUCKET" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
```

## 2. Agent app IAM policy — read + write

The agent app is the only thing that ever WRITES here (augmented resumes,
materialized private text responses — see
[agent/app/tools/artifact_store.py](../app/tools/artifact_store.py)).
[agent-iam-policy.json](agent-iam-policy.json) deliberately grants NOTHING
on the job-artifacts bucket yet — add that only when a concrete feature
needs it (least privilege over speculative access).

```bash
sed -e "s#{{PRIVATE_USER_ARTIFACTS_BUCKET}}#$PRIVATE_BUCKET#" \
    -e "s#{{AURORA_RESOURCE_ARN}}#$(aws rds describe-db-clusters --region "$AWS_REGION" --db-cluster-identifier job4younow-telegram --query 'DBClusters[0].DBClusterArn' --output text)#" \
    -e "s#{{AURORA_SECRET_ARN}}#$(aws rds describe-db-clusters --region "$AWS_REGION" --db-cluster-identifier job4younow-telegram --query 'DBClusters[0].MasterUserSecret.SecretArn' --output text)#" \
    infra/agent-iam-policy.json > /tmp/job4younow-agent-policy.json

aws iam create-policy --policy-name job4younow-agent --policy-document file:///tmp/job4younow-agent-policy.json
# then attach it to whatever principal runs the agent app container.
```

## 3. Telegram adapter IAM policy — read only, private bucket

The adapter only ever DOWNLOADS an object to deliver it — it never writes,
lists, or deletes here, and it has NO Aurora access at all (see the
"Telegram writes no DB" boundary
[telegram/docs/producing-queues.md](../../telegram/docs/producing-queues.md)
already establishes for the job-queue pipeline). Attach this ALONGSIDE the
adapter's existing grant on the job-artifacts bucket
([telegram/infra/iam-policy.json](../../telegram/infra/iam-policy.json)) —
that one still needs `PutObject`/`DeleteObject` for `cli.mjs`'s `ingest`
step; this one is read-only because the adapter never uploads here.

```bash
sed -e "s#{{PRIVATE_USER_ARTIFACTS_BUCKET}}#$PRIVATE_BUCKET#g" \
    infra/telegram-iam-policy.json > /tmp/job4younow-telegram-private-policy.json

aws iam create-policy --policy-name job4younow-telegram-private-read \
  --policy-document file:///tmp/job4younow-telegram-private-policy.json
# then attach it to whatever principal runs the telegram adapter container.
```

## 4. Fill in the repo-root `.env`

```bash
# PRIVATE_USER_ARTIFACTS_BUCKET=<the bucket name printed by step 1>
```

## Teardown

```bash
aws s3 rm "s3://$PRIVATE_BUCKET" --recursive && aws s3api delete-bucket --bucket "$PRIVATE_BUCKET"
aws iam detach-user-policy --user-name <agent-iam-user> --policy-arn <ARN from step 2>
aws iam delete-policy --policy-arn <ARN from step 2>
aws iam detach-role-policy --role-name <telegram-iam-role> --policy-arn <ARN from step 3>
aws iam delete-policy --policy-arn <ARN from step 3>
```
