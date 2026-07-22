# Model-artifacts bucket for InferenceOps.
#
# Thin wrapper around terraform-aws-modules/s3-bucket/aws (~> 5.15) pinned to the
# settings model storage needs:
#   * versioning enabled  — every artifact write is immutable-by-history; a bad
#     upload can be rolled back to the previous object version.
#   * SSE AES256 at rest  — artifacts are encrypted server-side (S3-managed keys;
#     no KMS dependency so the bucket works in every account/region, incl. moto).
#   * all public access blocked — model weights are never served from S3 directly
#     (the model-server fetches them over IRSA-scoped reads).
#
# Consumed by the env roots (todo 24); the model-server IRSA role (iam module)
# gets AmazonS3ReadOnlyAccess to read artifacts from this bucket.

module "s3_bucket" {
  source  = "terraform-aws-modules/s3-bucket/aws"
  version = "~> 5.15"

  bucket        = var.bucket_name
  force_destroy = var.force_destroy

  # s3-bucket v5 maps `enabled = true` to versioning_configuration.status = "Enabled".
  versioning = {
    enabled = true
  }

  server_side_encryption_configuration = {
    rule = {
      apply_server_side_encryption_by_default = {
        sse_algorithm = "AES256"
      }
    }
  }

  # Model artifacts must never be publicly readable.
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true

  tags = var.tags
}
