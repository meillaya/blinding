# s3 module

Versioned, SSE-AES256-encrypted, fully-private bucket that stores InferenceOps
model artifacts. Thin wrapper around
[`terraform-aws-modules/s3-bucket/aws ~> 5.15`](https://registry.terraform.io/modules/terraform-aws-modules/s3-bucket/aws).

The model-server reads artifacts from this bucket through the IRSA role created
by the [`iam`](../iam/) module (`AmazonS3ReadOnlyAccess`).

## Usage

```hcl
module "s3" {
  source = "../../modules/s3"

  bucket_name   = "inferenceops-dev-model-artifacts"
  force_destroy = true # dev only
}
```

## Variables

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `bucket_name` | `string` | — | Globally unique bucket name (validated against S3 naming rules). |
| `force_destroy` | `bool` | `false` | Allow destroying a non-empty bucket (dev/staging convenience). |
| `tags` | `map(string)` | `{}` | Tags applied to the bucket. |

## Outputs

| Name | Description |
|------|-------------|
| `bucket_arn` | Bucket ARN, for IAM policies scoping model-server reads. |
| `bucket_id` | Bucket name (ID). |

## Notes

- Versioning is enabled so every artifact write is recoverable.
- Server-side encryption uses S3-managed keys (AES256) — no KMS dependency.
- All public access is blocked; artifacts are never served from S3 directly.
