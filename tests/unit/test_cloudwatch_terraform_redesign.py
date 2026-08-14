from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_sqs_worker_and_dlq_wiring():
    text = (
        ROOT / "terraform" / "modules" / "custom" / "cloudwatch-collection" / "main.tf"
    ).read_text()
    assert "visibility_timeout_seconds = 960" in text
    assert "maxReceiveCount     = 3" in text
    assert "timeout          = 900" in text
    assert "batch_size       = 1" in text
    assert "aws_sqs_queue.dlq.arn" in text


def test_worker_can_search_resource_explorer_aggregator():
    text = (
        ROOT / "terraform" / "modules" / "custom" / "cloudwatch-collection" / "main.tf"
    ).read_text()
    assert '"resource-explorer-2:GetDefaultView"' in text
    assert '"resource-explorer-2:ListResources"' in text
    assert '"resource-explorer-2:Search"' in text


def test_tool_policy_uses_role_output_and_scoped_resources():
    text = (ROOT / "terraform" / "main.tf").read_text()
    assert 'module.lambda_tools["cloudwatch"].lambda_role_name' in text
    assert "module.cloudwatch_collection[0].table_arn" in text
    assert "module.cloudwatch_collection[0].coordinator_function_arn" in text
    assert '"${var.project_tag}-cloudwatch-tool-role"' not in text


def test_collector_can_be_disabled_and_is_selective():
    text = (ROOT / "terraform" / "main.tf").read_text()
    assert "var.enable_cloudwatch_coverage" in text
    assert 'contains(var.selected_tools, "cloudwatch")' in text
