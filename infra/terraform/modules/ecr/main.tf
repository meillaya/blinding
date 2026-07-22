# ECR repositories for the InferenceOps container images.
#
# Creates one aws_ecr_repository per entry in var.repository_names (default:
# gateway, model-server, worker — the three images the platform ships):
#   * image_tag_mutability = "IMMUTABLE" → tags cannot be overwritten → reproducible deploys
#   * image_scanning_configuration { scan_on_push = true } → CVE scan on every push
#
# The final repository name is "${var.name_prefix}${name}" so environments can
# namespace repositories (e.g. prefix "inferenceops-dev-") without changing the
# logical keys used in the output maps.

resource "aws_ecr_repository" "this" {
  for_each = toset(var.repository_names)

  name                 = "${var.name_prefix}${each.value}"
  image_tag_mutability = var.image_tag_mutability

  image_scanning_configuration {
    scan_on_push = var.scan_on_push
  }

  tags = var.tags
}
