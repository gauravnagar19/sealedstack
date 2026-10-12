# compute_aws (spec section 4): an EC2 instance and a stateful security group.

locals {
  is_ami_id = startswith(var.image, "ami-")

  # Resolve a human-friendly AMI name pattern to a concrete, region-specific id.
  ami = local.is_ami_id ? var.image : data.aws_ami.this[0].id
}

data "aws_ami" "this" {
  count       = local.is_ami_id ? 0 : 1
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = [var.image]
  }
  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_key_pair" "this" {
  key_name   = var.name
  public_key = var.ssh_public_key
  tags       = var.tags
}

resource "aws_security_group" "this" {
  name        = "ss-${var.name}"
  description = "sealedstack ${var.name}: mesh + icmp in, all egress"
  tags        = var.tags

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "this" {
  for_each = { for index, rule in var.inbound_rules : index => rule }

  security_group_id = aws_security_group.this.id
  ip_protocol       = each.value.ip_protocol
  from_port         = each.value.from_port
  to_port           = each.value.to_port
  cidr_ipv4         = each.value.cidr_ipv4
  description       = each.value.description
}

# Terraform removes the default allow-all egress rule when managing a security
# group, so egress is re-opened explicitly.
resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.this.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
  description       = "All egress"
}

resource "aws_instance" "this" {
  ami                         = local.ami
  instance_type               = var.server_type
  availability_zone           = var.location != "" ? var.location : null
  key_name                    = aws_key_pair.this.key_name
  vpc_security_group_ids      = [aws_security_group.this.id]
  associate_public_ip_address = true
  tags                        = merge(var.tags, { Name = var.name })
}
