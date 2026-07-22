# IRSA role for the model-server service account.
#
# v6 change: `iam-assumable-role-with-oidc` was REMOVED in terraform-aws-iam v6
# (verified 404 at the v6.6.0 tag). It was merged into `iam-role`, which now takes
# `enable_oidc = true` plus `oidc_provider_urls` / `oidc_subjects`. We use `iam-role`.
module "iam_role" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role"
  version = "~> 6.6"

  name            = var.role_name
  use_name_prefix = false

  enable_oidc        = true
  oidc_provider_urls = [var.oidc_provider_url]
  oidc_subjects      = var.oidc_subjects

  policies = var.policies

  tags = var.tags
}
