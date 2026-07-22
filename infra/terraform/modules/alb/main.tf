# Internet-facing ALB for the InferenceOps inference tier.
#
# Plain resources (aws_lb + aws_lb_target_group + aws_lb_listener) rather than a
# registry wrapper: the surface is small, and keeping it explicit means the module
# has ZERO data sources — it plans cleanly against the offline moto endpoint
# (todo 24) and against real AWS identically.
#
# The ALB sits in the VPC's PUBLIC subnets (tagged kubernetes.io/role/elb=1 by the
# vpc module) and forwards to IP targets (the model-server pods, registered by the
# AWS Load Balancer Controller in the real-AWS path).

resource "aws_security_group" "alb" {
  name        = "${var.name}-alb"
  description = "Ingress to the ${var.name} internet-facing ALB"
  vpc_id      = var.vpc_id

  ingress {
    description = "Listener port from the internet"
    from_port   = var.listener_port
    to_port     = var.listener_port
    protocol    = "tcp"
    cidr_blocks = var.ingress_cidr_blocks
  }

  egress {
    description = "Forward to targets in the VPC"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "${var.name}-alb" })
}

resource "aws_lb" "this" {
  name               = var.name
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.subnet_ids

  drop_invalid_header_fields = true

  tags = merge(var.tags, { Name = var.name })
}

resource "aws_lb_target_group" "this" {
  name        = var.name
  port        = var.target_port
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    path                = var.health_check_path
    port                = "traffic-port"
    protocol            = "HTTP"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 30
    matcher             = "200"
  }

  tags = merge(var.tags, { Name = var.name })
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = var.listener_port
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.this.arn
  }

  tags = var.tags
}
