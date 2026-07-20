terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # >= 6.50.0: AGUI became a native server_protocol enum on
      # aws_bedrockagentcore_agent_runtime. The supervisor's
      # protocol_configuration relies on it.
      version = ">= 6.50.0"
    }
  }
}
