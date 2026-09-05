# HTTPS for the API without owning a domain.
#
# ACM will not issue a certificate for *.elb.amazonaws.com -- AWS owns that
# domain and public certs are only issued for domains you can prove you control.
# CloudFront supplies a free certificate on its own *.cloudfront.net domain, so
# putting a distribution in front of the ALB gives the browser HTTPS with no
# registration and no cost.
#
# The trade: the CloudFront -> ALB hop is HTTP. It stays inside the AWS network,
# but it is unencrypted, so the ALB is locked to CloudFront two ways below.
# Register a domain and attach an ACM certificate to the ALB directly when the
# data warrants end-to-end TLS.

resource "random_password" "origin_verify" {
  length  = 40
  special = false
}

resource "aws_cloudfront_distribution" "api" {
  enabled     = true
  comment     = "${var.project_name} API"
  price_class = "PriceClass_100"

  origin {
    domain_name = aws_lb.api.dns_name
    origin_id   = "api-alb"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
      # Answers take seconds: routing, retrieval, generation and verification.
      # The default 30s would cut off legitimate responses.
      origin_read_timeout      = 60
      origin_keepalive_timeout = 60
    }

    # Proves a request came through CloudFront. Without it the ALB's public DNS
    # would still serve traffic directly over plain HTTP, defeating the point.
    custom_header {
      name  = "X-Origin-Verify"
      value = random_password.origin_verify.result
    }
  }

  default_cache_behavior {
    target_origin_id       = "api-alb"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    # CachingDisabled: answers are per-tenant and per-conversation, and the SSE
    # endpoint must not be buffered or replayed.
    cache_policy_id = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
    # AllViewerExceptHostHeader: forwards auth and tenant headers but lets the
    # ALB keep its own Host, which an ALB origin requires.
    origin_request_policy_id = "b689b0a8-53d0-40ab-baf2-68738e2966ac"
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = { Name = "${var.project_name}-api-cdn" }
}

# Lock 1: only requests carrying the shared secret are forwarded.
resource "aws_lb_listener_rule" "cloudfront_only" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 100

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }

  condition {
    http_header {
      http_header_name = "X-Origin-Verify"
      values           = [random_password.origin_verify.result]
    }
  }
}

# Anything reaching the ALB directly is refused.
resource "aws_lb_listener_rule" "block_direct" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 200

  action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      message_body = "Direct access denied. Use the CloudFront endpoint."
      status_code  = "403"
    }
  }

  condition {
    path_pattern {
      values = ["/*"]
    }
  }
}

# Lock 2: at the network layer, so a rule misconfiguration is not the only
# thing standing between the ALB and the internet.
data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_vpc_security_group_ingress_rule" "alb_from_cloudfront_http" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTP from CloudFront edge locations only"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
  prefix_list_id    = data.aws_ec2_managed_prefix_list.cloudfront.id
}

output "api_cdn_url" {
  description = "HTTPS API endpoint. Point the UI at this."
  value       = "https://${aws_cloudfront_distribution.api.domain_name}"
}

output "api_cdn_distribution_id" {
  value = aws_cloudfront_distribution.api.id
}
