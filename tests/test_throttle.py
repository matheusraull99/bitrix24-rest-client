"""Testes do balde furado — a peça que decide se o RPA vive ou toma 503."""

from __future__ import annotations

import pytest

from bitrix24_client.throttle import LeakyBucket, backoff_delays


def test_balde_vazio_libera_sem_esperar():
    bucket = LeakyBucket(capacity=10, leak_rate=2)
    assert bucket.acquire() == 0.0
    assert bucket.level == pytest.approx(1.0, abs=0.01)


def test_balde_cheio_faz_esperar(no_sleep):
    bucket = LeakyBucket(capacity=3, leak_rate=2)
    for _ in range(3):
        bucket.acquire()
    esperou = bucket.acquire()
    assert esperou > 0, "a quarta chamada precisa aguardar o vazamento"
    assert esperou == pytest.approx(0.5, abs=0.01), "1 operacao a 2/s = 0,5s"


def test_vazamento_devolve_capacidade_com_o_tempo(no_sleep):
    bucket = LeakyBucket(capacity=4, leak_rate=2)
    for _ in range(4):
        bucket.acquire()
    assert bucket.saturation == pytest.approx(1.0)

    import time

    time.sleep(2.0)  # relogio falso avanca 2s -> vazam 4 operacoes
    assert bucket.saturation == pytest.approx(0.0)


def test_penalidade_segura_o_balde_pelo_tempo_pedido(no_sleep):
    """Depois de um 503, o servidor manda mais que a estimativa local."""
    bucket = LeakyBucket(capacity=50, leak_rate=2)
    bucket.penalize(6.0)
    esperou = bucket.acquire()
    assert esperou >= 6.0


def test_penalidade_nao_gira_em_fatias(no_sleep):
    """Uma espera só, não seis de um segundo — senao o log vira ruido."""
    bucket = LeakyBucket(capacity=50, leak_rate=2)
    bucket.penalize(6.0)
    bucket.acquire()
    assert len(no_sleep) == 1, f"esperava 1 sleep, houve {len(no_sleep)}"


def test_backoff_cresce_e_tem_teto():
    delays = backoff_delays(8, base=0.5, cap=30.0)
    assert delays[0] < delays[3] < delays[6], "precisa crescer"
    assert all(d <= 30.0 * 1.3 for d in delays), "precisa respeitar o teto"


def test_backoff_e_reproduzivel():
    """Jitter derivado do indice: mesmo argumento, mesma sequencia."""
    assert backoff_delays(5) == backoff_delays(5)


def test_backoff_tem_jitter_entre_tentativas():
    """Sem jitter, N robos reiniciam juntos e recriam a tempestade."""
    delays = backoff_delays(6, base=1.0, cap=1000.0)
    razoes = [delays[i + 1] / delays[i] for i in range(len(delays) - 1)]
    assert not all(r == pytest.approx(2.0) for r in razoes)
