"""Cliente REST do Bitrix24 pensado para RPA que roda sozinho de madrugada.

Três decisões separam este cliente de um ``requests.post`` cru:

1. **O limite é respeitado antes da chamada**, com um balde furado espelhado
   (:mod:`~bitrix24_client.throttle`), não depois de tomar ``503``.
2. **Escrita é idempotente por padrão.** Toda chamada mutante leva um
   ``Idempotency-Key`` derivado do payload; se o retry acontecer depois que o
   servidor já gravou, o portal devolve o mesmo resultado em vez de duplicar
   o negócio.
3. **Paginação usa filtro por ID, não ``start``.** Acima de ~50 mil registros
   o ``start`` fica quadrático porque o portal reconta o offset toda vez.
   Filtrar ``>ID`` com ordenação ascendente mantém o custo constante.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Iterable, Iterator, Sequence
from typing import Any
from urllib.parse import quote, urljoin

import requests

from .errors import (
    AUTH_ERROR_CODES,
    FATAL_ERROR_CODES,
    BitrixAPIError,
    BitrixAuthError,
    BitrixRateLimitError,
    BitrixTransportError,
)
from .throttle import LeakyBucket, backoff_delays

log = logging.getLogger("bitrix24")

#: Teto rígido do endpoint ``batch``. Mandar 51 não dá erro: o portal
#: silenciosamente ignora o excedente, que é bem pior que um 400.
BATCH_MAX_COMMANDS = 50

#: Tamanho de página do CRM. Pedir mais que isso não aumenta o retorno.
PAGE_SIZE = 50

#: Métodos que gravam. Só estes ganham ``Idempotency-Key``.
_MUTATING_PREFIXES = ("add", "update", "delete", "set", "register", "unregister")


class Bitrix24:
    """Ponto único de contato com a REST do portal.

    Args:
        webhook: URL completa do webhook de entrada, terminando em ``/``.
            Ex.: ``https://portal.bitrix24.com.br/rest/1/abc123xyz/``.
        timeout: timeout de socket, em segundos.
        max_attempts: tentativas totais por chamada, incluindo a primeira.
        bucket: balde customizado. Portais em plano maior aguentam taxa maior.
        session: ``requests.Session`` injetável — o teste passa um dublê.
    """

    def __init__(
        self,
        webhook: str,
        *,
        timeout: float = 30.0,
        max_attempts: int = 5,
        bucket: LeakyBucket | None = None,
        session: requests.Session | None = None,
    ) -> None:
        if not webhook.endswith("/"):
            webhook += "/"
        self.webhook = webhook
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.bucket = bucket or LeakyBucket()
        self.session = session or requests.Session()
        self.stats = {"calls": 0, "retries": 0, "throttled_seconds": 0.0, "batches": 0}

    # ------------------------------------------------------------------ #
    # Chamada base
    # ------------------------------------------------------------------ #

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Executa um método REST e devolve o conteúdo de ``result``.

        Raises:
            BitrixAuthError: token/webhook inválido — não adianta repetir.
            BitrixAPIError: erro de negócio devolvido pelo portal.
            BitrixRateLimitError: limite estourado além do orçamento de retry.
            BitrixTransportError: rede indisponível após todas as tentativas.
        """
        params = params or {}
        url = urljoin(self.webhook, method)
        headers = {"Content-Type": "application/json"}
        if self._is_mutating(method):
            headers["Idempotency-Key"] = self._idempotency_key(method, params)

        delays = backoff_delays(self.max_attempts)
        last_error: Exception | None = None

        for attempt in range(self.max_attempts):
            self.stats["throttled_seconds"] += self.bucket.acquire()
            self.stats["calls"] += 1
            try:
                resp = self.session.post(
                    url, json=params, headers=headers, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_error = BitrixTransportError(f"{method}: {exc}")
                log.warning("rede falhou em %s (tentativa %d): %s", method, attempt + 1, exc)
            else:
                outcome = self._interpret(method, params, resp)
                if not isinstance(outcome, Exception):
                    return outcome
                last_error = outcome
                if isinstance(outcome, BitrixAuthError):
                    raise outcome
                if isinstance(outcome, BitrixAPIError) and outcome.code in FATAL_ERROR_CODES:
                    raise outcome
                if isinstance(outcome, BitrixRateLimitError):
                    # O servidor sabe mais que o balde local: realinha os dois.
                    self.bucket.penalize(outcome.retry_after)

            if attempt + 1 < self.max_attempts:
                self.stats["retries"] += 1
                time.sleep(delays[attempt])

        assert last_error is not None
        raise last_error

    def _interpret(
        self, method: str, params: dict[str, Any], resp: requests.Response
    ) -> Any:
        """Traduz a resposta HTTP em resultado ou exceção — sem levantar.

        Devolver a exceção em vez de levantá-la deixa o laço de retry decidir
        o que é recuperável, num lugar só.
        """
        if resp.status_code in (429, 503):
            kind = "rate" if resp.status_code == 429 else "operating"
            retry_after = self._retry_after(resp, default=2.0 if kind == "rate" else 5.0)
            log.info("limite '%s' em %s; aguardando %.1fs", kind, method, retry_after)
            return BitrixRateLimitError(kind, retry_after, method)

        if resp.status_code >= 500:
            return BitrixTransportError(f"{method}: HTTP {resp.status_code}")

        try:
            body = resp.json()
        except ValueError:
            snippet = resp.text[:200]
            return BitrixTransportError(
                f"{method}: resposta nao-JSON (HTTP {resp.status_code}): {snippet}"
            )

        if isinstance(body, dict) and "error" in body:
            code = str(body.get("error"))
            desc = str(body.get("error_description", ""))
            cls = BitrixAuthError if code in AUTH_ERROR_CODES else BitrixAPIError
            return cls(code, desc, method, params)

        if resp.status_code >= 400:
            return BitrixTransportError(f"{method}: HTTP {resp.status_code}")

        return body.get("result") if isinstance(body, dict) else body

    @staticmethod
    def _retry_after(resp: requests.Response, default: float) -> float:
        """Lê ``Retry-After`` em segundos; cai no default quando ausente."""
        raw = resp.headers.get("Retry-After")
        if raw:
            try:
                return max(0.0, float(raw))
            except ValueError:
                pass
        return default

    @staticmethod
    def _is_mutating(method: str) -> bool:
        tail = method.rsplit(".", 1)[-1].lower()
        return tail.startswith(_MUTATING_PREFIXES)

    @staticmethod
    def _idempotency_key(method: str, params: dict[str, Any]) -> str:
        """Chave estável derivada de método + payload canônico.

        Duas chamadas iguais geram a mesma chave — que é exatamente o ponto:
        o retry de uma escrita perdida não pode virar registro duplicado.
        """
        canonical = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(f"{method}|{canonical}".encode()).hexdigest()
        return digest[:32]

    # ------------------------------------------------------------------ #
    # Paginação
    # ------------------------------------------------------------------ #

    def fetch_all(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        id_field: str = "ID",
        page_size: int = PAGE_SIZE,
    ) -> Iterator[dict[str, Any]]:
        """Percorre uma lista inteira do CRM, página a página.

        Usa o padrão recomendado para volume alto: ordena por ID ascendente,
        desliga a contagem total (``start: -1``) e avança o filtro por
        ``>ID``. Custo por página constante, sem o offset quadrático do
        ``start``.

        Yields:
            Um dicionário por registro, na ordem de ID.
        """
        base = dict(params or {})
        base_filter = dict(base.pop("filter", None) or {})
        base["order"] = {id_field: "ASC"}
        base["start"] = -1  # desliga o COUNT(*) escondido em cada página

        select = base.get("select")
        if select and id_field not in select and "*" not in select:
            base["select"] = [*select, id_field]

        last_id = 0
        while True:
            # Dicionário novo a cada página: reaproveitar um só faz o payload
            # de todas as páginas apontar para o mesmo objeto mutável.
            page_params = dict(base)
            page_params["filter"] = {**base_filter, f">{id_field}": last_id}

            rows = self._unwrap_rows(self.call(method, page_params))
            if not rows:
                return

            yield from rows

            if len(rows) < page_size:
                return

            new_last = int(rows[-1][id_field])
            if new_last <= last_id:  # trava contra loop infinito
                log.error("paginacao de %s nao avancou no ID %s", method, last_id)
                return
            last_id = new_last

    @staticmethod
    def _unwrap_rows(result: Any) -> list[dict[str, Any]]:
        """Normaliza o formato de lista, que muda entre famílias de método.

        ``crm.deal.list`` devolve a lista direta; ``crm.item.list`` embrulha
        em ``{"items": [...]}``. Essa inconsistência é fonte recorrente de
        ``TypeError`` em produção.
        """
        if isinstance(result, dict):
            for key in ("items", "tasks", "orders"):
                if key in result:
                    return list(result[key])
            return []
        return list(result or [])

    # ------------------------------------------------------------------ #
    # Batch
    # ------------------------------------------------------------------ #

    def batch(
        self,
        commands: dict[str, tuple[str, dict[str, Any]]],
        *,
        halt_on_error: bool = False,
    ) -> dict[str, Any]:
        """Executa até 50 comandos em uma requisição.

        Args:
            commands: mapa ``apelido -> (método, params)``. O apelido volta
                como chave no resultado.
            halt_on_error: ``True`` aborta o lote no primeiro erro. O padrão
                é ``False`` porque em carga costuma ser melhor gravar 49 e
                reprocessar 1 do que perder o lote inteiro.

        Raises:
            ValueError: mais de 50 comandos — falhar aqui é melhor que ver o
                portal descartar o excedente em silêncio.
        """
        if len(commands) > BATCH_MAX_COMMANDS:
            raise ValueError(
                f"batch aceita no maximo {BATCH_MAX_COMMANDS} comandos, "
                f"recebeu {len(commands)}"
            )
        if not commands:
            return {"result": {}, "errors": {}, "next": {}}

        payload = {
            "halt": int(halt_on_error),
            "cmd": {
                alias: f"{method}?{encode_query(params)}"
                for alias, (method, params) in commands.items()
            },
        }
        self.stats["batches"] += 1
        raw = self.call("batch", payload) or {}

        errors = raw.get("result_error") or {}
        for alias, err in errors.items():
            log.error("comando '%s' falhou no batch: %s", alias, err)

        return {
            "result": raw.get("result") or {},
            "errors": errors,
            "next": raw.get("result_next") or {},
        }

    def batch_iter(
        self,
        items: Sequence[Any],
        method: str,
        build_params,
        *,
        halt_on_error: bool = False,
    ) -> Iterator[tuple[Any, Any, str | None]]:
        """Aplica ``method`` a muitos itens, em lotes de 50.

        Args:
            items: coleção a processar.
            method: método REST a chamar por item.
            build_params: função ``item -> dict`` com os parâmetros.

        Yields:
            Tuplas ``(item, resultado, erro)``; ``erro`` é ``None`` no sucesso.
            Devolver o item junto evita o clássico bug de reparear resultado
            com entrada por índice depois que um lote falhou pela metade.
        """
        for chunk in chunked(items, BATCH_MAX_COMMANDS):
            commands = {
                f"c{i}": (method, build_params(item)) for i, item in enumerate(chunk)
            }
            out = self.batch(commands, halt_on_error=halt_on_error)
            for i, item in enumerate(chunk):
                alias = f"c{i}"
                if alias in out["errors"]:
                    yield item, None, str(out["errors"][alias])
                else:
                    yield item, out["result"].get(alias), None

    # ------------------------------------------------------------------ #
    # Atalhos de CRM
    # ------------------------------------------------------------------ #

    def item_list(self, entity_type_id: int, **params: Any) -> Iterator[dict[str, Any]]:
        """Lista itens de qualquer entidade do CRM, inclusive SPA.

        ``entityTypeId`` identifica o *tipo* (1 = lead, 2 = negócio, 3 = contato,
        4 = empresa, 128+ = processos inteligentes). Não confundir com o ``id``
        do item nem com o ``entityId`` da timeline: os três aparecem no mesmo
        payload e trocá-los devolve lista vazia, sem erro nenhum.
        """
        return self.fetch_all(
            "crm.item.list", {"entityTypeId": entity_type_id, **params}, id_field="id"
        )

    def whoami(self) -> dict[str, Any]:
        """Valida a credencial e devolve o usuário dono do webhook."""
        return self.call("profile")

    def __repr__(self) -> str:  # pragma: no cover - conveniência de debug
        host = self.webhook.split("/rest/")[0]
        return (
            f"<Bitrix24 {host} calls={self.stats['calls']} "
            f"saturacao={self.bucket.saturation:.0%}>"
        )


def encode_query(params: dict[str, Any]) -> str:
    """Serializa params no formato de array PHP que o ``batch`` exige.

    ``{"fields": {"TITLE": "x"}}`` precisa virar ``fields[TITLE]=x``. A
    ``urlencode`` padrão do Python produz ``fields={'TITLE': 'x'}``, que o
    portal aceita e interpreta como string — gravando lixo sem reclamar.
    """
    parts: list[str] = []

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for key, val in value.items():
                walk(f"{prefix}[{key}]", val)
        elif isinstance(value, (list, tuple)):
            for index, val in enumerate(value):
                walk(f"{prefix}[{index}]", val)
        elif isinstance(value, bool):
            parts.append(f"{quote(prefix)}={int(value)}")
        elif value is None:
            parts.append(f"{quote(prefix)}=")
        else:
            parts.append(f"{quote(prefix)}={quote(str(value), safe='')}")

    for key, val in params.items():
        walk(key, val)
    return "&".join(parts)


def chunked(seq: Iterable[Any], size: int) -> Iterator[list[Any]]:
    """Fatia um iterável em blocos de ``size``."""
    chunk: list[Any] = []
    for item in seq:
        chunk.append(item)
        if len(chunk) == size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk
