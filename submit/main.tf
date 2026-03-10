# Anomaly Detection Pipeline — Terraform

terraform {
  required_version = ">= 1.3.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# Variables

variable "aws_region" {
  type        = string
  default     = "us-east-1"
  description = "AWS region to deploy resources into."
}

variable "ip_address" {
  type        = string
  default     = "98.244.81.22/32"
  description = "Your IP address in CIDR notation for SSH access on port 22."
}

variable "git_repo_url" {
  type        = string
  default     = "https://github.com/chasecha1/anomaly-detection"
  description = "URL of your forked anomaly-detection repository."
}

variable "key_pair_name" {
  type        = string
  default     = "ds5220-keypair"
  description = "Name of an existing EC2 key pair for SSH access."
}

# Data Sources

# always resolves to the latest Ubuntu 24.04 LTS AMI for the region.
data "aws_ssm_parameter" "ubuntu_ami" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

# Current AWS account ID — used to name the S3 bucket consistently
data "aws_caller_identity" "current" {}

# Locals

locals {
  # Match the CloudFormation bucket name
  bucket_name = "anomaly-detection-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
}

# S3 Bucket

resource "aws_s3_bucket" "anomaly_bucket" {
  bucket = local.bucket_name

  lifecycle {
    prevent_destroy = false
  }

  tags = {
    Name = local.bucket_name
  }
}

# S3 event notification — triggers SNS when a .csv lands under raw/
resource "aws_s3_bucket_notification" "anomaly_notification" {
  bucket = aws_s3_bucket.anomaly_bucket.id

  topic {
    topic_arn     = aws_sns_topic.anomaly_topic.arn
    events        = ["s3:ObjectCreated:*"]
    filter_prefix = "raw/"
    filter_suffix = ".csv"
  }

  depends_on = [aws_sns_topic_policy.anomaly_topic_policy]
}

# IAM Role, Policy, and Instance Profile

resource "aws_iam_role" "ec2_role" {
  name = "anomaly-detection-ec2-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_policy" "s3_access_policy" {
  name = "anomaly-detection-s3-policy"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:DeleteObject",
          "s3:ListBucket",
          "s3:GetBucketLocation",
          "s3:GetObjectVersion",
          "s3:DeleteObjectVersion"
        ]
        Resource = [
          aws_s3_bucket.anomaly_bucket.arn,
          "${aws_s3_bucket.anomaly_bucket.arn}/*"
        ]
      }
    ]
  })
}

# Attach the policy to the role
resource "aws_iam_role_policy_attachment" "ec2_s3_attach" {
  role       = aws_iam_role.ec2_role.name
  policy_arn = aws_iam_policy.s3_access_policy.arn
}

# Instance profile — wraps the role so EC2 can assume it
resource "aws_iam_instance_profile" "ec2_profile" {
  name = "anomaly-detection-profile"
  role = aws_iam_role.ec2_role.name
}

# Security Group

resource "aws_security_group" "anomaly_sg" {
  name        = "anomaly-detection-sg"
  description = "Allow SSH from my IP and API access on port 8000 from anywhere"

  # SSH — restricted to your IP
  ingress {
    description = "SSH access"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.ip_address]
  }

  # FastAPI / SNS webhook — open to the world
  ingress {
    description = "FastAPI / SNS webhook access"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Allow all outbound traffic
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "anomaly-detection-sg"
  }
}

# EC2 Instance

resource "aws_instance" "anomaly_instance" {
  ami                    = data.aws_ssm_parameter.ubuntu_ami.value
  instance_type          = "t3.micro"
  key_name               = var.key_pair_name
  iam_instance_profile   = aws_iam_instance_profile.ec2_profile.name
  vpc_security_group_ids = [aws_security_group.anomaly_sg.id]

  root_block_device {
    volume_size           = 16
    volume_type           = "gp3"
    delete_on_termination = true
  }

  # templatefile() renders the user data script with the bucket name injected,
  user_data = templatefile("${path.module}/userdata.sh", {
    bucket_name  = local.bucket_name
    git_repo_url = var.git_repo_url
  })

  tags = {
    Name = "anomaly-detection"
  }
}

# Elastic IP + Association

resource "aws_eip" "anomaly_eip" {
  domain = "vpc"

  tags = {
    Name = "anomaly-detection-eip"
  }
}

resource "aws_eip_association" "anomaly_eip_assoc" {
  instance_id   = aws_instance.anomaly_instance.id
  allocation_id = aws_eip.anomaly_eip.allocation_id
}

# SNS Topic and Policy

resource "aws_sns_topic" "anomaly_topic" {
  name = "ds5220-dp1"
}

# Grants S3 permission to publish to the SNS topic
resource "aws_sns_topic_policy" "anomaly_topic_policy" {
  arn = aws_sns_topic.anomaly_topic.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowS3Publish"
        Effect = "Allow"
        Principal = {
          Service = "s3.amazonaws.com"
        }
        Action   = "sns:Publish"
        Resource = aws_sns_topic.anomaly_topic.arn
        Condition = {
          ArnLike = {
            "aws:SourceArn" = "arn:aws:s3:::${local.bucket_name}"
          }
        }
      }
    ]
  })
}

# SNS HTTP subscription
resource "aws_sns_topic_subscription" "anomaly_http_sub" {
  topic_arn = aws_sns_topic.anomaly_topic.arn
  protocol  = "http"
  endpoint  = "http://${aws_eip.anomaly_eip.public_ip}:8000/notify"

  depends_on = [aws_eip_association.anomaly_eip_assoc]
}

# Outputs

output "bucket_name" {
  description = "S3 bucket used by the anomaly detection app"
  value       = aws_s3_bucket.anomaly_bucket.id
}

output "elastic_ip" {
  description = "Stable public IP address of the EC2 instance"
  value       = aws_eip.anomaly_eip.public_ip
}

output "instance_id" {
  description = "ID of the running EC2 instance"
  value       = aws_instance.anomaly_instance.id
}

output "sns_topic_arn" {
  description = "ARN of the ds5220-dp1 SNS topic"
  value       = aws_sns_topic.anomaly_topic.arn
}

output "api_base_url" {
  description = "Base URL for the FastAPI service"
  value       = "http://${aws_eip.anomaly_eip.public_ip}:8000"
}

output "ssh_command" {
  description = "SSH command to connect to the instance"
  value       = "ssh ubuntu@${aws_eip.anomaly_eip.public_ip}"
}
