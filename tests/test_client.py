from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Optional

import pytest

from runner import (
    FinalTransactionStatus,
    RunnerProgressClient,
    RunnerProgressConfig,
    TransactionItemStatus,
    TransactionStatus,
)


class RecordingSender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], Mapping[str, str], float]] = []

    def __call__(
        self,
        url: str,
        payload: dict[str, Any],
        headers: Mapping[str, str],
        timeout: float,
    ) -> tuple[int, Optional[dict[str, Any]]]:
        self.calls.append((url, payload, headers, timeout))
        transaction_id = "transaction-1"
        return 202, {
            "status": "accepted",
            "seq": payload["seq"],
            "transaction": {
                "id": transaction_id,
                "executionId": "execution-1",
                "requestId": payload.get("request_id"),
                "transactionKey": payload.get("transactionKey", "invoice_collection"),
                "items": payload.get("items")
                or ([payload["item"]] if "item" in payload else []),
            },
        }


def test_config_reads_runner_environment() -> None:
    config = RunnerProgressConfig.from_env(
        {
            "RUNNER_PROGRESS_URL": " http://127.0.0.1:32123/ ",
            "RUNNER_PROGRESS_EXECUTION_ID": " execution-1 ",
            "RUNNER_PROGRESS_TOKEN": " token-1 ",
        }
    )

    assert config.is_available is True
    assert config.progress_url == "http://127.0.0.1:32123/"
    assert config.execution_id == "execution-1"
    assert config.token == "token-1"
    assert (
        config.transaction_url()
        == "http://127.0.0.1:32123/internal/executions/execution-1/transactions"
    )


def test_config_falls_back_to_legacy_execution_id_env_name() -> None:
    config = RunnerProgressConfig.from_env(
        {
            "RUNNER_PROGRESS_URL": "http://127.0.0.1:32123",
            "RUNNER_EXECUTION_ID": "execution-legacy",
            "RUNNER_PROGRESS_TOKEN": "token-1",
        }
    )

    assert config.is_available is True
    assert config.execution_id == "execution-legacy"


def test_create_transaction_serializes_payload_headers_and_request_id() -> None:
    sender = RecordingSender()
    client = RunnerProgressClient(
        RunnerProgressConfig(
            progress_url="http://127.0.0.1:32123",
            execution_id="execution/with space",
            token="token-1",
            timeout=2.5,
        ),
        sender=sender,
    )

    transaction = client.create_transaction(
        key=" invoice_collection ",
        label="Coleta de notas fiscais",
        request_id="request-123",
        status=TransactionStatus.RUNNING,
        message="Coletando",
        total_items=100,
        metadata={"source": "sefaz"},
    )

    assert transaction.id == "transaction-1"
    assert transaction.creation_result is not None
    assert transaction.creation_result.ok is True
    assert transaction.creation_result.seq == 0

    url, payload, headers, timeout = sender.calls[0]
    assert (
        url
        == "http://127.0.0.1:32123/internal/executions/"
        "execution%2Fwith%20space/transactions"
    )
    assert headers["Authorization"] == "Bearer token-1"
    assert "X-Runner-Progress-Token" not in headers
    assert timeout == 2.5
    assert payload["seq"] == 0
    assert datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))
    assert payload["request_id"] == "request-123"
    assert payload["transactionKey"] == "invoice_collection"
    assert payload["transactionLabel"] == "Coleta de notas fiscais"
    assert payload["status"] == "running"
    assert payload["message"] == "Coletando"
    assert payload["totalItems"] == 100
    assert payload["metadata"] == {"source": "sefaz"}


def test_create_transaction_can_send_initial_item() -> None:
    sender = RecordingSender()
    client = RunnerProgressClient(
        RunnerProgressConfig(
            progress_url="http://127.0.0.1:32123",
            execution_id="execution-1",
            token="token-1",
        ),
        sender=sender,
    )

    client.create_transaction(
        key="invoice_collection",
        item={
            "status": TransactionItemStatus.PROCESSING,
            "value": {"invoiceNumber": "NF-001"},
        },
    )

    assert sender.calls[0][1]["item"] == {
        "status": "processing",
        "value": {"invoiceNumber": "NF-001"},
    }


def test_transaction_add_item_posts_to_items_endpoint() -> None:
    sender = RecordingSender()
    client = RunnerProgressClient(
        RunnerProgressConfig(
            progress_url="http://127.0.0.1:32123",
            execution_id="execution-1",
            token="token-1",
        ),
        sender=sender,
    )
    transaction = client.create_transaction(key="invoice_collection")

    result = transaction.addItem(
        status=TransactionItemStatus.SUCCESS,
        value={"invoiceNumber": "NF-001"},
    )

    assert result.ok is True
    assert result.seq == 1
    url, payload, _headers, _timeout = sender.calls[1]
    assert (
        url
        == "http://127.0.0.1:32123/internal/executions/"
        "execution-1/transactions/transaction-1/items"
    )
    assert payload["items"] == [
        {
            "status": "success",
            "value": {"invoiceNumber": "NF-001"},
        }
    ]


def test_transaction_add_items_sends_public_item_status_values() -> None:
    sender = RecordingSender()
    client = RunnerProgressClient(
        RunnerProgressConfig(
            progress_url="http://127.0.0.1:32123",
            execution_id="execution-1",
            token="token-1",
        ),
        sender=sender,
    )
    transaction = client.create_transaction(key="invoice_collection")

    transaction.addItems(
        [
            {
                "status": TransactionItemStatus.SUCCESS,
                "value": {"invoiceNumber": "NF-001"},
            },
            {
                "status": TransactionItemStatus.ERROR,
                "message": "XML ausente",
                "value": {"invoiceNumber": "NF-002", "reason": "missing_xml"},
            },
        ]
    )

    assert sender.calls[1][1]["items"] == [
        {
            "status": "success",
            "value": {"invoiceNumber": "NF-001"},
        },
        {
            "status": "error",
            "message": "XML ausente",
            "value": {"invoiceNumber": "NF-002", "reason": "missing_xml"},
        },
    ]


def test_sequence_increments_monotonically_per_client() -> None:
    sender = RecordingSender()
    client = RunnerProgressClient(
        RunnerProgressConfig(
            progress_url="http://127.0.0.1:32123",
            execution_id="execution-1",
            token="token-1",
        ),
        sender=sender,
    )

    transaction = client.create_transaction(
        key="invoice_collection",
        status=TransactionStatus.RUNNING,
    )
    transaction.addItem(
        status=TransactionItemStatus.SUCCESS,
        value={"invoiceNumber": "NF-001"},
    )
    transaction.finish(status=FinalTransactionStatus.SUCCESS)

    assert [call[1]["seq"] for call in sender.calls] == [0, 1, 2]


def test_missing_runner_environment_is_safe_noop() -> None:
    sender = RecordingSender()
    client = RunnerProgressClient(
        RunnerProgressConfig.from_env({}),
        sender=sender,
    )

    transaction = client.create_transaction(
        key="invoice_collection",
        status=TransactionStatus.RUNNING,
    )
    result = transaction.addItem(
        status=TransactionItemStatus.SUCCESS,
        value={"invoiceNumber": "NF-001"},
    )

    assert transaction.creation_result is not None
    assert transaction.creation_result.ok is True
    assert transaction.creation_result.noop is True
    assert result.ok is True
    assert result.noop is True
    assert sender.calls == []


def test_finish_transaction_rejects_non_final_status() -> None:
    client = RunnerProgressClient(RunnerProgressConfig.from_env({}))

    with pytest.raises(ValueError, match="finish_transaction status"):
        client.finish_transaction(key="invoice_collection", status="running")


def test_item_status_rejects_values_outside_item_enum() -> None:
    client = RunnerProgressClient(RunnerProgressConfig.from_env({}))

    with pytest.raises(ValueError, match="item status"):
        client.create_transaction(
            key="invoice_collection",
            item={"status": "failed", "value": {"invoiceNumber": "NF-001"}},
        )


def test_item_status_accepts_processing() -> None:
    sender = RecordingSender()
    client = RunnerProgressClient(
        RunnerProgressConfig(
            progress_url="http://127.0.0.1:32123",
            execution_id="execution-1",
            token="token-1",
        ),
        sender=sender,
    )

    transaction = client.create_transaction(key="invoice_collection")
    transaction.addItem(
        status=TransactionItemStatus.PROCESSING,
        value={"invoiceNumber": "NF-001"},
    )

    assert sender.calls[1][1]["items"] == [
        {
            "status": "processing",
            "value": {"invoiceNumber": "NF-001"},
        }
    ]


def test_error_item_requires_message() -> None:
    client = RunnerProgressClient(RunnerProgressConfig.from_env({}))

    with pytest.raises(ValueError, match="item message"):
        client.create_transaction(
            key="invoice_collection",
            item={
                "status": TransactionItemStatus.ERROR,
                "value": {"invoiceNumber": "NF-001"},
            },
        )


def test_item_value_must_be_json_serializable() -> None:
    client = RunnerProgressClient(RunnerProgressConfig.from_env({}))

    with pytest.raises(ValueError, match="item value"):
        client.create_transaction(
            key="invoice_collection",
            item={"status": TransactionItemStatus.SUCCESS, "value": object()},
        )


def test_main_transaction_reports_running_then_success() -> None:
    sender = RecordingSender()
    client = RunnerProgressClient(
        RunnerProgressConfig(
            progress_url="http://127.0.0.1:32123",
            execution_id="execution-1",
            token="token-1",
        ),
        sender=sender,
    )

    with client.main_transaction(
        key="invoice_collection",
        label="Coleta de notas fiscais",
        total_items=100,
    ) as transaction:
        assert transaction.id == "transaction-1"
        transaction.report(message="Metade das notas processadas")

    assert [call[1]["status"] for call in sender.calls] == [
        "running",
        "running",
        "success",
    ]
    assert [call[1]["seq"] for call in sender.calls] == [0, 1, 2]
