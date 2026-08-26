"""Dublê de ``requests.Session`` para testar sem tocar em portal real."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import pytest


@dataclass
class FakeResponse:
    """Resposta mínima com a superfície que o client consome."""

    status_code: int = 200
    payload: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    text_body: str | None = None

    @property
    def text(self) -> str:
        if self.text_body is not None:
            return self.text_body
        return json.dumps(self.payload)

    def json(self) -> Any:
        if self.text_body is not None:
            raise ValueError("nao e JSON")
        return self.payload


class FakeSession:
    """Devolve respostas programadas e grava as chamadas recebidas."""

    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, json: Any = None, headers: Any = None, timeout: Any = None):
        self.calls.append({"url": url, "json": json, "headers": headers or {}})
        if not self._responses:
            raise AssertionError(f"chamada nao prevista para {url}")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def ok(result: Any, **headers: str) -> FakeResponse:
    """Resposta 200 no envelope padrão do Bitrix."""
    return FakeResponse(200, {"result": result}, dict(headers))


def api_error(code: str, description: str = "") -> FakeResponse:
    """Erro de negócio: HTTP 200 com ``error`` no corpo."""
    return FakeResponse(200, {"error": code, "error_description": description})


def limit(status: int, retry_after: str | None = None) -> FakeResponse:
    """429 (janela de tempo) ou 503 (limite operacional)."""
    headers = {"Retry-After": retry_after} if retry_after else {}
    return FakeResponse(status, {"error": "QUERY_LIMIT_EXCEEDED"}, headers)


@pytest.fixture
def no_sleep(monkeypatch):
    """Troca a espera real por um relógio falso que o ``sleep`` adianta.

    Só zerar ``time.sleep`` não basta: o balde furado decide quando liberar
    olhando ``time.monotonic``. Com o relógio parado e o sono anulado, o
    ``acquire`` gira para sempre. O relógio falso avança junto e mantém o
    teste em milissegundos sem mudar a lógica testada.

    Returns:
        Lista com a duração de cada espera, na ordem em que ocorreram.
    """
    clock = {"now": 10_000.0}
    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(time, "sleep", fake_sleep)
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    return slept
