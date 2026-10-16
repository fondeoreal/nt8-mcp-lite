"""Cliente TCP contra el AddOn MCPBridge de NinjaTrader 8.

Es de solo lectura por construccion: rechaza localmente cualquier comando de ordenes
antes de que salga hacia NinjaTrader (el AddOn v3 ademas los tiene eliminados).
"""

from __future__ import annotations

import json
import socket
import threading
import time
import uuid
from typing import Any

# Comandos que el cliente nunca enviara, aunque un AddOn antiguo los aceptase.
BLOCKED_COMMANDS = frozenset({
    "place_order", "place_bracket", "submit_order", "change_order", "modify_order",
    "cancel_order", "cancel_all", "flatten", "close_position", "reverse_position",
    "strategy_stop", "strategy_start", "strategy_enable", "strategy_disable",
    "atm_start", "atm_close",
})


class NT8Error(RuntimeError):
    """El AddOn de NT8 devolvio un error o no hay conexion."""


class BlockedError(NT8Error):
    """Operacion de ordenes bloqueada por la politica de solo lectura."""


class NT8Client:
    """Conexion persistente con reconexion automatica y serializacion de peticiones."""

    def __init__(self, host: str, port: int, token: str, timeout: float = 30.0) -> None:
        self.host = host
        self.port = port
        self.token = token
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._reader = None
        self._lock = threading.Lock()
        self._info: dict | None = None
        self._info_at = 0.0
        self.last_error: str | None = None  # motivo del ultimo ping fallido (para nt8_status)

    # -- conexion --------------------------------------------------
    def _connect(self, timeout: float) -> None:
        self.close()
        try:
            sock = socket.create_connection((self.host, self.port), timeout=min(timeout, 5.0))
        except OSError as exc:
            raise NT8Error(
                f"No puedo conectar con NinjaTrader en {self.host}:{self.port} ({exc}). "
                "Comprueba que NT8 este abierto y que el AddOn MCPBridge este compilado. "
                "Las herramientas de historial de cuenta (nt8_history_*) y de vault funcionan igualmente sin NT8."
            ) from exc
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock
        self._reader = sock.makefile("r", encoding="utf-8", newline="\n")

    def close(self) -> None:
        for obj in (self._reader, self._sock):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self._reader = None
        self._sock = None

    # -- peticiones ------------------------------------------------
    def call(self, cmd: str, timeout: float | None = None, **args: Any) -> Any:
        if cmd in BLOCKED_COMMANDS:
            raise BlockedError(
                f"BLOQUEADO: '{cmd}'. Este servidor es de SOLO LECTURA: no envia, modifica ni cancela "
                "ordenes, ni aplana posiciones, ni arranca/para estrategias."
            )
        timeout = float(timeout or self.timeout)
        payload = {
            "id": uuid.uuid4().hex[:8],
            "token": self.token,
            "cmd": cmd,
            "args": {k: v for k, v in args.items() if v is not None},
        }
        line = json.dumps(payload, ensure_ascii=False) + "\n"

        with self._lock:
            raw = ""
            for attempt in (1, 2):  # un reintento con reconexion
                try:
                    if self._sock is None:
                        self._connect(timeout)
                    self._sock.settimeout(timeout)
                    self._sock.sendall(line.encode("utf-8"))
                    raw = self._reader.readline()
                    if not raw:
                        raise OSError("conexion cerrada por NinjaTrader")
                    break
                except socket.timeout as exc:
                    # No reintentar: la peticion puede seguir en curso dentro de NT8.
                    self.close()
                    raise NT8Error(f"NT8 no respondio en {timeout:.0f} s al comando '{cmd}'.") from exc
                except (OSError, AttributeError) as exc:
                    self.close()
                    if attempt == 2:
                        raise NT8Error(f"Fallo de comunicacion con NT8: {exc}") from exc

        try:
            response = json.loads(raw)
        except json.JSONDecodeError as exc:
            if raw[:1] not in "{[":
                raise NT8Error(
                    f"El puerto {self.port} no habla el protocolo del puente MCP (¿es el ATI de NinjaTrader, "
                    "36973?). Revisa mcp_bridge.json / config.json."
                ) from exc
            raise NT8Error(f"Respuesta ilegible de NT8: {raw[:200]!r}") from exc

        if not response.get("ok", False):
            error = str(response.get("error", "error desconocido en NT8"))
            if error.startswith("comando desconocido"):
                error += (" — el AddOn MCPBridge instalado en NT8 es anterior a v3. Copia ninjatrader/MCPBridge.cs "
                          "a bin\\Custom\\AddOns y compila (F5) cuando estes plano.")
            raise NT8Error(error)
        return response.get("data")

    # -- utilidad --------------------------------------------------
    def info(self, max_age: float = 30.0) -> dict:
        """Ping cacheado: version del puente y estado. {} si NT8 no esta disponible."""
        now = time.monotonic()
        if self._info is not None and now - self._info_at < max_age:
            return self._info
        try:
            self._info = self.call("ping", timeout=8) or {}
            self.last_error = None
        except NT8Error as exc:
            self._info = {}
            self.last_error = str(exc)
        self._info_at = now
        return self._info

    def bridge_version(self) -> tuple[int, ...]:
        v = self.info().get("bridge_version")
        if not v:
            return (2,) if self._info else (0,)
        try:
            return tuple(int(x) for x in str(v).split("."))
        except ValueError:
            return (3,)

    def supports_v3(self) -> bool:
        return self.bridge_version() >= (3,)

    def is_alive(self) -> bool:
        return bool(self.info(max_age=0))
