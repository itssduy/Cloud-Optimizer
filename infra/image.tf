# The image tag is a hash of the app code, so apply rebuilds and redeploys only when it changes.
locals {
  app_dir = "${path.module}/../app"
  app_files = sort([for f in setunion(
    fileset(local.app_dir, "cloud_optimizer/**"),
    fileset(local.app_dir, "cloud_optimizer_project/**"),
    toset(["Dockerfile", "pyproject.toml", "uv.lock", "manage.py"]),
  ) : f if !strcontains(f, "__pycache__")])
  image_tag = substr(sha1(join("", [for f in local.app_files : filesha1("${local.app_dir}/${f}")])), 0, 12)
  app_image = "${aws_ecr_repository.app.repository_url}:${local.image_tag}"
}

resource "terraform_data" "app_image" {
  triggers_replace = [local.app_image]

  provisioner "local-exec" {
    command = "${path.module}/scripts/push-image.sh"
    environment = {
      REPO    = aws_ecr_repository.app.repository_url
      TAG     = local.image_tag
      REGION  = var.aws_region
      APP_DIR = local.app_dir
    }
  }
}
