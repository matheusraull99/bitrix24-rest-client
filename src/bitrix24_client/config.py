"""Carregamento de credencial a partir do ambiente.

Webhook do Bitrix24 é credencial de acesso total ao portal: quem tem a URL
tem o CRM. Por isso ela nunca entra no código nem no repositório — só em
variável de ambiente ou cofre.
"""

from __future__ import annotations

import os
import re

from .client import Bitrix24
from .errors import BitrixError
from .throttle import LeakyBucket

#: ``https://portal.bitrix24.com.br/rest/<user_id>/<token>/``
WEBHOOK_RE = re.compile(r"^https://[\w.-]+/rest/\d+/[A-Za-z0-9]+/?$")


def from_env(prefix: str = "BITRIX") -> Bitrix24:
    """Monta um cliente a partir de ``BITRIX_WEBHOOK`` e afins.

    Variáveis lidas:
        ``<prefix>_WEBHOOK``: obrigatória, URL completa do webhook.
        ``<prefix>_TIMEOUT``: timeout em segundos (padrão ``30``).
        ``<prefix>_BUCKET_CAPACITY``: tamanho do balde (padrão ``50``).
        ``<prefix>_BUCKET_RATE``: vazamento por segundo (padrão ``2``).

    Raises:
        BitrixError: variável ausente ou URL fora do formato esperado.
    """
    webhook = os.environ.get(f"{prefix}_WEBHOOK", "").strip()
    if not webhook:
        raise BitrixError(
            f"variavel {prefix}_WEBHOOK nao definida. "
            "Copie .env.example para .env e preencha."
        )
    if not WEBHOOK_RE.match(webhook):
        raise BitrixError(
            f"{prefix}_WEBHOOK fora do formato "
            "https://portal.bitrix24.com.br/rest/<id>/<token>/"
        )

    return Bitrix24(
        webhook,
        timeout=_env_float(f"{prefix}_TIMEOUT", 30.0),
        bucket=LeakyBucket(
            capacity=_env_float(f"{prefix}_BUCKET_CAPACITY", 50.0),
            leak_rate=_env_float(f"{prefix}_BUCKET_RATE", 2.0),
        ),
    )


def _env_float(name: str, default: float) -> float:
    """Lê float do ambiente sem explodir quando o valor vem sujo."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def mask(webhook: str) -> str:
    """Versão da URL segura para log — o token vira ``***``.

    Log de RPA vai para arquivo, Sentry e às vezes para o chat da equipe.
    Um webhook vazado ali é acesso irrestrito ao CRM.
    """
    return re.sub(r"(/rest/\d+/)[^/]+", r"\1***", webhook)
