# Terraform (optional AWS layer)

> **This entire directory is optional.** The core InferenceOps platform runs
> fully on a local kind cluster using the manifests under [`k8s/`](../../k8s/)
> and [`scripts/deploy_local.sh`](../../scripts/deploy_local.sh). Nothing in
> the local platform depends on anything here. No AWS account, credentials, or
> spend are needed to run, test, or understand the platform.

## What this is for

The Terraform modules and environment roots in this directory exist for
AWS-readiness and to demonstrate infrastructure-as-code. They show how the same
Kubernetes manifests and application images would land on EKS, with managed
services (ECR, S3, IAM/IRSA, ALB, CloudWatch) replacing the local equivalents.

## Layout

```
modules/   vpc, eks, ecr, s3, iam, alb, monitoring
envs/      dev, staging, prod (each wires all seven modules)
```

## Offline validation

The Terraform surface was validated offline with no real AWS access.
[`scripts/tf_mock.sh`](../../scripts/tf_mock.sh) starts a local
[moto](https://github.com/getmoto/moto) mock endpoint, and `terraform plan`
runs against it (the default `offline_plan = true`). This proves the resource
graph, counts, and schema are coherent without touching a live account.
`terraform apply` is never run against the mock and is always a manual step
against real AWS.

## See also

- [docs/aws-deployment.md](../../docs/aws-deployment.md): full module walkthrough, IRSA, GPU node group, cost.
- [envs/prod/README.md](envs/prod/README.md): the prod environment root's own notes.
