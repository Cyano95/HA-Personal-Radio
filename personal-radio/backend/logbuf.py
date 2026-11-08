"""
Protokollpuffer für die Oberfläche.

Das Addon schreibt sein Protokoll nach stdout — sichtbar nur im Supervisor.
Für die Fehlersuche ist das umständlich, deshalb hält dieser Handler die
letzten Zeilen zusätzlich im Speicher und die Einstellungsseite zeigt sie an.

Bewusst ohne Datei: nichts, was mitwächst oder aufgeräumt werden müsste.
"""
from __future__ import annotations

import logging
from collections import deque

MAX_LINES = 400

_buffer: deque[str] = deque(maxlen=MAX_LINES)


class RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _buffer.append(self.format(record))
        except Exception:                      # Protokollieren darf nie stören
            pass


def install(level: int = logging.INFO) -> None:
    """Einmalig an den Wurzel-Logger hängen."""
    root = logging.getLogger()
    if any(isinstance(h, RingHandler) for h in root.handlers):
        return
    h = RingHandler()
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
    h.setLevel(level)
    root.addHandler(h)


def text(limit: int = MAX_LINES) -> str:
    lines = list(_buffer)
    return "\n".join(lines[-limit:])


def clear() -> None:
    _buffer.clear()
