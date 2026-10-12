# secrets_aws_ssm (registry secrets.yaml): SecureString parameters in AWS SSM
# Parameter Store (free standard tier).

resource "aws_ssm_parameter" "this" {
  for_each = toset(var.keys)

  name  = "${var.prefix}/${each.key}"
  type  = "SecureString"
  value = var.values[each.key]
}
