resource "aws_cloudfront_distribution" "this" {
  enabled             = true
  is_ipv6_enabled     = true
  comment             = "${var.name}-distribution"
  default_root_object = ""
  price_class         = var.price_class
  
  origin {
    domain_name = var.alb_domain_name
    origin_id   = "ALB"
    
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }
  
  # Default cache behavior for API requests
  default_cache_behavior {
    allowed_methods  = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods   = ["GET", "HEAD", "OPTIONS"]
    target_origin_id = "ALB"
    
    forwarded_values {
      query_string = true
      headers      = ["Authorization", "Host", "Origin"]
      
      cookies {
        forward = "all"
      }
    }
    
    viewer_protocol_policy = "redirect-to-https"
    min_ttl                = 0
    default_ttl            = 0
    max_ttl                = 0
    compress               = true
  }
  
  # Use the provided certificate if custom domain is used
  dynamic "viewer_certificate" {
    for_each = var.certificate_arn != "" && var.custom_domain != "" ? [1] : []
    content {
      acm_certificate_arn = var.certificate_arn
      ssl_support_method  = "sni-only"
      minimum_protocol_version = "TLSv1.2_2021"
    }
  }
  
  # Use CloudFront default certificate if no custom domain
  dynamic "viewer_certificate" {
    for_each = var.certificate_arn == "" || var.custom_domain == "" ? [1] : []
    content {
      cloudfront_default_certificate = true
    }
  }
  
  # Add aliases only if custom domain is provided
  aliases = var.custom_domain != "" ? [var.custom_domain] : []
  
  # Associate WAF Web ACL if provided - commented out due to regional WAF scope issue
  # CloudFront requires global WAF WebACLs, but the current WAF is regional
  # web_acl_id = var.wafv2_acl_arn
  
  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  # Set to true to enable logging (can be set up later)
  # logging_config {
  #   include_cookies = false
  #   bucket          = "logs-bucket-domain"
  #   prefix          = "cloudfront-logs/"
  # }

  tags = {
    Name = "${var.name}-cloudfront-distribution"
  }
}
