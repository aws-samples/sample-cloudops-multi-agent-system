terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # >= 6.50.0 is load-bearing for TWO aws_bedrockagentcore_agent_runtime
      # features the supervisor relies on:
      #   - AGUI as a native server_protocol enum (protocol_configuration)
      #   - request_header_configuration (Authorization forwarding, which the
      #     JWT-derived actor_id in agui_server.py depends on)
      version = ">= 6.50.0"
    }
  }
}
