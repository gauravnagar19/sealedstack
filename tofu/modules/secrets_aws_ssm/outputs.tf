output "parameter_arns" {
  value     = { for key, param in aws_ssm_parameter.this : key => param.arn }
  sensitive = true
}
