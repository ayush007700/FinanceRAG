# Next.js UI: S3 + CloudFront.
#
# The app is client-rendered (it calls the API through NEXT_PUBLIC_API_URL), so
# no Node server is required. A static bucket behind a CDN costs a few dollars a
# month against ~$8 for a second ECS service, and has nothing to patch.

resource "aws_s3_bucket" "ui" {
  bucket        = "${var.project_name}-ui-${data.aws_caller_identity.current.account_id}"
  force_destroy = true # static build artefacts; always reproducible from source

  tags = { Name = "${var.project_name}-ui" }
}

# The bucket is never public. CloudFront reaches it through Origin Access
# Control, so the only way to the objects is via the distribution.
resource "aws_s3_bucket_public_access_block" "ui" {
  bucket                  = aws_s3_bucket.ui.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "ui" {
  bucket = aws_s3_bucket.ui.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_cloudfront_origin_access_control" "ui" {
  name                              = "${var.project_name}-ui-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "ui" {
  enabled             = true
  default_root_object = "index.html"
  comment             = "${var.project_name} UI"
  price_class         = "PriceClass_100" # NA + EU edges only; cheapest tier

  origin {
    domain_name              = aws_s3_bucket.ui.bucket_regional_domain_name
    origin_id                = "ui-s3"
    origin_access_control_id = aws_cloudfront_origin_access_control.ui.id
  }

  default_cache_behavior {
    target_origin_id       = "ui-s3"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    # CachingOptimized (AWS managed).
    cache_policy_id = "658327ea-f89d-4fab-a63d-7e88639e58f6"
  }

  # A static export has no server to resolve unknown paths, so a client-side
  # route reloaded directly would 403 from S3. Both codes serve index.html and
  # let the router decide.
  custom_error_response {
    error_code         = 403
    response_code      = 200
    response_page_path = "/index.html"
  }

  custom_error_response {
    error_code         = 404
    response_code      = 200
    response_page_path = "/index.html"
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = { Name = "${var.project_name}-ui" }
}

# Only this distribution may read the bucket.
resource "aws_s3_bucket_policy" "ui" {
  bucket = aws_s3_bucket.ui.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.ui.arn}/*"
      Condition = {
        StringEquals = {
          "AWS:SourceArn" = aws_cloudfront_distribution.ui.arn
        }
      }
    }]
  })
}

# The deploy identity publishes the built UI and invalidates the CDN cache.
resource "aws_iam_user_policy" "github_actions_ui" {
  name = "${var.project_name}-gha-ui"
  user = aws_iam_user.github_actions.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.ui.arn, "${aws_s3_bucket.ui.arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["cloudfront:CreateInvalidation"]
        Resource = aws_cloudfront_distribution.ui.arn
      }
    ]
  })
}

output "ui_url" {
  description = "CloudFront URL for the Next.js UI"
  value       = "https://${aws_cloudfront_distribution.ui.domain_name}"
}

output "ui_bucket" {
  description = "S3 bucket the built UI is synced to"
  value       = aws_s3_bucket.ui.bucket
}

output "ui_distribution_id" {
  description = "CloudFront distribution id, for cache invalidation"
  value       = aws_cloudfront_distribution.ui.id
}
