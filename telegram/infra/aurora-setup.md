# Provisioning runbook — Aurora Serverless v2 + S3 + IAM

Run these once, in order, with the AWS CLI. Each step's output feeds the next — the last section maps everything into `.env`. All commands assume you've already run `aws configure` (or otherwise have credentials + a default region), and use a single shell variable block so you can copy-paste the whole thing region by region.

```bash
export AWS_REGION=us-east-1          # pick yours
export CLUSTER_ID=job4menow-telegram
export DB_NAME=job4menow
export MASTER_USERNAME=job4menow_admin
export BUCKET_NAME=job4menow-telegram-artifacts-$(aws sts get-caller-identity --query Account --output text)
```

## 1. Confirm Serverless v2 scale-to-zero is available in your region

Auto-pause (min ACU 0) requires a recent enough engine version, and availability varies slightly by region:

```bash
aws rds describe-orderable-db-instance-options \
  --engine aurora-postgresql \
  --db-instance-class db.serverless \
  --region "$AWS_REGION" \
  --query 'OrderableDBInstanceOptions[].[EngineVersion]' --output text
```

Pick a version 16.1+ or 17.4+ (either clears the Data API + scale-to-zero bar everywhere) and export it:

```bash
export ENGINE_VERSION=16.4   # replace with a version the query above actually returned
```

## 2. Network: default VPC is fine

Data API calls go over the normal AWS HTTPS API plane, not the database's network port — the VPC only has to exist, it doesn't need public routing or an open security group. The default VPC's default subnet group and security group both work; skip this section entirely if you already have a VPC you'd rather use.

```bash
export VPC_ID=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text --region "$AWS_REGION")
export SUBNET_GROUP=default   # RDS provisions one named "default" per default VPC automatically
export SECURITY_GROUP_ID=$(aws ec2 describe-security-groups --filters Name=vpc-id,Values="$VPC_ID" Name=group-name,Values=default --query 'SecurityGroups[0].GroupId' --output text --region "$AWS_REGION")
```

No inbound rule is needed on that security group for the Data API path — leave it locked down.

## 3. Create the cluster (min 0 ACU, Data API enabled, credentials in Secrets Manager)

`--manage-master-user-password` has RDS generate the password and create+manage the Secrets Manager secret for you — no separate secret-creation step, and no password ever appears in your shell history.

```bash
aws rds create-db-cluster \
  --region "$AWS_REGION" \
  --db-cluster-identifier "$CLUSTER_ID" \
  --engine aurora-postgresql \
  --engine-version "$ENGINE_VERSION" \
  --master-username "$MASTER_USERNAME" \
  --manage-master-user-password \
  --database-name "$DB_NAME" \
  --db-subnet-group-name "$SUBNET_GROUP" \
  --vpc-security-group-ids "$SECURITY_GROUP_ID" \
  --serverless-v2-scaling-configuration MinCapacity=0,MaxCapacity=2,SecondsUntilAutoPause=300 \
  --enable-http-endpoint
```

`MinCapacity=0` is what makes auto-pause possible at all — anything ≥0.5 disables it. `SecondsUntilAutoPause=300` is the minimum (5 min); raise it (up to 86400 = 24h) if you'd rather trade a few cents of idle compute for fewer cold-start delays. `MaxCapacity=2` (≈4 GiB RAM) is comfortably more than this workload needs; lower it to 1 if you want a hard cost ceiling.

## 4. Add the one DB instance the cluster needs

Serverless v2 clusters still need an explicit writer instance — the CLI (unlike the console) doesn't create one automatically:

```bash
aws rds create-db-instance \
  --region "$AWS_REGION" \
  --db-cluster-identifier "$CLUSTER_ID" \
  --db-instance-identifier "${CLUSTER_ID}-1" \
  --db-instance-class db.serverless \
  --engine aurora-postgresql
```

Wait for both to become available (a few minutes):

```bash
aws rds wait db-instance-available --region "$AWS_REGION" --db-instance-identifier "${CLUSTER_ID}-1"
```

## 5. Collect the two ARNs `.env` needs

```bash
aws rds describe-db-clusters \
  --region "$AWS_REGION" \
  --db-cluster-identifier "$CLUSTER_ID" \
  --query 'DBClusters[0].{ResourceArn:DBClusterArn,SecretArn:MasterUserSecret.SecretArn}'
```

Copy the two values into `AURORA_RESOURCE_ARN` and `AURORA_SECRET_ARN` in `.env`.

## 6. S3 bucket for artifacts

```bash
aws s3api create-bucket --bucket "$BUCKET_NAME" --region "$AWS_REGION" \
  $( [ "$AWS_REGION" != "us-east-1" ] && echo --create-bucket-configuration LocationConstraint="$AWS_REGION" )
aws s3api put-public-access-block --bucket "$BUCKET_NAME" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket "$BUCKET_NAME" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
```

CV/JD artifacts are personal data — the public-access-block call is not optional.

## 7. IAM policy

[`iam-policy.json`](iam-policy.json) is a template scoped to exactly `rds-data:ExecuteStatement` / `BatchExecuteStatement` / `BeginTransaction` / `CommitTransaction` / `RollbackTransaction` on the cluster, `secretsmanager:GetSecretValue` on the one secret, and `s3:GetObject`/`PutObject`/`DeleteObject`/`ListBucket` on the one bucket — nothing broader. Fill in the three placeholders (cluster ARN, secret ARN, bucket name), then attach it to whatever principal actually calls the AWS SDK: your own IAM user/role if you're running the daemon and `cli.mjs ingest` locally with `AWS_PROFILE`, or a dedicated role later if this moves off a laptop.

```bash
sed -e "s#{{AURORA_RESOURCE_ARN}}#$(aws rds describe-db-clusters --region "$AWS_REGION" --db-cluster-identifier "$CLUSTER_ID" --query 'DBClusters[0].DBClusterArn' --output text)#" \
    -e "s#{{AURORA_SECRET_ARN}}#$(aws rds describe-db-clusters --region "$AWS_REGION" --db-cluster-identifier "$CLUSTER_ID" --query 'DBClusters[0].MasterUserSecret.SecretArn' --output text)#" \
    -e "s#{{S3_BUCKET}}#$BUCKET_NAME#g" \
    infra/iam-policy.json > /tmp/job4menow-telegram-policy.json

aws iam create-policy --policy-name job4menow-telegram --policy-document file:///tmp/job4menow-telegram-policy.json
# then attach it to your IAM user/role, e.g.:
aws iam attach-user-policy --user-name <your-iam-user> --policy-arn <the ARN create-policy just printed>
```

## 8. Fill in `.env` and initialize the schema

```bash
cp .env.example .env
# edit .env: AWS_REGION, AURORA_RESOURCE_ARN, AURORA_SECRET_ARN, AURORA_DATABASE=job4menow,
# S3_BUCKET, plus TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID (see docs/producing-queues.md — a
# SEPARATE bot from career-ops' own telegram mode) and your AWS credentials/profile.

docker compose build
docker compose run --rm bot node src/cli.mjs migrate
docker compose up -d
docker compose logs -f bot   # confirm "daemon started, entering poll loop"
```

## Living with auto-pause

- The database pauses after `SecondsUntilAutoPause` of no Data API activity. bot.mjs deliberately never polls Aurora in its idle loop (see `src/db/schema.sql`'s header comment) specifically so this can actually happen — don't "fix" that by adding a periodic health-check query, or the cluster will never pause and you'll pay for 24/7 compute for no benefit.
- The first Data API call after a pause throws `DatabaseResumingException` and resumes in ~12-15s; `src/lib/retry.mjs` retries this automatically and transparently. A `CV`/`JD`/`NEXT` sent right after a long idle period may just take a few seconds longer than usual — this is expected, not a hang.
- `aws rds describe-db-clusters --db-cluster-identifier "$CLUSTER_ID" --query 'DBClusters[0].Status'` and the RDS console's Recent Events both show pause/resume transitions if you want to confirm it's actually happening.

## Teardown

```bash
docker compose down
aws rds delete-db-instance --db-instance-identifier "${CLUSTER_ID}-1" --skip-final-snapshot
aws rds delete-db-cluster --db-cluster-identifier "$CLUSTER_ID" --skip-final-snapshot
aws s3 rm "s3://$BUCKET_NAME" --recursive && aws s3api delete-bucket --bucket "$BUCKET_NAME"
aws iam detach-user-policy --user-name <your-iam-user> --policy-arn <the policy ARN from step 7>
aws iam delete-policy --policy-arn <the policy ARN from step 7>
```
