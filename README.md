# Runner Python SDK

Official Python SDK for reporting Runner automation execution transactions.

Automations executed by Runner receive progress configuration through environment
variables. This package reads those variables, sends typed transaction updates to
the local Runner endpoint, and becomes a safe no-op when the script runs outside
Runner.

## Install

After the first public release is published:

```bash
pip install runner-python-sdk
```

With `uv`:

```bash
uv add runner-python-sdk
```

Until a PyPI release exists, the package can also be installed from the public
repository:

```bash
pip install "runner-python-sdk @ git+https://github.com/dclick-rj/runner-python-sdk.git"
uv add "runner-python-sdk @ git+https://github.com/dclick-rj/runner-python-sdk.git"
```

## Usage

```py
import runner

transaction = runner.create_transaction(
    key="invoice_collection",
    label="Coleta de notas fiscais",
    request_id="request-123",
    status=runner.TransactionStatus.RUNNING,
    total_items=100,
    metadata={"source": "sefaz"},
)

transaction.addItem(
    status=runner.TransactionItemStatus.PROCESSING,
    value={"invoiceNumber": "NF-001"},
)

transaction.addItems(
    [
        {
            "status": runner.TransactionItemStatus.SUCCESS,
            "value": {"invoiceNumber": "NF-002"},
        },
        {
            "status": runner.TransactionItemStatus.ERROR,
            "message": "XML ausente",
            "value": {"invoiceNumber": "NF-003", "reason": "missing_xml"},
        },
    ]
)

transaction.finish(
    status=runner.FinalTransactionStatus.PARTIALLY_COMPLETED,
    message="90 notas coletadas, 10 com falha",
)
```

For the common case of one main transaction per automation execution:

```py
import runner

with runner.main_transaction(
    key="invoice_collection",
    label="Coleta de notas fiscais",
    total_items=100,
) as transaction:
    transaction.report(message="Coletando notas fiscais")
    transaction.addItem(
        status=runner.TransactionItemStatus.SUCCESS,
        value={"invoiceNumber": "NF-001"},
    )
```

The context manager reports `running` on entry, `success` on normal exit, and
`failed` if the block raises an exception.

Status values are exposed as enums:

- `runner.TransactionStatus`
- `runner.FinalTransactionStatus`
- `runner.TransactionItemStatus`

Transaction items accept only `TransactionItemStatus.PROCESSING`,
`TransactionItemStatus.SUCCESS`, or `TransactionItemStatus.ERROR` in the SDK
public API.

## Runner environment

Runner injects these variables into automation processes:

- `RUNNER_PROGRESS_URL`
- `RUNNER_PROGRESS_EXECUTION_ID` or `RUNNER_EXECUTION_ID`
- `RUNNER_PROGRESS_TOKEN`

The SDK posts transaction updates to:

```text
POST {RUNNER_PROGRESS_URL}/internal/executions/{RUNNER_EXECUTION_ID}/transactions
```

Transaction items are posted to:

```text
POST {RUNNER_PROGRESS_URL}/internal/executions/{RUNNER_EXECUTION_ID}/transactions/{transaction_id}/items
```

The token is sent as `Authorization: Bearer ...`.

When any required variable is missing or blank, calls return a no-op result and
do not perform HTTP requests.

## Development

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest
```

Build a distribution:

```bash
python -m build
```

Publishing is configured in `.github/workflows/publish.yml` for PyPI Trusted
Publishing on GitHub release publication.
