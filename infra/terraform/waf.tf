/*
 * Web application firewall in front of both CloudFront distributions.
 *
 * Why it is needed: rate limiting in the API is per credential and runs in
 * the application, so it can only act on a request that has already reached
 * a task, been authenticated, and had its counter incremented. An
 * unauthenticated flood -- credential stuffing against /v1/ask, a bot
 * hammering /health -- never meets that limiter and costs Fargate CPU on
 * every request it rejects. WAF sits at the CloudFront edge, before the ALB,
 * before the task, and drops those requests where they are cheapest to drop.
 *
 * Why managed rule groups rather than hand-written rules: the common-threats
 * set is maintained by AWS against new CVE classes and evasion techniques.
 * A hand-written rule set is only as current as its last review.
 *
 * Why us-east-1: a WAF for CloudFront must live there. That is a CloudFront
 * constraint, not a choice; the aliased provider exists for this file.
 *
 * Cost: roughly $5/month per web ACL, $1 per rule group, $0.60 per million
 * requests. Off by default so the demo profile stays at its documented cost.
 */

variable "enable_waf" {
  type        = bool
  default     = false
  description = "Attach a WAF with AWS managed rule groups to both CloudFront distributions. ~$10/month."
}

variable "waf_rate_limit_per_5min" {
  type        = number
  default     = 2000
  description = "Requests per 5 minutes from one IP before WAF blocks it. Above anything a person produces; below a scripted flood."
}

resource "aws_wafv2_web_acl" "edge" {
  count    = var.enable_waf ? 1 : 0
  provider = aws.us_east_1

  name  = "${var.project_name}-edge"
  scope = "CLOUDFRONT"

  default_action {
    allow {}
  }

  # Per-IP rate limit at the edge. This is the layer the application limiter
  # cannot be: it acts before authentication, so a flood of bad credentials is
  # stopped here rather than counted in the task.
  rule {
    name     = "rate-limit-per-ip"
    priority = 1

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = var.waf_rate_limit_per_5min
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "RateLimitPerIp"
      sampled_requests_enabled   = true
    }
  }

  # Known-bad IPs: the Amazon threat intelligence list. Cheapest rule to
  # evaluate and the highest signal per request.
  rule {
    name     = "aws-ip-reputation"
    priority = 10

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesAmazonIpReputationList"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "IpReputation"
      sampled_requests_enabled   = true
    }
  }

  # OWASP-class threats: injection, path traversal, malformed requests.
  rule {
    name     = "aws-common"
    priority = 20

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesCommonRuleSet"

        # The body-size rule blocks requests over 8 KB. A question with an
        # attached image is larger than that by design, so it is counted
        # rather than blocked; the API's own input limit is the control.
        rule_action_override {
          name = "SizeRestrictions_BODY"
          action_to_use {
            count {}
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "CommonRuleSet"
      sampled_requests_enabled   = true
    }
  }

  # Request patterns known to target specific vulnerabilities -- Log4j-style
  # JNDI strings, for instance -- that the common set does not cover.
  rule {
    name     = "aws-known-bad-inputs"
    priority = 30

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "KnownBadInputs"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.project_name}-edge"
    sampled_requests_enabled   = true
  }
}

output "waf_web_acl_arn" {
  value = var.enable_waf ? aws_wafv2_web_acl.edge[0].arn : null
}
