from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import (
    Any,
    Callable,
    Iterable,
    Mapping,
    MutableMapping,
    Optional,
    Tuple,
    Union,
)
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from typing_extensions import Literal


class RequestStatus(str, Enum):
    NOVA = "nova"
    PENDENTE = "pendente"
    CONCLUIDA = "concluida"
    ERRO = "erro"
    CANCELADA = "cancelada"

class RequestEventStatus(str, Enum):
    SUCESSO = "erro"
    ERRO = "erro"

class TransactionStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIALLY_COMPLETED = "partially_completed"


class FinalTransactionStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    PARTIALLY_COMPLETED = "partially_completed"


class ExecutionCompletionStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    STOPPED = "stopped"


class TransactionItemStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"


REQUEST_STATUSES = tuple(status.value for status in RequestStatus)
REQUEST_EVENT_STATUSES = tuple(status.value for status in RequestEventStatus)
TRANSACTION_STATUSES = tuple(status.value for status in TransactionStatus)
FINAL_TRANSACTION_STATUSES = tuple(status.value for status in FinalTransactionStatus)
EXECUTION_COMPLETION_STATUSES = tuple(
    status.value for status in ExecutionCompletionStatus
)
TRANSACTION_ITEM_STATUSES = tuple(status.value for status in TransactionItemStatus)

TransactionStatusValue = Union[TransactionStatus, str]
FinalTransactionStatusValue = Union[FinalTransactionStatus, TransactionStatus, str]
ExecutionCompletionStatusValue = Union[ExecutionCompletionStatus, str]
TransactionItemStatusValue = Union[TransactionItemStatus, str]
JsonObject = dict[str, Any]
TransactionItemInput = Mapping[str, Any]

Sender = Callable[
    [str, JsonObject, Mapping[str, str], float],
    Tuple[int, Optional[JsonObject]],
]


class RunnerProgressError(RuntimeError):
    """Raised when transaction reporting fails in strict mode."""


@dataclass(frozen=True)
class RunnerProgressConfig:
    progress_url: Optional[str]
    execution_id: Optional[str]
    token: Optional[str]
    timeout: float = 5.0

    @classmethod
    def from_env(
        cls,
        env: Optional[Mapping[str, str]] = None,
        *,
        timeout: float = 5.0,
    ) -> "RunnerProgressConfig":
        source = os.environ if env is None else env
        progress_url = _clean_env_value(source.get("RUNNER_PROGRESS_URL"))
        execution_id = _clean_env_value(source.get("RUNNER_PROGRESS_EXECUTION_ID"))

        if execution_id is None:
            execution_id = _clean_env_value(source.get("RUNNER_EXECUTION_ID"))

        return cls(
            progress_url=progress_url,
            execution_id=execution_id,
            token=_clean_env_value(source.get("RUNNER_PROGRESS_TOKEN")),
            timeout=timeout,
        )

    @property
    def is_available(self) -> bool:
        return bool(self.progress_url and self.execution_id and self.token)

    def transaction_url(self) -> Optional[str]:
        if not self.progress_url or not self.execution_id:
            return None

        base_url = self.progress_url.rstrip("/")
        execution_id = quote(self.execution_id, safe="")
        return f"{base_url}/internal/executions/{execution_id}/transactions"

    def transaction_items_url(self, transaction_id: str) -> Optional[str]:
        transaction_url = self.transaction_url()
        if not transaction_url:
            return None

        encoded_transaction_id = quote(transaction_id, safe="")
        return f"{transaction_url}/{encoded_transaction_id}/items"

    def completion_status_url(self) -> Optional[str]:
        if not self.progress_url or not self.execution_id:
            return None

        base_url = self.progress_url.rstrip("/")
        execution_id = quote(self.execution_id, safe="")
        return f"{base_url}/internal/executions/{execution_id}/completion-status"


@dataclass(frozen=True)
class CompletionStatusReportResult:
    sent: bool
    noop: bool
    status_code: Optional[int] = None
    response: Optional[JsonObject] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.noop or (
            self.sent
            and self.error is None
            and self.status_code is not None
            and 200 <= self.status_code < 300
        )


@dataclass(frozen=True)
class TransactionReportResult:
    sent: bool
    noop: bool
    seq: Optional[int] = None
    status_code: Optional[int] = None
    response: Optional[JsonObject] = None
    error: Optional[str] = None
    transaction_id: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.noop or (
            self.sent
            and self.error is None
            and self.status_code is not None
            and 200 <= self.status_code < 300
        )


class RunnerProgressClient:
    def __init__(
        self,
        config: Optional[RunnerProgressConfig] = None,
        *,
        sender: Optional[Sender] = None,
        raise_on_error: bool = False,
    ) -> None:
        self.config = config or RunnerProgressConfig.from_env()
        self.raise_on_error = raise_on_error
        self._sender = sender or _send_json
        self._lock = threading.Lock()
        self._next_seq = 0

    @property
    def is_available(self) -> bool:
        return self.config.is_available

    def request_completion_status(
        self,
        status: ExecutionCompletionStatusValue,
    ) -> CompletionStatusReportResult:
        status_value = _enum_value(
            status,
            ExecutionCompletionStatus,
            "completion status",
        )

        return self._emit_completion_status_to_url(
            self.config.completion_status_url(),
            {"status": status_value},
        )

    def set_completion_status(
        self,
        status: ExecutionCompletionStatusValue,
    ) -> CompletionStatusReportResult:
        return self.request_completion_status(status)

    def create_transaction(
        self,
        *,
        key: str,
        label: Optional[str] = None,
        request_id: Optional[str] = None,
        status: Optional[TransactionStatusValue] = None,
        message: Optional[str] = None,
        total_items: Optional[int] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        item: Optional[TransactionItemInput] = None,
        items: Optional[Iterable[TransactionItemInput]] = None,
    ) -> "Transaction":
        result = self.report_transaction(
            key=key,
            label=label,
            request_id=request_id,
            status=status,
            message=message,
            total_items=total_items,
            metadata=metadata,
            item=item,
            items=items,
        )

        return Transaction(
            client=self,
            key=key,
            label=label,
            request_id=request_id,
            total_items=total_items,
            metadata=metadata,
            transaction_id=result.transaction_id,
            creation_result=result,
        )

    def report_transaction(
        self,
        *,
        key: str,
        label: Optional[str] = None,
        request_id: Optional[str] = None,
        status: Optional[TransactionStatusValue] = None,
        message: Optional[str] = None,
        total_items: Optional[int] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        item: Optional[TransactionItemInput] = None,
        items: Optional[Iterable[TransactionItemInput]] = None,
    ) -> TransactionReportResult:
        payload = self._build_transaction_payload(
            key=key,
            label=label,
            request_id=request_id,
            status=status,
            message=message,
            total_items=total_items,
            metadata=metadata,
        )

        if item is not None:
            payload["item"] = self._build_transaction_item_payload_from(item)

        if items is not None:
            payload["items"] = self._build_transaction_items_payload(items)

        return self._emit_to_url(self.config.transaction_url(), payload)

    def finish_transaction(
        self,
        *,
        key: str,
        status: FinalTransactionStatusValue,
        label: Optional[str] = None,
        request_id: Optional[str] = None,
        message: Optional[str] = None,
        total_items: Optional[int] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> TransactionReportResult:
        final_status = _enum_value(
            status,
            FinalTransactionStatus,
            "finish_transaction status",
        )

        return self.report_transaction(
            key=key,
            label=label,
            request_id=request_id,
            status=final_status,
            message=message,
            total_items=total_items,
            metadata=metadata,
        )

    def add_transaction_item(
        self,
        *,
        transaction_id: str,
        status: TransactionItemStatusValue,
        message: Optional[str] = None,
        value: Any,
        service_item_code: Optional[str] = None,
    ) -> TransactionReportResult:
        return self.add_transaction_items(
            transaction_id=transaction_id,
            items=[
                {
                    "status": status,
                    "message": message,
                    "value": value,
                    "service_item_code": service_item_code,
                }
            ],
        )

    def add_transaction_items(
        self,
        *,
        transaction_id: str,
        items: Iterable[TransactionItemInput],
    ) -> TransactionReportResult:
        cleaned_transaction_id = transaction_id.strip()
        if not cleaned_transaction_id:
            return self._failure_without_seq("transaction_id must be non-empty.")

        payload: JsonObject = {
            "timestamp": _utc_timestamp(),
            "items": self._build_transaction_items_payload(items),
        }

        return self._emit_to_url(
            self.config.transaction_items_url(cleaned_transaction_id),
            payload,
        )

    def main_transaction(
        self,
        *,
        key: str,
        label: Optional[str] = None,
        request_id: Optional[str] = None,
        total_items: Optional[int] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> "Transaction":
        return Transaction(
            client=self,
            key=key,
            label=label,
            request_id=request_id,
            total_items=total_items,
            metadata=metadata,
        )

    def _build_transaction_payload(
        self,
        *,
        key: str,
        label: Optional[str],
        request_id: Optional[str],
        status: Optional[TransactionStatusValue],
        message: Optional[str],
        total_items: Optional[int],
        metadata: Optional[Mapping[str, Any]],
    ) -> JsonObject:
        transaction_key = key.strip()
        if not transaction_key:
            raise ValueError("key must be a non-empty string.")

        status_value = (
            _enum_value(status, TransactionStatus, "status")
            if status is not None
            else None
        )

        if total_items is not None and (
            not isinstance(total_items, int) or total_items < 0
        ):
            raise ValueError("total_items must be a non-negative integer.")

        payload: JsonObject = {
            "timestamp": _utc_timestamp(),
            "transactionKey": transaction_key,
        }

        _put_optional(payload, "request_id", request_id)
        _put_optional(payload, "transactionLabel", label)
        _put_optional(payload, "status", status_value)
        _put_optional(payload, "message", message)
        _put_optional(payload, "totalItems", total_items)
        _put_optional(
            payload,
            "metadata",
            dict(metadata) if metadata is not None else None,
        )

        return payload

    def _build_transaction_items_payload(
        self,
        items: Iterable[TransactionItemInput],
    ) -> list[JsonObject]:
        payload_items = [
            self._build_transaction_item_payload_from(item) for item in items
        ]

        if not payload_items:
            raise ValueError("items must contain at least one transaction item.")

        return payload_items

    def _build_transaction_item_payload_from(
        self,
        item: TransactionItemInput,
    ) -> JsonObject:
        service_item_code = item.get("service_item_code")
        if service_item_code is None:
            service_item_code = item.get("serviceItemCode")

        return self._build_transaction_item_payload(
            status=item["status"],
            message=item.get("message"),
            value=item["value"],
            service_item_code=service_item_code,
        )

    def _build_transaction_item_payload(
        self,
        *,
        status: TransactionItemStatusValue,
        message: Optional[str],
        value: Any,
        service_item_code: Optional[str] = None,
    ) -> JsonObject:
        status_value = _enum_value(status, TransactionItemStatus, "item status")
        if status_value == TransactionItemStatus.ERROR.value and (
            message is None or not message.strip()
        ):
            raise ValueError("item message is required when status is error.")
        _ensure_json_serializable(value, "item value")
        if service_item_code is not None and not isinstance(service_item_code, str):
            raise ValueError("item service_item_code must be a string.")

        payload: JsonObject = {
            "status": status_value,
            "value": value,
        }
        _put_optional(payload, "message", message)
        _put_optional(
            payload,
            "serviceItemCode",
            _clean_env_value(service_item_code),
        )
        return payload

    def _emit_to_url(
        self,
        url: Optional[str],
        payload: JsonObject,
    ) -> TransactionReportResult:
        if not self.config.is_available or url is None or self.config.token is None:
            return TransactionReportResult(sent=False, noop=True)

        seq = self._reserve_seq()
        payload_with_seq = {"seq": seq, **payload}
        headers = {
            "Authorization": f"Bearer {self.config.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "runner-python-sdk/0.2.0",
        }

        try:
            status_code, response = self._sender(
                url,
                payload_with_seq,
                headers,
                self.config.timeout,
            )
        except Exception as exc:  # noqa: BLE001 - strict mode re-raises below.
            return self._failure(seq, str(exc), exc)

        if status_code < 200 or status_code >= 300:
            message = _error_message_from_response(status_code, response)
            return self._failure(seq, message)

        return TransactionReportResult(
            sent=True,
            noop=False,
            seq=seq,
            status_code=status_code,
            response=response,
            transaction_id=_extract_transaction_id(response),
        )

    def _emit_completion_status_to_url(
        self,
        url: Optional[str],
        payload: JsonObject,
    ) -> CompletionStatusReportResult:
        if not self.config.is_available or url is None or self.config.token is None:
            return CompletionStatusReportResult(sent=False, noop=True)

        headers = {
            "Authorization": f"Bearer {self.config.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "runner-python-sdk/0.2.0",
        }

        try:
            status_code, response = self._sender(
                url,
                payload,
                headers,
                self.config.timeout,
            )
        except Exception as exc:  # noqa: BLE001 - strict mode re-raises below.
            return self._completion_status_failure(str(exc), exc)

        if status_code < 200 or status_code >= 300:
            message = _error_message_from_response(status_code, response)
            return self._completion_status_failure(message)

        return CompletionStatusReportResult(
            sent=True,
            noop=False,
            status_code=status_code,
            response=response,
        )

    def _reserve_seq(self) -> int:
        with self._lock:
            seq = self._next_seq
            self._next_seq += 1
            return seq

    def _failure_without_seq(self, message: str) -> TransactionReportResult:
        if self.raise_on_error:
            raise RunnerProgressError(message)

        return TransactionReportResult(
            sent=False,
            noop=False,
            error=message,
        )

    def _completion_status_failure(
        self,
        message: str,
        cause: Optional[BaseException] = None,
    ) -> CompletionStatusReportResult:
        if self.raise_on_error:
            raise RunnerProgressError(message) from cause

        return CompletionStatusReportResult(
            sent=False,
            noop=False,
            error=message,
        )

    def _failure(
        self,
        seq: int,
        message: str,
        cause: Optional[BaseException] = None,
    ) -> TransactionReportResult:
        if self.raise_on_error:
            raise RunnerProgressError(message) from cause

        return TransactionReportResult(
            sent=False,
            noop=False,
            seq=seq,
            error=message,
        )


class Transaction:
    def __init__(
        self,
        *,
        client: RunnerProgressClient,
        key: str,
        label: Optional[str],
        request_id: Optional[str] = None,
        total_items: Optional[int] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        transaction_id: Optional[str] = None,
        creation_result: Optional[TransactionReportResult] = None,
    ) -> None:
        self._client = client
        self.key = key
        self.label = label
        self.request_id = request_id
        self.total_items = total_items
        self.metadata = metadata
        self.id = transaction_id
        self.creation_result = creation_result

    def __enter__(self) -> "Transaction":
        self.report(status=TransactionStatus.RUNNING)
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        traceback: Any,
    ) -> Literal[False]:
        if exc is None:
            self.finish(status=FinalTransactionStatus.SUCCESS)
            return False

        error_message = str(exc)
        if not error_message and exc_type is not None:
            error_message = exc_type.__name__

        self.finish(
            status=FinalTransactionStatus.FAILED,
            message=error_message or "Transaction failed.",
        )
        return False

    def report(
        self,
        *,
        status: Optional[TransactionStatusValue] = TransactionStatus.RUNNING,
        message: Optional[str] = None,
        total_items: Optional[int] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> TransactionReportResult:
        result = self._client.report_transaction(
            key=self.key,
            label=self.label,
            request_id=self.request_id,
            status=status,
            message=message,
            total_items=self.total_items if total_items is None else total_items,
            metadata=self.metadata if metadata is None else metadata,
        )
        self._apply_result(result)
        return result

    def finish(
        self,
        *,
        status: FinalTransactionStatusValue,
        message: Optional[str] = None,
        total_items: Optional[int] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> TransactionReportResult:
        result = self._client.finish_transaction(
            key=self.key,
            label=self.label,
            request_id=self.request_id,
            status=status,
            message=message,
            total_items=self.total_items if total_items is None else total_items,
            metadata=self.metadata if metadata is None else metadata,
        )
        self._apply_result(result)
        return result

    def addItem(
        self,
        *,
        status: TransactionItemStatusValue,
        message: Optional[str] = None,
        value: Any,
        service_item_code: Optional[str] = None,
    ) -> TransactionReportResult:
        return self.add_item(
            status=status,
            message=message,
            value=value,
            service_item_code=service_item_code,
        )

    def add_item(
        self,
        *,
        status: TransactionItemStatusValue,
        message: Optional[str] = None,
        value: Any,
        service_item_code: Optional[str] = None,
    ) -> TransactionReportResult:
        if self.id is None:
            return self._missing_transaction_id_result()

        result = self._client.add_transaction_item(
            transaction_id=self.id,
            status=status,
            message=message,
            value=value,
            service_item_code=service_item_code,
        )
        self._apply_result(result)
        return result

    def addItems(
        self,
        items: Iterable[TransactionItemInput],
    ) -> TransactionReportResult:
        return self.add_items(items)

    def add_items(
        self,
        items: Iterable[TransactionItemInput],
    ) -> TransactionReportResult:
        if self.id is None:
            return self._missing_transaction_id_result()

        result = self._client.add_transaction_items(
            transaction_id=self.id,
            items=items,
        )
        self._apply_result(result)
        return result

    def _apply_result(self, result: TransactionReportResult) -> None:
        if result.transaction_id:
            self.id = result.transaction_id

    def _missing_transaction_id_result(self) -> TransactionReportResult:
        if not self._client.is_available:
            return TransactionReportResult(sent=False, noop=True)

        return self._client._failure_without_seq(
            "Transaction id is required before adding transaction items."
        )


_default_client: Optional[RunnerProgressClient] = None
_default_client_lock = threading.Lock()


def get_default_client() -> RunnerProgressClient:
    global _default_client

    with _default_client_lock:
        if _default_client is None:
            _default_client = RunnerProgressClient()

        return _default_client


def request_completion_status(
    status: ExecutionCompletionStatusValue,
) -> CompletionStatusReportResult:
    return get_default_client().request_completion_status(status)


def set_completion_status(
    status: ExecutionCompletionStatusValue,
) -> CompletionStatusReportResult:
    return get_default_client().set_completion_status(status)


def create_transaction(
    *,
    key: str,
    label: Optional[str] = None,
    request_id: Optional[str] = None,
    status: Optional[TransactionStatusValue] = None,
    message: Optional[str] = None,
    total_items: Optional[int] = None,
    metadata: Optional[Mapping[str, Any]] = None,
    item: Optional[TransactionItemInput] = None,
    items: Optional[Iterable[TransactionItemInput]] = None,
) -> Transaction:
    return get_default_client().create_transaction(
        key=key,
        label=label,
        request_id=request_id,
        status=status,
        message=message,
        total_items=total_items,
        metadata=metadata,
        item=item,
        items=items,
    )


def report_transaction(
    *,
    key: str,
    label: Optional[str] = None,
    request_id: Optional[str] = None,
    status: Optional[TransactionStatusValue] = None,
    message: Optional[str] = None,
    total_items: Optional[int] = None,
    metadata: Optional[Mapping[str, Any]] = None,
    item: Optional[TransactionItemInput] = None,
    items: Optional[Iterable[TransactionItemInput]] = None,
) -> TransactionReportResult:
    return get_default_client().report_transaction(
        key=key,
        label=label,
        request_id=request_id,
        status=status,
        message=message,
        total_items=total_items,
        metadata=metadata,
        item=item,
        items=items,
    )


def finish_transaction(
    *,
    key: str,
    status: FinalTransactionStatusValue,
    label: Optional[str] = None,
    request_id: Optional[str] = None,
    message: Optional[str] = None,
    total_items: Optional[int] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> TransactionReportResult:
    return get_default_client().finish_transaction(
        key=key,
        status=status,
        label=label,
        request_id=request_id,
        message=message,
        total_items=total_items,
        metadata=metadata,
    )


def main_transaction(
    *,
    key: str,
    label: Optional[str] = None,
    request_id: Optional[str] = None,
    total_items: Optional[int] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> Transaction:
    return get_default_client().main_transaction(
        key=key,
        label=label,
        request_id=request_id,
        total_items=total_items,
        metadata=metadata,
    )


def is_available() -> bool:
    return get_default_client().is_available


def _clean_env_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None

    cleaned = value.strip()
    return cleaned or None


def _put_optional(
    payload: MutableMapping[str, Any],
    key: str,
    value: Any,
) -> None:
    if value is not None:
        payload[key] = value


def _enum_value(value: Any, enum_type: type[Enum], field_name: str) -> str:
    if isinstance(value, enum_type):
        return str(value.value)

    if isinstance(value, str):
        allowed_values = {str(item.value) for item in enum_type}
        if value in allowed_values:
            return value

    allowed = ", ".join(str(item.value) for item in enum_type)
    raise ValueError(f"{field_name} must be one of: {allowed}")


def _ensure_json_serializable(value: Any, field_name: str) -> None:
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be JSON serializable.") from exc


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _error_message_from_response(
    status_code: int,
    response: Optional[JsonObject],
) -> str:
    if not response:
        return f"Runner progress endpoint returned HTTP {status_code}."

    error = response.get("error")
    message = response.get("message")
    details = " - ".join(str(part) for part in (error, message) if part)

    if details:
        return f"Runner progress endpoint returned HTTP {status_code}: {details}"

    return f"Runner progress endpoint returned HTTP {status_code}."


def _extract_transaction_id(response: Optional[JsonObject]) -> Optional[str]:
    if not response:
        return None

    transaction = response.get("transaction")
    if not isinstance(transaction, Mapping):
        return None

    transaction_id = transaction.get("id")
    return transaction_id if isinstance(transaction_id, str) else None


def _send_json(
    url: str,
    payload: JsonObject,
    headers: Mapping[str, str],
    timeout: float,
) -> Tuple[int, Optional[JsonObject]]:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers=dict(headers),
        method="POST",
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, _decode_response(response.read())
    except HTTPError as exc:
        body = exc.read()
        return exc.code, _decode_response(body)
    except URLError as exc:
        raise RunnerProgressError(str(exc.reason)) from exc


def _decode_response(body: Union[bytes, bytearray]) -> Optional[JsonObject]:
    if not body:
        return None

    decoded = body.decode("utf-8")
    if not decoded.strip():
        return None

    parsed = json.loads(decoded)
    return parsed if isinstance(parsed, dict) else {"value": parsed}
