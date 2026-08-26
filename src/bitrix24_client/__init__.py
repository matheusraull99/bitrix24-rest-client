"""Cliente REST resiliente para o Bitrix24.

Uso mínimo::

    from bitrix24_client import Bitrix24, from_env

    bx = from_env()
    for deal in bx.fetch_all("crm.deal.list", {"select": ["ID", "TITLE"]}):
        print(deal["ID"], deal["TITLE"])
"""

from .client import BATCH_MAX_COMMANDS, PAGE_SIZE, Bitrix24, chunked, encode_query
from .config import from_env
from .errors import (
    BitrixAPIError,
    BitrixAuthError,
    BitrixError,
    BitrixRateLimitError,
    BitrixTransportError,
)
from .throttle import LeakyBucket, backoff_delays

__version__ = "1.0.0"

__all__ = [
    "BATCH_MAX_COMMANDS",
    "PAGE_SIZE",
    "Bitrix24",
    "BitrixAPIError",
    "BitrixAuthError",
    "BitrixError",
    "BitrixRateLimitError",
    "BitrixTransportError",
    "LeakyBucket",
    "backoff_delays",
    "chunked",
    "encode_query",
    "from_env",
]
