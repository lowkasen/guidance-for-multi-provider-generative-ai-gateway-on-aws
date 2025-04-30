variable "name" {
  description = "Standard name to be used as prefix on all resources."
  type        = string
}

variable "alb_domain_name" {
  description = "Domain name of the ALB"
  type        = string
}

variable "alb_zone_id" {
  description = "Zone ID of the ALB"
  type        = string
}

variable "certificate_arn" {
  description = "ARN of the ACM certificate"
  type        = string
  default     = ""
}

variable "custom_domain" {
  description = "Custom domain for CloudFront distribution"
  type        = string
  default     = ""
}

variable "price_class" {
  description = "Price class for CloudFront distribution"
  type        = string
  default     = "PriceClass_100"
  validation {
    condition     = contains(["PriceClass_100", "PriceClass_200", "PriceClass_All"], var.price_class)
    error_message = "Price class must be one of PriceClass_100, PriceClass_200, or PriceClass_All."
  }
}

variable "wafv2_acl_arn" {
  description = "ARN of the WAFv2 ACL for CloudFront"
  type        = string
  default     = ""
}
