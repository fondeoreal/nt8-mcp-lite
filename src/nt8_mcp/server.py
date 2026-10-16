"""Servidor MCP de NinjaTrader 8 — v3, SOLO LECTURA de ordenes.

Arranque:
    python -m nt8_mcp.server                      (stdio: Claude Desktop/Code, Cursor, VS Code, Gemini CLI...)
    python -m nt8_mcp.http_gateway                (HTTP: MCP streamable + SSE + REST/OpenAPI para cualquier IA)

Cualquier IA puede leer cuentas, posiciones, ordenes, ejecuciones, historial completo de la
base de datos de NT8, historico de precios, backtests y codigo NinjaScript. Enviar, modificar
o cancelar ordenes esta BLOQUEADO en tres capas: aqui no existe ninguna herramienta que lo haga,
el cliente TCP rechaza esos comandos y el AddOn MCPBridge v3 no tiene ese codigo.

Perfil de herramientas (NT8_MCP_PROFILE o "tool_profile" en config.json):
    full (defecto, ~75 herramientas) | core (~30, para clientes con limite de herramientas)
"""

from __future__ import annotations

import csv
import functools
import json
import os
import re
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import history as H
from .client import BlockedError, NT8Client, NT8Error
from .config import Config
from .edition import LITE_TOOLS, PRO_PROMPTS
from .ninjascript import NinjaScriptFiles, SourceError
from .nt8db import DBError, NT8Database
from .vault import Trade, Vault, analyze, read_strategy_template, simulate_trailing_drawdown

VERSION = "3.0.0"

CFG = Config.load()
NT8 = NT8Client(CFG.host, CFG.port, CFG.token, CFG.timeout_seconds)
VAULT = Vault(CFG.vault_root, CFG.vault_extra)
SOURCES = NinjaScriptFiles(CFG.custom_root)
DB = NT8Database(CFG.db_path)

INSTRUCTIONS = """\
Servidor MCP de NinjaTrader 8 (SOLO LECTURA de ordenes). Da acceso a:
- Cuentas en vivo (nt8_accounts_overview, nt8_account_status, nt8_positions, nt8_orders, nt8_executions).
- Historial COMPLETO de operativa desde la base de datos de NT8, funciona con NT8 cerrado
  (nt8_history_trades, nt8_history_performance, nt8_history_orders...).
- Historico de precios (nt8_data_inventory, nt8_bars, nt8_bars_stats).
- Codigo NinjaScript (nt8_source_*), plantillas y estrategias.
En cuentas REALES no puede enviar, modificar ni cancelar ordenes, ni aplanar posiciones, ni
arrancar/parar estrategias: no lo intentes ni ofrezcas hacerlo; indica al usuario que lo haga en NinjaTrader.
Empieza por nt8_help o nt8_status. Las horas son locales del PC salvo que se indique.
"""
if CFG.allow_sim_trading:
    INSTRUCTIONS += """\
Modo simulador (allow_sim_trading=true, AddOn v3.1): las herramientas nt8_sim_* (sim_accounts, sim_order,
sim_cancel, sim_flatten, sim_strategy_start, sim_strategy_stop, sim_compile) escriben SOLO en cuentas
Provider.Simulator (Sim101, Sim102, SimMNQ...). El AddOn rechaza cualquier otra cuenta. Para probar ordenes
usa una cuenta sim sin estrategias del mismo instrumento, para no descuadrar sus posiciones.
"""

mcp = FastMCP("ninjatrader8", instructions=INSTRUCTIONS)

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
LOCAL_FILE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
LOCAL_OVERWRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)

# name -> {"fn", "group", "core"}; lo usa tambien la pasarela REST
REGISTRY: dict[str, dict[str, Any]] = {}

_HANDLED = (NT8Error, DBError, SourceError, ValueError, KeyError, FileNotFoundError, OSError, TypeError)


def _fmt(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1, default=str)


def nt8_tool(group: str, annotations: ToolAnnotations = READ, core: bool = False, title: str | None = None):
    """Registra una herramienta: captura errores conocidos y los devuelve como 'ERROR: ...'."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> str:
            try:
                result = fn(*args, **kwargs)
            except BlockedError as exc:
                return f"ERROR: {exc}"
            except _HANDLED as exc:
                return f"ERROR: {exc}"
            return result if isinstance(result, str) else _fmt(result)

        REGISTRY[fn.__name__] = {"fn": wrapper, "group": group, "core": core}
        return mcp.tool(annotations=annotations, title=title, structured_output=False)(wrapper)

    return decorator


def _account(account: str | None) -> str | None:
    return account or CFG.default_account


def _require_v3(feature: str) -> None:
    if not NT8.supports_v3():
        if not NT8.info():
            if NT8.last_error and "No puedo conectar" not in NT8.last_error:
                raise NT8Error(f"El puente de NinjaTrader responde con un error: {NT8.last_error}")
            raise NT8Error("NinjaTrader no esta disponible (¿cerrado?). " + feature
                           + " necesita NT8 abierto con el AddOn MCPBridge v3.")
        raise NT8Error(f"{feature} necesita el AddOn MCPBridge v3. Copia ninjatrader/MCPBridge.cs a "
                       "Documents\\NinjaTrader 8\\bin\\Custom\\AddOns y compila con F5 (cuando estes plano).")


def _live_accounts() -> list[str]:
    return [a["name"] for a in NT8.call("accounts")]


# =====================================================================
#  Meta
# =====================================================================

@nt8_tool("meta", core=True, title="Ayuda y mapa de herramientas")
def nt8_help() -> dict:
    """Mapa de todo lo que puede hacer este servidor, flujos recomendados y politica de solo lectura.
    Empieza por aqui si no sabes que herramienta usar."""
    groups: dict[str, list[str]] = defaultdict(list)
    for name, meta in REGISTRY.items():
        if name in _ACTIVE_TOOLS:
            groups[meta["group"]].append(name)
    flujos = {
            "estado_cuentas": "nt8_accounts_overview -> nt8_positions -> nt8_orders",
            "rendimiento_real": "nt8_history_performance(account, since='30d', group_by='day') -> nt8_history_trades",
            "cuenta_prop": "nt8_history_performance(account, initial_balance=50000, trailing_drawdown=2500)",
            "historico_precios": "nt8_data_inventory('MNQ') -> nt8_bars -> nt8_bars_stats",
            "codigo": "nt8_source_list -> nt8_source_outline -> nt8_source_read / nt8_source_search",
    }
    # Solo los flujos cuyas herramientas estan activas (en la Lite desaparecen los de la Pro)
    flujos = {k: v for k, v in flujos.items()
              if all(t in _ACTIVE_TOOLS for t in re.findall(r"nt8_[a-z_]+", v)) or k == "historico_precios"}
    if EDITION == "lite":
        flujos["historico_precios"] = "nt8_data_inventory('MNQ') -> nt8_bars -> nt8_bars_stats"
        flujos["cuenta_prop"] = "nt8_history_performance(account, initial_balance=50000, trailing_drawdown=2500)"
    return {
        "servidor": f"NT8-MCP v{VERSION} ({EDITION}) — NinjaTrader 8 para cualquier IA",
        "politica": "SOLO LECTURA de ordenes: no existe forma de enviar, modificar o cancelar ordenes desde aqui.",
        "perfil": _PROFILE,
        "edicion": EDITION,
        "edicion_motivo": EDITION_REASON,
        "herramientas_por_grupo": dict(groups),
        "flujos": flujos,
        "horas": "Hora local del PC (la base de datos de NT8 guarda UTC; se convierte).",
    }


@nt8_tool("meta", core=True, title="Estado del servidor y de NinjaTrader")
def nt8_status() -> dict:
    """Estado general: conexion con NT8 (version del AddOn), base de datos de historial, rutas,
    avisos de configuracion y politica de seguridad. Funciona aunque NT8 este cerrado."""
    info = NT8.info(max_age=0)
    bridge = {
        "reachable": bool(info),
        "host": CFG.host, "port": CFG.port,
        "addon_version": info.get("bridge_version") or ("2.x (antiguo)" if info else None),
        "read_only_addon": bool(info.get("read_only_orders")),
    }
    if info:
        bridge.update({k: info.get(k) for k in ("server_time", "connections", "accounts")})
        if not info.get("read_only_orders"):
            bridge["aviso"] = ("El AddOn instalado en NT8 es anterior a v3: aun contiene codigo de ordenes (este "
                               "servidor no lo usa). Instala ninjatrader/MCPBridge.cs v3 cuando estes plano.")
    else:
        bridge["hint"] = "NT8 cerrado o AddOn no cargado. Historial (nt8_history_*), vault y codigo siguen disponibles."
        if NT8.last_error and "No puedo conectar" not in NT8.last_error:
            bridge["error"] = NT8.last_error  # p. ej. token invalido: NT8 si esta abierto
    db = {"path": str(DB.path), "available": DB.available()}
    if DB.available():
        try:
            db.update(DB.summary())
        except DBError as exc:
            db["error"] = str(exc)
    return {
        "version": VERSION,
        "profile": _PROFILE,
        "edition": EDITION,
        "edition_reason": EDITION_REASON,
        "tools_active": len(_ACTIVE_TOOLS),
        "bridge": bridge,
        "history_db": db,
        "paths": {
            "ninjatrader": str(CFG.nt8_root), "custom": str(CFG.custom_root), "vault": str(VAULT.root),
            "vault_extra": [str(p) for p in VAULT.extra_dirs], "data_exports": str(CFG.data_root),
        },
        "security": {
            "orders": "BLOQUEADO (enviar/modificar/cancelar/aplanar/arrancar/parar estrategias)",
            "source_write": CFG.allow_source_write,
            "source_write_scope": "Strategies, Indicators y similares; AddOns y MCP* protegidos siempre",
            "reflection": CFG.allow_reflection,
            "sim_trading": CFG.allow_sim_trading,
            "sim_trading_scope": "solo cuentas Provider.Simulator (nt8_sim_*; AddOn v3.1); las reales siguen bloqueadas",
        },
        "config_source": CFG.source,
        "warnings": CFG.warnings,
    }


@nt8_tool("meta", title="Ping al puente")
def nt8_ping() -> Any:
    """Comprueba la conexion con el AddOn MCPBridge dentro de NinjaTrader."""
    return NT8.call("ping", timeout=8)


@nt8_tool("meta", title="Rutas en uso")
def nt8_paths() -> dict:
    """Rutas que usa el servidor: carpeta de NT8, Custom, vault, exportaciones y base de datos."""
    return {
        "ninjatrader_root": str(CFG.nt8_root), "ninjatrader_root_exists": CFG.nt8_root.exists(),
        "custom_root": str(CFG.custom_root), "vault_root": str(VAULT.root),
        "vault_extra": [str(p) for p in VAULT.extra_dirs], "data_root": str(CFG.data_root),
        "history_db": str(DB.path), "history_db_exists": DB.available(), "config": CFG.source,
    }


@nt8_tool("meta", title="Politica de seguridad")
def nt8_security_policy() -> dict:
    """Que esta permitido y que esta bloqueado, incluida la politica de reflexion del AddOn."""
    out: dict[str, Any] = {
        "ordenes": "BLOQUEADAS. Ninguna herramienta envia, modifica o cancela ordenes, aplana posiciones, "
                   "ni arranca/para estrategias. No hay opcion de configuracion que lo active.",
        "capas": [
            "1) Servidor MCP: no existe ninguna herramienta de ordenes.",
            "2) Cliente TCP: rechaza localmente los comandos de ordenes (BlockedError).",
            "3) AddOn MCPBridge v3: el codigo de ordenes no existe; los comandos antiguos devuelven BLOQUEADO; "
            "la reflexion solo puede escribir en objetos propios o del Strategy Analyzer y nunca invocar "
            "Submit/Cancel/Change/Flatten/Enter/Exit/Atm*/SetState...",
            "4) Codigo NinjaScript: AddOns y ficheros MCP* no se pueden escribir, para que nadie reescriba el puente.",
        ],
        "permitido": ["leer cuentas, posiciones, ordenes, ejecuciones e historial", "historico de precios y exportaciones CSV",
                      "backtests (vault, motor rapido, Strategy Analyzer en cuenta Backtest)",
                      "leer codigo; escribir estrategias/indicadores si allow_source_write=true"],
        "addon_instalado": NT8.info().get("bridge_version") or "desconocido/antiguo",
    }
    if NT8.supports_v3():
        out["reflexion_addon"] = NT8.call("reflect_policy")
    return out


# =====================================================================
#  Cuentas en vivo (NT8 abierto)
# =====================================================================

@nt8_tool("cuentas", core=True, title="Cuentas")
def nt8_accounts() -> Any:
    """Cuentas de NT8 (sim, prop, broker) con conexion y estado. Si NT8 esta cerrado, devuelve las
    cuentas conocidas en la base de datos de historial."""
    try:
        return NT8.call("accounts")
    except NT8Error as exc:
        if DB.available():
            return {"source": "history_db (NT8 no disponible)", "reason": str(exc), "accounts": DB.accounts()}
        raise


@nt8_tool("cuentas", core=True, title="Resumen de todas las cuentas")
def nt8_accounts_overview(include_empty: bool = False) -> Any:
    """Todas las cuentas de un vistazo: conexion, cash, net liquidation, PnL realizado y flotante,
    posiciones abiertas y ordenes vivas. include_empty=false oculta cuentas desconectadas sin actividad."""
    if NT8.supports_v3():
        rows = NT8.call("accounts_overview")
    else:
        rows = []
        for name in _live_accounts():
            try:
                rows.append(NT8.call("account_status", account=name))
            except NT8Error as exc:
                rows.append({"account": name, "error": str(exc)})
    if not include_empty:
        rows = [r for r in rows if r.get("connected") or r.get("open_positions") or r.get("working_orders")
                or r.get("realized_pnl")]
    return rows


@nt8_tool("cuentas", core=True, title="Estado de una cuenta")
def nt8_account_status(account: str | None = None) -> Any:
    """Estado financiero de una cuenta: cash, net liquidation, PnL realizado y flotante, buying power,
    posiciones y ordenes vivas."""
    return NT8.call("account_status", account=_account(account))


@nt8_tool("cuentas", title="Todos los valores de una cuenta")
def nt8_account_items(account: str) -> Any:
    """TODOS los AccountItem de una cuenta: margenes, comisiones, SOD, y en cuentas prop
    TrailingMaxDrawdown, DailyLossLimit, WeeklyProfitLoss... Con NT8 cerrado, ultimo valor guardado."""
    if NT8.supports_v3():
        return NT8.call("account_items", account=account)
    try:
        return {"source": "history_db", **DB.account_items(account)}
    except DBError:
        return NT8.call("account_status", account=account)


@nt8_tool("cuentas", core=True, title="Posiciones abiertas")
def nt8_positions(account: str = "all") -> Any:
    """Posiciones abiertas con precio medio y PnL flotante. account='all' recorre todas las cuentas."""
    if NT8.supports_v3():
        return NT8.call("positions", account=account)
    names = _live_accounts() if account in (None, "", "all", "*") else [account]
    out = []
    for name in names:
        out.extend(NT8.call("positions", account=name))
    return out


@nt8_tool("cuentas", core=True, title="Ordenes de la sesion")
def nt8_orders(account: str = "all", active_only: bool = True, limit: int = 100, instrument: str | None = None) -> Any:
    """Ordenes de la sesion actual de NT8 (active_only=true: solo vivas). Para el historial completo
    usa nt8_history_orders."""
    if NT8.supports_v3():
        return NT8.call("orders", account=account, active_only=active_only, limit=limit, instrument=instrument)
    names = _live_accounts() if account in (None, "", "all", "*") else [account]
    out = []
    for name in names:
        rows = NT8.call("orders", account=name, active_only=active_only, limit=limit)
        for r in rows:
            r["account"] = name
        out.extend(r for r in rows if not instrument or instrument.lower() in r["instrument"].lower())
    return out[:limit]


@nt8_tool("cuentas", core=True, title="Ejecuciones de la sesion")
def nt8_executions(account: str = "all", limit: int = 100, instrument: str | None = None,
                   since: str | None = None) -> Any:
    """Fills de la sesion actual de NT8 (precio, hora, comision). Para meses de historial usa
    nt8_history_executions / nt8_history_trades."""
    if NT8.supports_v3():
        return NT8.call("executions", account=account, limit=limit, instrument=instrument, since=since)
    names = _live_accounts() if account in (None, "", "all", "*") else [account]
    out = []
    for name in names:
        rows = NT8.call("executions", account=name, limit=limit)
        for r in rows:
            r["account"] = name
        out.extend(r for r in rows if not instrument or instrument.lower() in r["instrument"].lower())
    out.sort(key=lambda r: r.get("time") or "", reverse=True)
    return out[:limit]


@nt8_tool("cuentas", title="Conexiones")
def nt8_connections() -> Any:
    """Conexiones de NT8 (broker/datos): estado, estado de precios y cuentas asociadas."""
    _require_v3("nt8_connections")
    return NT8.call("connections")


@nt8_tool("cuentas", title="Feed de eventos")
def nt8_events(since: int = 0, kind: str | None = None, limit: int = 200) -> Any:
    """Eventos en tiempo real capturados por el AddOn: order, execution, position, connection.
    Pasa since=last_seq de la llamada anterior para recibir solo lo nuevo (polling incremental)."""
    _require_v3("nt8_events")
    return NT8.call("events", since=since, kind=kind, limit=limit)


@nt8_tool("cuentas", title="Log del puente")
def nt8_bridge_log(limit: int = 50) -> Any:
    """Log interno del AddOn: arranque, rechazos de ordenes vistos, exportaciones, errores."""
    return NT8.call("log", limit=limit)


# =====================================================================
#  Historial de cuenta (SQLite de NT8, sin necesidad de NT8 abierto)
# =====================================================================

def _trades_page(trades: list[Trade], offset: int, limit: int) -> dict:
    limit = max(1, min(1000, limit))
    page = trades[offset: offset + limit]
    return {"total": len(trades), "offset": offset, "returned": len(page), "trades": [t.as_dict() for t in page]}


@nt8_tool("historial", title="Resumen de la base de datos")
def nt8_history_summary() -> dict:
    """Que hay en la base de datos de NT8: numero de ordenes, ejecuciones y estrategias, y rango de fechas."""
    return DB.summary()


@nt8_tool("historial", title="Cuentas con historial")
def nt8_history_accounts() -> list:
    """Cuentas registradas en la base de datos con numero de ejecuciones y primera/ultima operacion."""
    return DB.accounts()


@nt8_tool("historial", core=True, title="Ejecuciones historicas")
def nt8_history_executions(account: str | None = None, instrument: str | None = None, since: str | None = None,
                           until: str | None = None, limit: int = 200, offset: int = 0) -> dict:
    """Todas las ejecuciones guardadas por NT8 (meses). Filtros: account, instrument ('MNQ' o 'MNQ DEC26'),
    since/until ('2026-09-01', 'hoy', 'ayer', '7d', '3m'). Paginado con offset/limit."""
    return DB.executions(account, instrument, since, until, limit, offset)


@nt8_tool("historial", core=True, title="Ordenes historicas")
def nt8_history_orders(account: str | None = None, instrument: str | None = None, since: str | None = None,
                       until: str | None = None, state: str | None = None, limit: int = 200, offset: int = 0) -> dict:
    """Historial de ordenes con su estado final y ultimo comentario del broker. state: Filled, Cancelled,
    Rejected, Working..."""
    return DB.orders(account, instrument, since, until, state, limit, offset)


@nt8_tool("historial", core=True, title="Operaciones reales")
def nt8_history_trades(account: str | None = None, instrument: str | None = None, since: str | None = None,
                       until: str | None = None, offset: int = 0, limit: int = 100, newest_first: bool = True) -> dict:
    """Operaciones reales de ida y vuelta (FIFO) reconstruidas desde las ejecuciones, con PnL en dinero
    neto de comisiones. Mismo formato que las operaciones de un backtest. Por defecto, las mas recientes
    primero (como ordenes y ejecuciones); newest_first=false para orden cronologico (p. ej. para simular)."""
    trades = DB.trades(account, instrument, since, until)
    if newest_first:
        trades = trades[::-1]
    return _trades_page(trades, offset, limit)


def _group_key(t: Trade, by: str) -> str:
    stamp = t.exit_time or t.entry_time
    if by == "day":
        return stamp.date().isoformat() if stamp else "?"
    if by == "week":
        if not stamp:
            return "?"
        y, w, _ = stamp.isocalendar()
        return f"{y}-W{w:02d}"
    if by == "month":
        return stamp.strftime("%Y-%m") if stamp else "?"
    if by == "hour":
        return f"{t.entry_time.hour:02d}h" if t.entry_time else "?"
    if by == "weekday":
        return ["Lun", "Mar", "Mie", "Jue", "Vie", "Sab", "Dom"][t.entry_time.weekday()] if t.entry_time else "?"
    if by == "instrument":
        return (t.instrument or "?").split(" ")[0]
    if by == "direction":
        return t.direction or "?"
    if by == "account":
        return getattr(t, "account", None) or "?"
    raise ValueError("group_by debe ser day, week, month, hour, weekday, instrument, direction o account")


def _grouped(trades: list[Trade], by: str) -> list[dict]:
    groups: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        groups[_group_key(t, by)].append(t)
    rows = []
    for key in sorted(groups):
        g = groups[key]
        wins = [t.profit for t in g if t.profit > 0]
        losses = [t.profit for t in g if t.profit < 0]
        rows.append({
            by: key, "trades": len(g), "net": round(sum(t.profit for t in g), 2),
            "win_rate": round(len(wins) / max(1, len(wins) + len(losses)), 3),
            "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses else None,
            "best": round(max(t.profit for t in g), 2), "worst": round(min(t.profit for t in g), 2),
        })
    return rows


@nt8_tool("historial", core=True, title="Rendimiento real")
def nt8_history_performance(account: str | None = None, instrument: str | None = None, since: str | None = None,
                            until: str | None = None, group_by: str = "day", initial_balance: float | None = None,
                            trailing_drawdown: float | None = None, end_of_day_trailing: bool = True) -> dict:
    """Metricas de tu operativa REAL (win rate con IC, profit factor, expectancy, drawdown, rachas,
    desglose por hora/dia) + tabla agrupada (group_by: day, week, month, hour, weekday, instrument,
    direction, account). Con initial_balance y trailing_drawdown simula la regla de la prop firm."""
    trades = DB.trades(account, instrument, since, until)
    if not trades:
        return {"trades": 0, "note": "sin operaciones cerradas con esos filtros"}
    out: dict[str, Any] = {
        "filters": {"account": account, "instrument": instrument, "since": since, "until": until},
        "metrics": analyze(trades),
        "grouped": _grouped(trades, group_by),
    }
    if initial_balance and trailing_drawdown:
        out["prop_survival"] = simulate_trailing_drawdown(trades, initial_balance, trailing_drawdown, end_of_day_trailing)
    return out


# =====================================================================
#  Mercado e instrumentos
# =====================================================================

@nt8_tool("mercado", core=True, title="Ficha de instrumento")
def nt8_instrument_info(instrument: str) -> Any:
    """Tick size, point value, valor del tick, vencimiento, horario y contratos disponibles.
    Acepta 'MNQ' (contrato activo) o 'MNQ DEC26' / 'MNQ 12-26'."""
    try:
        return NT8.call("instrument_info", instrument=instrument)
    except NT8Error as exc:
        if not NT8.info() and DB.available():
            hits = DB.instrument_search(instrument.split(" ")[0], 1)
            if hits:
                return {"source": "history_db (NT8 no disponible)", **hits[0]}
        raise exc


@nt8_tool("mercado", title="Buscar instrumentos")
def nt8_instrument_search(query: str, type: str | None = None, limit: int = 30) -> Any:
    """Busca instrumentos por nombre o descripcion ('nasdaq', 'gold', 'ES'). type: Future, Stock, Forex, Index..."""
    if NT8.supports_v3():
        return NT8.call("instrument_search", query=query, type=type, limit=limit)
    rows = DB.instrument_search(query, limit * 3)
    if type:
        rows = [r for r in rows if (r["type"] or "").lower() == type.lower()]
    return rows[:limit]


@nt8_tool("mercado", title="Horarios de sesion")
def nt8_trading_hours(name: str | None = None, instrument: str | None = None) -> Any:
    """Plantilla de trading hours (sesiones, zona horaria, festivos). Sin argumentos lista las plantillas."""
    _require_v3("nt8_trading_hours")
    return NT8.call("trading_hours", name=name, instrument=instrument)


@nt8_tool("mercado", core=True, title="Cotizacion")
def nt8_quote(instrument: str, retries: int = 3) -> Any:
    """Last, bid, ask, spread y volumen. Requiere feed en vivo; reintenta mientras ready=false."""
    last: Any = None
    for attempt in range(max(1, retries)):
        last = NT8.call("quote", instrument=instrument)
        if isinstance(last, dict) and last.get("ready"):
            return last
        if attempt < retries - 1:
            time.sleep(1.0)
    return last


@nt8_tool("mercado", title="Cotizaciones multiples")
def nt8_quotes(instruments: list[str]) -> Any:
    """Cotizacion de varios instrumentos a la vez (maximo 50)."""
    if NT8.supports_v3():
        return NT8.call("quotes", instruments=instruments)
    return [NT8.call("quote", instrument=i) for i in instruments[:20]]


# =====================================================================
#  Historico de precios
# =====================================================================

@nt8_tool("historico", core=True, title="Inventario de datos historicos")
def nt8_data_inventory(instrument: str | None = None) -> dict:
    """Que datos historicos hay descargados en NT8 (tick, minute, day) por contrato y fechas.
    Funciona sin NT8 abierto."""
    return H.data_inventory(CFG.nt8_root, instrument)


@nt8_tool("historico", core=True, title="Barras OHLCV")
def nt8_bars(instrument: str, period_type: str = "Minute", period_value: int = 1, from_date: str | None = None,
             to_date: str | None = None, count: int = 300, trading_hours: str | None = None,
             market_data: str | None = None, base_period_type: str | None = None,
             base_period_value: int | None = None, value2: int | None = None, offset: int = 0,
             limit: int = 2000, output: str = "json") -> Any:
    """Barras historicas de NT8. period_type: Tick, Volume, Range, Second, Minute, Day, Week, Month, Year,
    Renko, HeikenAshi, Kagi, LineBreak, PointAndFigure... (HeikenAshi y similares usan base_period_type/value).
    Sin fechas: ultimas 'count' barras. Con from_date/to_date ('YYYY-MM-DD' o 'YYYY-MM-DD HH:mm'): el rango,
    paginado con offset/limit (max 20000). trading_hours: plantilla ('CME US Index Futures RTH').
    market_data: Last (defecto), Bid o Ask. output='csv' devuelve texto CSV compacto."""
    data = H.fetch_bars(NT8, instrument, period_type, period_value, from_date, to_date, count, trading_hours,
                        market_data, base_period_type, base_period_value, value2, offset, limit)
    if output == "csv":
        lines = ["time,open,high,low,close,volume"] + [
            f"{b['time']},{b['open']},{b['high']},{b['low']},{b['close']},{b['volume']}" for b in data["bars"]]
        meta = {k: v for k, v in data.items() if k != "bars"}
        return json.dumps(meta, ensure_ascii=False) + "\n" + "\n".join(lines)
    return data


@nt8_tool("historico", title="Ficheros de datos exportados")
def nt8_data_files() -> Any:
    """CSV de barras disponibles en mcp_data (exportaciones previas), reutilizables sin NT8."""
    return {"folder": str(CFG.data_root), "files": H.list_data_files(CFG.data_root)}


@nt8_tool("historico", title="Leer CSV de barras")
def nt8_bars_file(file: str, offset: int = 0, limit: int = 500, start: str | None = None, end: str | None = None,
                  time_column: str | None = None) -> dict:
    """Lee barras de un CSV (de mcp_data o una ruta absoluta), filtrando por start/end (ISO) y paginando."""
    path = H.resolve_data_file(CFG.data_root, file)
    bars = H.load_bars_cached(path, time_column)
    if start or end:
        s = datetime.fromisoformat(start) if start else None
        e = datetime.fromisoformat(end) if end else None
        bars = [b for b in bars if (not s or b["time"] >= s) and (not e or b["time"] <= e)]
    limit = max(1, min(20000, limit))
    page = bars[offset: offset + limit]
    return {"file": str(path), "total": len(bars), "offset": offset, "returned": len(page),
            "bars": [{**b, "time": b["time"].isoformat()} for b in page]}


@nt8_tool("historico", core=True, title="Estadisticas de precio")
def nt8_bars_stats(instrument: str | None = None, period_type: str = "Minute", period_value: int = 1,
                   from_date: str | None = None, to_date: str | None = None, count: int = 2000,
                   file: str | None = None, trading_hours: str | None = None) -> dict:
    """Estadisticas de un tramo de precio: cambio, maximos/minimos, ATR (y en ticks), volatilidad, rango y
    volumen medio por hora, y tabla diaria OHLC. Fuente: NT8 (instrument...) o un CSV (file)."""
    tick = None
    if file:
        bars = H.load_bars_cached(H.resolve_data_file(CFG.data_root, file))
    else:
        if not instrument:
            raise ValueError("indica instrument o file")
        bars = _bars_for_analysis(instrument, period_type, period_value, from_date, to_date, count, trading_hours)
        tick = _instrument_specs(instrument).get("tick_size")
    return H.bar_stats(bars, tick)


_SPECS_CACHE: dict[str, dict] = {}


def _instrument_specs(instrument: str) -> dict:
    key = instrument.upper()
    if key in _SPECS_CACHE:
        return _SPECS_CACHE[key]
    specs: dict[str, Any] = {}
    try:
        info = NT8.call("instrument_info", instrument=instrument)
        specs = {"tick_size": info.get("tick_size"), "point_value": info.get("point_value"),
                 "full_name": info.get("full_name")}
    except NT8Error:
        hits = DB.instrument_search(instrument.split(" ")[0], 1) if DB.available() else []
        if hits and hits[0]["name"].upper() == instrument.split(" ")[0].upper():
            specs = {"tick_size": hits[0]["tick_size"], "point_value": hits[0]["point_value"]}
    _SPECS_CACHE[key] = specs
    return specs


def _bars_for_analysis(instrument: str, period_type: str, period_value: int, from_date: str | None,
                       to_date: str | None, count: int | None, trading_hours: str | None) -> list[dict]:
    """Barras listas para analizar (datetime). Rangos -> exportacion CSV (se reutiliza si ya existe)."""
    if from_date and NT8.supports_v3():
        fname = (f"{instrument.replace(' ', '_')}_{period_value}{period_type}_{from_date}_{to_date or 'hoy'}"
                 f"{'_' + trading_hours.replace(' ', '') if trading_hours else ''}.csv").replace(":", "")
        path = CFG.data_root / fname
        fresh = path.exists() and (to_date and to_date < datetime.now().strftime("%Y-%m-%d")
                                   or time.time() - path.stat().st_mtime < 300)
        if not fresh:
            res = H.export_bars(NT8, CFG.data_root, instrument, period_type, period_value, from_date, to_date,
                                None, trading_hours, None, None, None, None, fname, wait_seconds=110)
            if res.get("status") == "running":
                raise NT8Error(f"la exportacion sigue en curso (job {res.get('job_id')}); repite en un momento")
            if res.get("status") not in ("done", "empty"):
                raise NT8Error(f"exportacion fallida: {res.get('error') or res}")
            path = Path(res["file"])
        return H.load_bars_cached(path)
    data = H.fetch_bars(NT8, instrument, period_type, period_value, from_date, to_date, count or 5000,
                        trading_hours, limit=20000)
    return H.normalize_bars(data["bars"])


# =====================================================================
#  Backtests: vault (CSV del Strategy Analyzer)
# =====================================================================

# =====================================================================
#  Backtests: motor rapido de reglas
# =====================================================================

# =====================================================================
#  Backtests: Strategy Analyzer de NT8 y estrategias
# =====================================================================

@nt8_tool("estrategias", core=True, title="Estrategias cargadas")
def nt8_strategies(account: str | None = None) -> Any:
    """Estrategias NinjaScript cargadas ahora en las cuentas: nombre, clase, estado, instrumento y posicion."""
    return NT8.call("strategies", account=_account(account))


# =====================================================================
#  Escritura SOLO en cuentas del simulador (v3.1: MCPSim.cs + allow_sim_trading)
#
#  Las cuentas reales siguen en solo lectura: el AddOn rechaza en cada comando
#  cualquier cuenta que no sea Provider.Simulator, y los comandos antiguos de
#  ordenes siguen BLOQUEADOS en las tres capas.
# =====================================================================

import time as _time  # noqa: E402

@nt8_tool("estrategias", title="Plantillas ATM")
def nt8_atm_templates(read: str | None = None) -> dict:
    """Plantillas ATM (templates\\AtmStrategy): lista, o con read='nombre' devuelve sus parametros
    (stops, targets, auto breakeven, trailing). Solo lectura."""
    folder = CFG.nt8_root / "templates" / "AtmStrategy"
    if not folder.exists():
        return {"folder": str(folder), "templates": []}
    if read:
        target = folder / (read if read.lower().endswith(".xml") else read + ".xml")
        return read_strategy_template(target)
    return {"folder": str(folder), "templates": sorted(p.stem for p in folder.glob("*.xml"))}


# =====================================================================
#  Codigo NinjaScript
# =====================================================================

@nt8_tool("codigo", core=True, title="Listar ficheros NinjaScript")
def nt8_source_list(folder: str | None = None, pattern: str | None = None) -> dict:
    """Ficheros .cs de NinjaScript (Strategies, Indicators, AddOns, BarsTypes...). pattern filtra por ruta."""
    return {"root": str(CFG.custom_root), "files": SOURCES.list_files(folder, pattern)}


@nt8_tool("codigo", title="Esqueleto de un fichero")
def nt8_source_outline(path: str) -> dict:
    """Clases, metodos y propiedades de un .cs con su numero de linea. Usalo antes de leer ficheros grandes."""
    return SOURCES.outline(path)


@nt8_tool("codigo", core=True, title="Leer fichero NinjaScript")
def nt8_source_read(path: str, start_line: int = 1, max_lines: int = 800) -> dict:
    """Lee un tramo de un .cs. Ejemplo: path='Strategies/MiEstrategia.cs'."""
    return SOURCES.read(path, start_line, max_lines)


@nt8_tool("codigo", title="Buscar en el codigo")
def nt8_source_search(pattern: str, folder: str | None = None, regex: bool = False, max_results: int = 100) -> dict:
    """Busca texto (o regex) en todos los .cs de NinjaScript. Devuelve fichero, linea y contexto."""
    try:
        rx = re.compile(pattern if regex else re.escape(pattern), re.IGNORECASE)
    except re.error as exc:
        return f"ERROR: expresion regular no valida ({exc})"
    hits = []
    for f in SOURCES.list_files(folder):
        path = CFG.custom_root / f["path"]
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, start=1):
            if rx.search(line):
                hits.append({"path": f["path"], "line": i, "text": line.strip()[:200]})
                if len(hits) >= max_results:
                    return {"pattern": pattern, "truncated": True, "hits": hits}
    return {"pattern": pattern, "truncated": False, "hits": hits}


# =====================================================================
#  Reflexion (lectura libre; escritura limitada por el AddOn)
# =====================================================================

# =====================================================================
#  Recursos y prompts MCP
# =====================================================================

@mcp.resource("nt8://status", name="status", description="Estado del servidor, del AddOn y de la base de datos",
              mime_type="application/json")
def res_status() -> str:
    return REGISTRY["nt8_status"]["fn"]()


@mcp.resource("nt8://help", name="help", description="Mapa de herramientas y flujos", mime_type="application/json")
def res_help() -> str:
    return REGISTRY["nt8_help"]["fn"]()


@mcp.resource("nt8://accounts/overview", name="accounts_overview", description="Resumen de cuentas en vivo",
              mime_type="application/json")
def res_accounts() -> str:
    return REGISTRY["nt8_accounts_overview"]["fn"]()


@mcp.resource("nt8://positions", name="positions", description="Posiciones abiertas en todas las cuentas",
              mime_type="application/json")
def res_positions() -> str:
    return REGISTRY["nt8_positions"]["fn"]()


@mcp.resource("nt8://history/summary", name="history_summary", description="Resumen de la base de datos de NT8",
              mime_type="application/json")
def res_history() -> str:
    return REGISTRY["nt8_history_summary"]["fn"]()


@mcp.prompt(name="revision_diaria", description="Revision del dia: cuentas, posiciones y operaciones de hoy")
def prompt_daily(account: str = "all") -> str:
    return (f"Haz una revision de mi jornada en NinjaTrader para la cuenta '{account}'. Usa nt8_accounts_overview y "
            f"nt8_positions para el estado actual, y nt8_history_performance(account='{account}', since='hoy', "
            "group_by='hour') para lo operado hoy. Resume PnL y "
            "patrones. Recuerda: no puedes enviar ni modificar ordenes.")


@mcp.prompt(name="salud_cuenta_prop", description="Riesgo de una cuenta fondeada frente a su trailing drawdown")
def prompt_prop(account: str, initial_balance: float = 50000, trailing_drawdown: float = 2500) -> str:
    return (f"Evalua la salud de la cuenta prop '{account}' (saldo inicial {initial_balance}, trailing "
            f"{trailing_drawdown}). Usa nt8_account_items('{account}') para TrailingMaxDrawdown y limites, "
            f"nt8_history_performance(account='{account}', initial_balance={initial_balance}, "
            f"trailing_drawdown={trailing_drawdown}, group_by='day'). Dame el colchon actual, la "
            "distancia al suelo en perdidas tipicas y que dias/horas destruyen valor.")


# =====================================================================
#  Perfil de herramientas y arranque
# =====================================================================

def _decide_edition() -> tuple[str, str]:
    """Lite si se pide o si no hay licencia Pro valida. Nunca falla: ante cualquier duda, Lite."""
    wanted = (CFG.edition or "pro").lower()
    if wanted != "pro":
        return "lite", "Edicion Lite elegida en la configuracion."
    try:
        from .license import check  # la distribucion Lite no incluye este modulo
    except ImportError:
        return "lite", "Edicion Lite."
    state = check(CFG.license_key)
    return state.edition, state.reason


EDITION, EDITION_REASON = _decide_edition()
if EDITION == "lite":
    for _name in list(REGISTRY):
        if _name not in LITE_TOOLS:
            mcp.remove_tool(_name)
            REGISTRY.pop(_name)
    for _p in PRO_PROMPTS:
        try:
            mcp._prompt_manager._prompts.pop(_p, None)  # noqa: SLF001 (FastMCP no expone remove_prompt)
        except AttributeError:
            pass

_PROFILE = (os.environ.get("NT8_MCP_PROFILE") or "full").lower()
if _PROFILE == "core":
    for _name, _meta in list(REGISTRY.items()):
        if not _meta["core"]:
            mcp.remove_tool(_name)
_ACTIVE_TOOLS = {n for n, m in REGISTRY.items() if _PROFILE != "core" or m["core"]}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
