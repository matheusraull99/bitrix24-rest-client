"""Testes do cliente REST — foco no que quebra em produção."""

from __future__ import annotations

import pytest
import requests
from conftest import FakeResponse, FakeSession, api_error, limit, ok

from bitrix24_client import Bitrix24, LeakyBucket, encode_query
from bitrix24_client.errors import (
    BitrixAPIError,
    BitrixAuthError,
    BitrixTransportError,
)

WEBHOOK = "https://portal.bitrix24.com.br/rest/1/tok3n/"


def build(responses, **kwargs) -> tuple[Bitrix24, FakeSession]:
    session = FakeSession(responses)
    bucket = LeakyBucket(capacity=1000.0, leak_rate=1000.0)  # não atrapalha o teste
    return Bitrix24(WEBHOOK, session=session, bucket=bucket, **kwargs), session


def test_devolve_result_desembrulhado():
    bx, _ = build([ok({"ID": "7"})])
    assert bx.call("crm.deal.get", {"id": 7}) == {"ID": "7"}


def test_erro_no_corpo_com_http_200_vira_excecao(no_sleep):
    """O caso que ``raise_for_status`` deixa passar."""
    bx, _ = build([api_error("ERROR_NOT_FOUND", "nao existe")] * 5)
    with pytest.raises(BitrixAPIError) as exc:
        bx.call("crm.deal.get", {"id": 999})
    assert exc.value.code == "ERROR_NOT_FOUND"


def test_erro_de_auth_nao_gasta_retry(no_sleep):
    bx, session = build([api_error("expired_token")])
    with pytest.raises(BitrixAuthError):
        bx.call("crm.deal.list")
    assert len(session.calls) == 1, "token morto nao deve ser repetido"


def test_erro_fatal_de_argumento_nao_gasta_retry(no_sleep):
    bx, session = build([api_error("ERROR_METHOD_NOT_FOUND")])
    with pytest.raises(BitrixAPIError):
        bx.call("crm.metodo.inexistente")
    assert len(session.calls) == 1


def test_503_e_repetido_e_depois_sucede(no_sleep):
    bx, session = build([limit(503), limit(503), ok([{"ID": "1"}])])
    assert bx.call("crm.deal.list") == [{"ID": "1"}]
    assert len(session.calls) == 3
    assert bx.stats["retries"] == 2


def test_429_respeita_retry_after(no_sleep):
    """O header do servidor manda mais que o backoff local."""
    bx, session = build([limit(429, retry_after="7"), ok([])])
    bx.call("crm.deal.list")
    assert len(session.calls) == 2
    assert sum(no_sleep) >= 7, "a espera total precisa cobrir o Retry-After"


def test_falha_de_rede_e_repetida(no_sleep):
    boom = requests.ConnectionError("dns")
    bx, session = build([boom, boom, ok({"ID": "3"})])
    assert bx.call("crm.deal.get", {"id": 3}) == {"ID": "3"}
    assert len(session.calls) == 3


def test_html_de_manutencao_nao_vira_crash_de_json(no_sleep):
    """Portal em manutenção responde 200 com HTML — já vi derrubar RPA."""
    html = FakeResponse(200, None, text_body="<html>Manutencao</html>")
    bx, _ = build([html] * 5)
    with pytest.raises(BitrixTransportError):
        bx.call("crm.deal.list")


def test_escrita_leva_idempotency_key_estavel():
    bx, session = build([ok(1), ok(1)])
    params = {"fields": {"TITLE": "Negocio"}}
    bx.call("crm.deal.add", params)
    bx.call("crm.deal.add", params)
    chaves = [c["headers"].get("Idempotency-Key") for c in session.calls]
    assert chaves[0] == chaves[1], "mesmo payload precisa gerar a mesma chave"
    assert chaves[0] is not None


def test_leitura_nao_leva_idempotency_key():
    bx, session = build([ok([])])
    bx.call("crm.deal.list")
    assert "Idempotency-Key" not in session.calls[0]["headers"]


def test_paginacao_avanca_por_id_e_para_na_pagina_curta():
    pagina1 = ok([{"ID": str(i)} for i in range(1, 51)])
    pagina2 = ok([{"ID": "51"}, {"ID": "52"}])
    bx, session = build([pagina1, pagina2])

    linhas = list(bx.fetch_all("crm.deal.list"))

    assert len(linhas) == 52
    assert session.calls[0]["json"]["filter"][">ID"] == 0
    assert session.calls[1]["json"]["filter"][">ID"] == 50
    assert session.calls[0]["json"]["start"] == -1, "start=-1 desliga o COUNT"


def test_paginacao_nao_entra_em_loop_se_id_nao_avancar():
    """Defesa contra portal devolvendo a mesma página para sempre."""
    repetida = ok([{"ID": "10"}] * 50)
    bx, session = build([repetida, repetida])
    linhas = list(bx.fetch_all("crm.deal.list"))
    assert len(linhas) == 100, "para na segunda pagina em vez de girar sem fim"
    assert len(session.calls) == 2


def test_item_list_desembrulha_items():
    """``crm.item.list`` embrulha em ``items``; ``crm.deal.list`` não."""
    bx, _ = build([ok({"items": [{"id": 1}, {"id": 2}]})])
    assert len(list(bx.item_list(128))) == 2


def test_batch_recusa_mais_de_50_comandos():
    bx, _ = build([])
    comandos = {f"c{i}": ("crm.deal.add", {}) for i in range(51)}
    with pytest.raises(ValueError, match="50"):
        bx.batch(comandos)


def test_batch_separa_sucesso_de_erro():
    # O ``batch`` aninha duas vezes: ``result.result`` traz os sucessos e
    # ``result.result_error`` os erros, no mesmo HTTP 200.
    interno = {
        "result": {"c0": 10, "c2": 12},
        "result_error": {"c1": "ACCESS_DENIED"},
        "result_next": {},
    }
    bx, _ = build([ok(interno)])
    out = bx.batch(
        {
            "c0": ("crm.deal.add", {"fields": {"TITLE": "a"}}),
            "c1": ("crm.deal.add", {"fields": {"TITLE": "b"}}),
            "c2": ("crm.deal.add", {"fields": {"TITLE": "c"}}),
        }
    )
    assert out["result"] == {"c0": 10, "c2": 12}
    assert out["errors"] == {"c1": "ACCESS_DENIED"}


def test_batch_iter_devolve_item_junto_do_resultado():
    """Reparear por índice é bug clássico quando um lote falha pela metade."""
    interno = {"result": {"c0": 100, "c1": 101}, "result_error": {}, "result_next": {}}
    bx, _ = build([ok(interno)])
    titulos = ["Alfa", "Beta"]
    pares = list(
        bx.batch_iter(titulos, "crm.deal.add", lambda t: {"fields": {"TITLE": t}})
    )
    assert pares == [("Alfa", 100, None), ("Beta", 101, None)]


class TestEncodeQuery:
    """O formato de array do PHP é a parte que mais silenciosamente falha."""

    def test_dicionario_aninhado_vira_colchetes(self):
        assert encode_query({"fields": {"TITLE": "Casa"}}) == "fields%5BTITLE%5D=Casa"

    def test_lista_vira_indice_numerico(self):
        assert encode_query({"select": ["ID", "TITLE"]}) == (
            "select%5B0%5D=ID&select%5B1%5D=TITLE"
        )

    def test_booleano_vira_zero_ou_um(self):
        assert encode_query({"halt": True}) == "halt=1"

    def test_valor_com_espaco_e_escapado(self):
        assert "Novo%20lead" in encode_query({"fields": {"TITLE": "Novo lead"}})
