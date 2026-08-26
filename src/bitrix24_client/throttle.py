"""Leaky bucket local, no mesmo formato que o Bitrix24 usa do lado dele.

O portal mantém um balde de operações por conta: cada chamada REST consome
uma unidade e o balde vaza a uma taxa constante. Quando o balde enche, o
portal responde ``503``.

A diferença entre pedir perdão e pedir licença é grande aqui. Só reagir ao
``503`` significa que o RPA já gastou a chamada, já pagou a latência e ainda
vai dormir. Manter um balde espelhado no cliente faz a espera acontecer
*antes* da requisição — o 503 vira exceção rara em vez de rotina.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class LeakyBucket:
    """Balde furado com espera bloqueante.

    Args:
        capacity: quantas operações cabem no balde cheio.
        leak_rate: operações que vazam por segundo.
    """

    capacity: float = 50.0
    leak_rate: float = 2.0
    _level: float = field(default=0.0, init=False)
    # Lambda em vez de ``default_factory=time.monotonic``: a referência direta
    # congela a função no import e escapa de qualquer relógio injetado depois.
    _last_leak: float = field(default_factory=lambda: time.monotonic(), init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def _drain(self, now: float) -> None:
        """Baixa o nível conforme o tempo passado. Exige ``_lock`` tomado."""
        elapsed = now - self._last_leak
        if elapsed > 0:
            self._level = max(0.0, self._level - elapsed * self.leak_rate)
            self._last_leak = now

    def acquire(self, cost: float = 1.0) -> float:
        """Reserva ``cost`` operações, dormindo o necessário.

        Returns:
            Segundos efetivamente dormidos — útil para métrica de saturação.
        """
        slept = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._drain(now)
                if self._level + cost <= self.capacity:
                    self._level += cost
                    return slept
                deficit = self._level + cost - self.capacity
                wait = deficit / self.leak_rate
                if self._last_leak > now:
                    # Penalidade de 503/429 ainda vigente: o balde só volta a
                    # vazar depois dela. Somar aqui evita acordar cedo e
                    # repetir a espera em fatias.
                    wait += self._last_leak - now
            # Dorme fora do lock para não segurar as outras threads.
            time.sleep(wait)
            slept += wait

    def penalize(self, seconds: float) -> None:
        """Enche o balde para forçar pausa após um ``503``/``429`` real.

        O servidor sabe mais do que a estimativa local: quando ele reclama,
        o balde local estava otimista. Encher até o teto por ``seconds``
        realinha os dois lados.
        """
        with self._lock:
            self._level = self.capacity
            self._last_leak = time.monotonic() + seconds

    @property
    def level(self) -> float:
        """Nível instantâneo, já descontado o vazamento."""
        with self._lock:
            self._drain(time.monotonic())
            return self._level

    @property
    def saturation(self) -> float:
        """Fração do balde em uso, de ``0.0`` a ``1.0``."""
        return self.level / self.capacity if self.capacity else 0.0


def backoff_delays(
    attempts: int, base: float = 0.5, cap: float = 30.0, jitter: float = 0.3
) -> list[float]:
    """Gera atrasos exponenciais com jitter determinístico por tentativa.

    O jitter é derivado do índice, não de ``random``, para o teste ser
    reprodutível sem monkeypatch.
    """
    delays = []
    for i in range(attempts):
        raw = min(cap, base * (2**i))
        skew = 1.0 + jitter * (((i * 2654435761) % 1000) / 1000.0 - 0.5) * 2
        delays.append(round(raw * skew, 3))
    return delays
