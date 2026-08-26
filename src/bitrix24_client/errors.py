"""Exceções da camada REST do Bitrix24.

O Bitrix24 devolve erro em dois lugares diferentes e é fácil tratar só um:

* no HTTP status (``503`` estouro de limite operacional, ``429`` estouro de
  janela de tempo, ``401`` token expirado);
* no corpo JSON, com HTTP 200 e um campo ``error`` — típico de método
  inexistente, permissão faltando e parâmetro inválido.

Quem só olha ``response.raise_for_status()`` engole a segunda família inteira.
"""

from __future__ import annotations


class BitrixError(Exception):
    """Base de tudo que o client levanta."""


class BitrixAPIError(BitrixError):
    """Erro devolvido no corpo JSON, normalmente com HTTP 200."""

    def __init__(self, code: str, description: str, method: str, params: dict | None = None):
        self.code = code
        self.description = description
        self.method = method
        self.params = params or {}
        super().__init__(f"[{code}] {method}: {description}")


class BitrixAuthError(BitrixAPIError):
    """Webhook revogado ou access_token expirado (``expired_token``, ``NO_AUTH_FOUND``)."""


class BitrixRateLimitError(BitrixError):
    """Estouro de limite.

    ``kind`` distingue as duas naturezas, porque a estratégia de espera muda:

    * ``operating`` (HTTP 503) — o portal gastou o orçamento de operação.
      O balde vaza sozinho; esperar resolve.
    * ``rate`` (HTTP 429) — chamadas demais na janela de tempo. Costuma vir
      com ``Retry-After``; respeitar o header é obrigatório.
    """

    def __init__(self, kind: str, retry_after: float, method: str):
        self.kind = kind
        self.retry_after = retry_after
        self.method = method
        super().__init__(f"limite '{kind}' em {method}; aguardar {retry_after:.1f}s")


class BitrixTransportError(BitrixError):
    """Falha de rede/DNS/TLS ou 5xx que não é 503 de limite."""


#: Códigos que indicam token morto — nunca vale a pena repetir sem renovar.
AUTH_ERROR_CODES = frozenset(
    {"expired_token", "invalid_token", "NO_AUTH_FOUND", "INVALID_CREDENTIALS"}
)

#: Códigos que não adianta repetir: o erro é do payload, não do servidor.
FATAL_ERROR_CODES = frozenset(
    {
        "ERROR_METHOD_NOT_FOUND",
        "ERROR_ARGUMENT",
        "ACCESS_DENIED",
        "INVALID_ARG_VALUE",
        "BAD_REQUEST",
    }
)
