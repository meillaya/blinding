# AWS provider with an OFFLINE mock mode (todo 24 — the Oracle B1 fix).
#
# skip_credentials_validation / skip_requesting_account_id / skip_metadata_api_check
# plus mock keys suppress only the provider's OWN init calls — they do NOT stop
# data sources, which plan still reads. So when offline_plan=true we ALSO point
# every service the plan touches at the local moto server (scripts/tf_mock.sh,
# http://localhost:5000) via a dynamic "endpoints" block. moto then answers the
# mockable data sources (aws_caller_identity / aws_iam_session_context via STS,
# aws_subnet / availability zones via EC2); aws_partition / aws_region resolve
# locally without any API call.
#
# When offline_plan=false this collapses to a normal provider: real credentials
# via the default chain, real AWS endpoints, no skips — the path that actually
# creates the OIDC provider / IRSA and resolves real AMIs and addon versions.
provider "aws" {
  region = var.region

  dynamic "endpoints" {
    for_each = var.offline_plan ? [1] : []
    content {
      sts        = "http://localhost:5000"
      iam        = "http://localhost:5000"
      ec2        = "http://localhost:5000"
      eks        = "http://localhost:5000"
      s3         = "http://localhost:5000"
      elbv2      = "http://localhost:5000"
      cloudwatch = "http://localhost:5000"
      logs       = "http://localhost:5000"
      ssm        = "http://localhost:5000"
    }
  }

  skip_credentials_validation = var.offline_plan
  skip_requesting_account_id  = var.offline_plan
  skip_metadata_api_check     = var.offline_plan

  # Static mock credentials offline (moto accepts any SigV4 signer); the default
  # credential chain online.
  access_key = var.offline_plan ? "mock" : null
  secret_key = var.offline_plan ? "mock" : null

  # Path-style S3 addressing so bucket operations work against localhost (moto),
  # where virtual-hosted-style DNS cannot resolve.
  s3_use_path_style = var.offline_plan
}
