# AWS Hosting

Target: persistent 24/7 polling on ECS Fargate.

## Steps

1. **Build and push the image**
   ```bash
   aws ecr create-repository --repository-name edge-engine
   docker build -t edge-engine .
   docker tag edge-engine:latest <account>.dkr.ecr.<region>.amazonaws.com/edge-engine:latest
   docker push <account>.dkr.ecr.<region>.amazonaws.com/edge-engine:latest
   ```
   (Add a `Dockerfile` based on `python:3.11-slim` running
   `python -m edge_engine.scheduler`.)

2. **Secrets** - store in Secrets Manager: `edge-engine/keys`
   (`KALSHI_KEY_ID`, `ODDS_API_KEY`, `DISCORD_WEBHOOK_URL`, ...).
   Reference them in the task definition `secrets` block; grant the task
   execution role `secretsmanager:GetSecretValue` on that secret only.

3. **ECS service** - Fargate launch type, 0.25 vCPU / 0.5 GB, desired count 1.
   Command runs the scheduler loop. Health check: a lightweight HTTP endpoint
   (add one) or a CloudWatch "last successful tick" metric with an alarm.

4. **Networking** - for v1, public subnets with `assignPublicIp: ENABLED`
   avoids the NAT Gateway (~$30+/mo). Move to private subnets + VPC endpoints
   (Secrets Manager, DynamoDB, ECR) when hardening.

5. **State** - DynamoDB on-demand tables `edge-signals`, `edge-alerts`,
   `edge-fills` (same key shape as the SQLite schema in `storage/state.py`).

6. **Batch jobs** - EventBridge Scheduler -> ECS RunTask for the daily P&L
   summary and the market-mapping refresh.

7. **Ops** - CloudWatch Logs via the `awslogs` driver; alarms on task
   stopped, error rate, and "no signals in N hours" (stale-data detector);
   SNS topic for ops alerts, separate from the Discord user alerts.

## Cost shape

Fargate at this size is a few dollars a month; Secrets Manager about $0.40 per
secret; DynamoDB on-demand a few dollars. Realistic baseline $10-50/mo.
Verify against current AWS pricing for your region.
