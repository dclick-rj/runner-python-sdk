# Runner Python SDK

SDK Python oficial para reportar transações de execução de automações do Runner.

Automações executadas pelo Runner recebem a configuração de progresso por meio
de variáveis de ambiente. Este pacote lê essas variáveis, envia atualizações de
transação tipadas para o endpoint local do Runner e se torna um no-op seguro
quando o script roda fora do Runner.

## Instalação

Depois que a primeira versão pública for publicada:

```bash
pip install runner-python-sdk
```

Com `uv`:

```bash
uv add runner-python-sdk
```

Até que exista uma versão no PyPI, o pacote também pode ser instalado a partir
do repositório público:

```bash
pip install "runner-python-sdk @ git+https://github.com/dclick-rj/runner-python-sdk.git"
uv add "runner-python-sdk @ git+https://github.com/dclick-rj/runner-python-sdk.git"
```

## Uso

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
    status=runner.TransactionItemStatus.SUCCESS,
    value={"invoiceNumber": "NF-001"},
)

transaction.addItems(
    [
        {
            "status": runner.TransactionItemStatus.SUCCESS,
            "value": {"invoiceNumber": "NF-002"},
            "service_item_code": "NFE",
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

Para o caso comum de uma transação principal por execução de automação:

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

O gerenciador de contexto reporta `running` ao entrar, `success` em uma saída
normal e `failed` se o bloco levantar uma exceção.

Os valores de status são expostos como enums:

- `runner.TransactionStatus`
- `runner.FinalTransactionStatus`
- `runner.TransactionItemStatus`
- `runner.ExecutionCompletionStatus`


### Status final da execucao

A automacao tambem pode solicitar qual status o Runner deve aplicar quando a
execucao terminar:

```py
import runner

runner.set_completion_status(runner.ExecutionCompletionStatus.ERROR)
```

Tambem e possivel chamar `runner.request_completion_status("success")`,
`"error"` ou `"stopped"`. Essa chamada usa o mesmo `RUNNER_PROGRESS_TOKEN` e
nao consome a sequencia de progresso das transacoes.

Itens de transação aceitam apenas `TransactionItemStatus.SUCCESS` ou
`TransactionItemStatus.ERROR` na API pública do SDK.

Um item pode informar o item de serviço que atendeu com `service_item_code`,
tanto em `addItem(..., service_item_code="NFE")` quanto nos dicionários de
`addItems`. O campo é opcional; com ele, o relatório de uso de serviço atribui o
item de transação a esse item de serviço em vez de usar a chave da transação.

## Ambiente do Runner

O Runner injeta estas variáveis nos processos de automação:

- `RUNNER_PROGRESS_URL`
- `RUNNER_PROGRESS_EXECUTION_ID` ou `RUNNER_EXECUTION_ID`
- `RUNNER_PROGRESS_TOKEN`

O SDK envia atualizações de transação para:

```text
POST {RUNNER_PROGRESS_URL}/internal/executions/{RUNNER_EXECUTION_ID}/transactions
```

Itens de transação são enviados para:

```text
POST {RUNNER_PROGRESS_URL}/internal/executions/{RUNNER_EXECUTION_ID}/transactions/{transaction_id}/items
```

Solicitacoes de status final da execucao sao enviadas para:

```text
POST {RUNNER_PROGRESS_URL}/internal/executions/{RUNNER_EXECUTION_ID}/completion-status
```

O token é enviado como `Authorization: Bearer ...`.

Quando alguma variável obrigatória está ausente ou em branco, as chamadas
retornam um resultado no-op e não fazem requisições HTTP.

## Desenvolvimento

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest
```

Gerar uma distribuição:

```bash
python -m build
```

A publicação está configurada em `.github/workflows/publish.yml` para PyPI
Trusted Publishing quando uma release do GitHub for publicada.

