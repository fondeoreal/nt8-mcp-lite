"""Lectura (solo lectura) de la base de datos SQLite de NinjaTrader 8.

Documents\\NinjaTrader 8\\db\\NinjaTrader.sqlite guarda TODO el historial: cuentas, ordenes,
actualizaciones de ordenes (con motivo de rechazo), ejecuciones y estrategias. A diferencia
de la API en vivo (que solo ve la sesion actual), aqui estan meses de operativa, y se puede
leer con NinjaTrader cerrado.

- Se abre con mode=ro: nunca se escribe. Si NT8 la tiene bloqueada, se lee una copia temporal.
- Las fechas se guardan como ticks .NET en UTC; se devuelven en hora local e ISO 8601.
- Los enums (estado de orden, accion...) se decodifican con data/nt_enums.json, extraido de
  NinjaTrader.Core.dll de esta misma version.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .vault import Trade

_EPOCH = datetime(1, 1, 1)
_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
_MIN_DOTNET = -1.7e308


class DBError(RuntimeError):
    pass


def ticks_to_utc(ticks: int | None) -> datetime | None:
    if not ticks or ticks <= 0:
        return None
    try:
        return _EPOCH + timedelta(microseconds=int(ticks) // 10)
    except OverflowError:
        return None


def ticks_to_local(ticks: int | None) -> datetime | None:
    dt = ticks_to_utc(ticks)
    if dt is None or dt.year < 1900 or dt.year > 2200:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


def local_to_ticks(dt: datetime) -> int:
    utc = dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else \
        dt.replace(tzinfo=datetime.now().astimezone().tzinfo).astimezone(timezone.utc).replace(tzinfo=None)
    return int((utc - _EPOCH) / timedelta(microseconds=1)) * 10


def parse_when(value: str | None) -> datetime | None:
    """'2026-09-01', '2026-09-01 14:30', 'hoy', 'ayer', '7d', '3m' -> datetime local."""
    if not value:
        return None
    v = str(value).strip().lower()
    now = datetime.now()
    if v in ("hoy", "today"):
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if v in ("ayer", "yesterday"):
        return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if v[:-1].isdigit() and v[-1] in "dwmy":
        n = int(v[:-1])
        days = {"d": 1, "w": 7, "m": 30, "y": 365}[v[-1]] * n
        return now - timedelta(days=days)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value).strip(), fmt)
        except ValueError:
            continue
    raise DBError(f"fecha invalida: {value!r} (usa YYYY-MM-DD, 'hoy', 'ayer', '7d', '3m'...)")


def until_bound(until: str | None) -> datetime | None:
    """Limite superior EXCLUSIVO: una fecha sin hora ('2026-09-24', 'hoy', 'ayer') incluye el dia entero."""
    u = parse_when(until)
    if u is None:
        return None
    raw = str(until).strip().lower()
    if raw in ("hoy", "today", "ayer", "yesterday") or (len(raw) <= 10 and raw[:1].isdigit() and "-" in raw):
        return u.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return u


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def _clean_price(v: Any) -> Any:
    if isinstance(v, float) and (abs(v) > 1e300 or v != v):
        return None
    return v


class Enums:
    def __init__(self, path: Path) -> None:
        try:
            self.maps: dict[str, dict[str, str]] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.maps = {}

    def name(self, enum: str, value: Any) -> Any:
        if value is None:
            return None
        m = self.maps.get(enum, {})
        return m.get(str(int(value)), f"{enum}{int(value)}") if isinstance(value, (int, float)) else value

    def value(self, enum: str, name: str) -> int | None:
        for k, v in self.maps.get(enum, {}).items():
            if v.lower() == str(name).lower():
                return int(k)
        return None


class NT8Database:
    def __init__(self, path: Path, enums_path: Path | None = None) -> None:
        self.path = Path(path)
        self.enums = Enums(enums_path or Path(__file__).parent / "data" / "nt_enums.json")
        self._lock = threading.Lock()
        self._snapshot: Path | None = None
        self._snapshot_at = 0.0
        self._cache: dict[str, Any] = {}
        self._cache_at = 0.0

    # -- conexion ---------------------------------------------------
    def available(self) -> bool:
        return self.path.exists()

    def _open(self, target: Path) -> sqlite3.Connection:
        uri = "file:" + target.as_posix().replace("?", "%3F") + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=3.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        return conn

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        if not self.available():
            raise DBError(f"No encuentro la base de datos de NT8 en {self.path}")
        with self._lock:
            try:
                conn = self._open(self.path)
                try:
                    return conn.execute(sql, tuple(params)).fetchall()
                finally:
                    conn.close()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise DBError(f"error SQLite: {exc}") from exc
            # NT8 esta escribiendo: lee una copia reciente (<= 20 s)
            now = time.monotonic()
            if self._snapshot is None or now - self._snapshot_at > 20:
                tmp = Path(tempfile.gettempdir()) / "nt8_mcp_db_snapshot.sqlite"
                shutil.copyfile(self.path, tmp)
                self._snapshot, self._snapshot_at = tmp, now
            conn = self._open(self._snapshot)
            try:
                return conn.execute(sql, tuple(params)).fetchall()
            finally:
                conn.close()

    # -- tablas de referencia (cacheadas 60 s) -------------------------
    def _ref(self) -> dict:
        now = time.monotonic()
        if self._cache and now - self._cache_at < 60:
            return self._cache
        accounts = {r["Id"]: dict(r) for r in self.query(
            "SELECT Id, Name, DisplayName, Provider, Denomination, SimulatorInitialCash, AccountStatus, "
            "LossLimit, MaxOrderSize, MaxPositionSize, Fcm FROM Accounts")}
        masters = {r["Id"]: dict(r) for r in self.query(
            "SELECT Id, Name, Description, InstrumentType, PointValue, TickSize, Currency, TradingHours FROM MasterInstruments")}
        self._cache = {"accounts": accounts, "masters": masters, "instruments": {}}
        self._cache_at = now
        return self._cache

    def _instrument(self, inst_id: int) -> dict:
        ref = self._ref()
        cached = ref["instruments"].get(inst_id)
        if cached:
            return cached
        rows = self.query("SELECT Id, MasterInstrument, Expiry FROM Instruments WHERE Id = ?", (inst_id,))
        if not rows:
            info = {"name": f"#{inst_id}", "point_value": 1.0, "tick_size": None, "master": None}
        else:
            r = rows[0]
            m = ref["masters"].get(r["MasterInstrument"], {})
            name = m.get("Name") or f"#{inst_id}"
            expiry = ticks_to_utc(r["Expiry"])
            itype = self.enums.name("InstrumentType", m.get("InstrumentType"))
            if itype == "Future" and expiry and expiry.year < 2200:
                name = f"{name} {_MONTHS[expiry.month - 1]}{expiry.year % 100:02d}"
            info = {
                "name": name,
                "master": m.get("Name"),
                "point_value": m.get("PointValue") or 1.0,
                "tick_size": m.get("TickSize"),
                "expiry": expiry.date().isoformat() if expiry and expiry.year < 2200 else None,
            }
        ref["instruments"][inst_id] = info
        return info

    def _account_ids(self, account: str | None) -> list[int] | None:
        if not account or account.lower() in ("all", "*", "todas"):
            return None
        ids = [aid for aid, a in self._ref()["accounts"].items()
               if a["Name"].lower() == account.lower() or (a.get("DisplayName") or "").lower() == account.lower()]
        if not ids:
            names = ", ".join(sorted(a["Name"] for a in self._ref()["accounts"].values()))
            raise DBError(f"cuenta '{account}' no encontrada en el historial. Disponibles: {names}")
        return ids

    def _instrument_ids(self, instrument: str | None) -> list[int] | None:
        if not instrument:
            return None
        root = instrument.strip().split(" ")[0].upper()
        masters = [mid for mid, m in self._ref()["masters"].items() if (m["Name"] or "").upper() == root]
        if not masters:
            raise DBError(f"instrumento '{instrument}' no encontrado en la base de datos")
        rows = self.query(f"SELECT Id FROM Instruments WHERE MasterInstrument IN ({','.join('?' * len(masters))})", masters)
        ids = [r["Id"] for r in rows]
        if " " in instrument.strip():  # contrato concreto: filtra por nombre compuesto
            want = instrument.strip().upper()
            ids = [i for i in ids if self._instrument(i)["name"].upper() == want] or ids
        return ids

    @staticmethod
    def _in(column: str, values: list[int] | None, where: list[str], params: list) -> None:
        if values is None:
            return
        if not values:
            where.append("0")
            return
        where.append(f"{column} IN ({','.join('?' * len(values))})")
        params.extend(values)

    def _time_filter(self, column: str, since: str | None, until: str | None, where: list[str], params: list) -> None:
        s, u = parse_when(since), until_bound(until)
        if s:
            where.append(f"{column} >= ?")
            params.append(local_to_ticks(s))
        if u:
            where.append(f"{column} < ?")
            params.append(local_to_ticks(u))

    # -- API ---------------------------------------------------------
    def summary(self) -> dict:
        counts = {}
        for table in ("Accounts", "Orders", "OrderUpdates", "Executions", "Strategies", "MasterInstruments"):
            counts[table] = self.query(f'SELECT COUNT(*) AS n FROM "{table}"')[0]["n"]
        rng = self.query("SELECT MIN(Time) AS a, MAX(Time) AS b FROM Executions")[0]
        return {
            "database": str(self.path),
            "size_mb": round(self.path.stat().st_size / 1e6, 2),
            "modified": datetime.fromtimestamp(self.path.stat().st_mtime).isoformat(timespec="seconds"),
            "tables": counts,
            "executions_from": _iso(ticks_to_local(rng["a"])),
            "executions_to": _iso(ticks_to_local(rng["b"])),
        }

    def accounts(self) -> list[dict]:
        stats = {r["Account"]: dict(r) for r in self.query(
            "SELECT Account, COUNT(*) AS n, MIN(Time) AS first, MAX(Time) AS last FROM Executions GROUP BY Account")}
        out = []
        for aid, a in sorted(self._ref()["accounts"].items(), key=lambda kv: kv[1]["Name"]):
            s = stats.get(aid, {})
            out.append({
                "name": a["Name"],
                "display_name": a.get("DisplayName"),
                "provider": self.enums.name("Provider", a.get("Provider")),
                "currency": self.enums.name("Currency", a.get("Denomination")),
                "sim_initial_cash": a.get("SimulatorInitialCash"),
                "executions": s.get("n", 0),
                "first_execution": _iso(ticks_to_local(s.get("first"))),
                "last_execution": _iso(ticks_to_local(s.get("last"))),
            })
        return out

    def account_items(self, account: str) -> dict:
        ids = self._account_ids(account)
        if not ids:
            raise DBError("indica una cuenta concreta")
        rows = self.query(
            f"SELECT ItemType, Value, Currency, MAX(TimeUtc) AS t FROM AccountItems WHERE Account IN ({','.join('?' * len(ids))}) "
            "GROUP BY ItemType, Currency", ids)
        items = {self.enums.name("AccountItem", r["ItemType"]): r["Value"] for r in rows}
        last = max((r["t"] for r in rows), default=None)
        return {"account": account, "last_update": _iso(ticks_to_local(last)), "items": items,
                "note": "Ultimo valor guardado por NT8 en su base de datos (puede no ser el de este instante)."}

    def executions(self, account: str | None = None, instrument: str | None = None, since: str | None = None,
                   until: str | None = None, limit: int = 200, offset: int = 0, newest_first: bool = True) -> dict:
        where, params = [], []
        self._in("Account", self._account_ids(account), where, params)
        self._in("Instrument", self._instrument_ids(instrument), where, params)
        self._time_filter("Time", since, until, where, params)
        cond = ("WHERE " + " AND ".join(where)) if where else ""
        total = self.query(f"SELECT COUNT(*) AS n FROM Executions {cond}", params)[0]["n"]
        limit = max(1, min(5000, int(limit)))
        rows = self.query(
            f"SELECT * FROM Executions {cond} ORDER BY Time {'DESC' if newest_first else 'ASC'} LIMIT ? OFFSET ?",
            [*params, limit, max(0, int(offset))])
        return {"total": total, "offset": offset, "returned": len(rows),
                "executions": [self._exec_dict(r) for r in rows]}

    def _exec_dict(self, r: sqlite3.Row) -> dict:
        ins = self._instrument(r["Instrument"])
        acc = self._ref()["accounts"].get(r["Account"], {})
        return {
            "execution_id": r["ExecutionId"],
            "order_id": r["OrderId"],
            "account": acc.get("Name"),
            "instrument": ins["name"],
            "action": "Buy" if r["MarketPosition"] == 0 else "Sell",
            "quantity": r["Quantity"],
            "price": r["Price"],
            "time": _iso(ticks_to_local(r["Time"])),
            "commission": r["Commission"],
            "fee": r["Fee"],
            "name": r["Name"] or None,
            "is_entry": bool(r["IsEntry"]),
            "is_exit": bool(r["IsExit"]),
            "position_after": r["Position"],
            "mae_price": _clean_price(r["MinPrice"]) if r["MarketPosition"] == 0 else _clean_price(r["MaxPrice"]),
        }

    def orders(self, account: str | None = None, instrument: str | None = None, since: str | None = None,
               until: str | None = None, state: str | None = None, limit: int = 200, offset: int = 0) -> dict:
        where, params = [], []
        self._in("o.Account", self._account_ids(account), where, params)
        self._in("o.Instrument", self._instrument_ids(instrument), where, params)
        self._time_filter("o.Time", since, until, where, params)
        if state:
            code = self.enums.value("OrderState", state)
            if code is None:
                raise DBError(f"estado desconocido: {state}. Validos: "
                              + ", ".join(self.enums.maps.get("OrderState", {}).values()))
            where.append("o.OrderState = ?")
            params.append(code)
        cond = ("WHERE " + " AND ".join(where)) if where else ""
        total = self.query(f"SELECT COUNT(*) AS n FROM Orders o {cond}", params)[0]["n"]
        limit = max(1, min(5000, int(limit)))
        rows = self.query(
            f"SELECT o.*, (SELECT u.Comment FROM OrderUpdates u WHERE u.\"Order\" = o.Id AND u.Comment != '' "
            f"ORDER BY u.Nr DESC LIMIT 1) AS LastComment FROM Orders o {cond} ORDER BY o.Time DESC LIMIT ? OFFSET ?",
            [*params, limit, max(0, int(offset))])
        out = []
        for r in rows:
            ins = self._instrument(r["Instrument"])
            acc = self._ref()["accounts"].get(r["Account"], {})
            out.append({
                "order_id": r["OrderId"],
                "account": acc.get("Name"),
                "instrument": ins["name"],
                "name": r["Name"],
                "action": self.enums.name("OrderAction", r["OrderAction"]),
                "type": self.enums.name("OrderType", r["OrderType"]),
                "state": self.enums.name("OrderState", r["OrderState"]),
                "quantity": r["Quantity"],
                "filled": r["Filled"],
                "limit_price": r["LimitPrice"] or None,
                "stop_price": r["StopPrice"] or None,
                "avg_fill_price": r["AvgFillPrice"] or None,
                "tif": self.enums.name("TimeInForce", r["TimeInForce"]),
                "oco": r["Oco"] or None,
                "time": _iso(ticks_to_local(r["Time"])),
                "comment": r["LastComment"] or None,
            })
        return {"total": total, "offset": offset, "returned": len(out), "orders": out}

    def order_history(self, order_id: str) -> dict:
        rows = self.query("SELECT * FROM Orders WHERE OrderId = ?", (order_id,))
        if not rows:
            raise DBError(f"orden {order_id} no encontrada")
        o = rows[0]
        ups = self.query('SELECT * FROM OrderUpdates WHERE "Order" = ? ORDER BY Nr', (o["Id"],))
        return {
            "order_id": order_id,
            "account": self._ref()["accounts"].get(o["Account"], {}).get("Name"),
            "instrument": self._instrument(o["Instrument"])["name"],
            "action": self.enums.name("OrderAction", o["OrderAction"]),
            "type": self.enums.name("OrderType", o["OrderType"]),
            "updates": [{
                "n": u["Nr"], "time": _iso(ticks_to_local(u["Time"])),
                "state": self.enums.name("OrderState", u["OrderState"]),
                "quantity": u["Quantity"], "filled": u["Filled"],
                "limit_price": u["LimitPrice"] or None, "stop_price": u["StopPrice"] or None,
                "avg_fill_price": u["AvgFillPrice"] or None,
                "error": self.enums.name("ErrorCode", u["Error"]) if u["Error"] else None,
                "comment": u["Comment"] or None,
            } for u in ups],
        }

    def rejections(self, account: str | None = None, since: str | None = None, limit: int = 100) -> list[dict]:
        rejected = self.enums.value("OrderState", "Rejected")
        where, params = ["u.OrderState = ?"], [rejected]
        self._in("o.Account", self._account_ids(account), where, params)
        self._time_filter("u.Time", since, None, where, params)
        rows = self.query(
            f'SELECT u.*, o.OrderId AS Oid, o.Account, o.Instrument, o.OrderAction, o.OrderType, o.Name '
            f'FROM OrderUpdates u JOIN Orders o ON o.Id = u."Order" WHERE {" AND ".join(where)} '
            f'ORDER BY u.Time DESC LIMIT ?', [*params, max(1, min(2000, limit))])
        return [{
            "time": _iso(ticks_to_local(r["Time"])),
            "account": self._ref()["accounts"].get(r["Account"], {}).get("Name"),
            "order_id": r["Oid"], "name": r["Name"],
            "instrument": self._instrument(r["Instrument"])["name"],
            "action": self.enums.name("OrderAction", r["OrderAction"]),
            "type": self.enums.name("OrderType", r["OrderType"]),
            "quantity": r["Quantity"], "limit_price": r["LimitPrice"] or None, "stop_price": r["StopPrice"] or None,
            "error": self.enums.name("ErrorCode", r["Error"]) if r["Error"] else None,
            "reason": r["Comment"] or None,
        } for r in rows]

    def strategies(self, limit: int = 100) -> list[dict]:
        rows = self.query(
            "SELECT s.*, (SELECT a.Name FROM Strategy2Account sa JOIN Accounts a ON a.Id = sa.Account "
            "WHERE sa.Strategy = s.Id LIMIT 1) AS AccountName, "
            "(SELECT si.Instrument FROM Strategy2Instrument si WHERE si.Strategy = s.Id LIMIT 1) AS Inst, "
            "(SELECT COUNT(*) FROM Strategy2Execution se WHERE se.Strategy = s.Id) AS NExec "
            "FROM Strategies s ORDER BY s.Id DESC LIMIT ?", (max(1, min(1000, limit)),))
        return [{
            "id": r["Id"], "name": r["Name"], "class": r["Classname"], "template": r["Template"] or None,
            "account": r["AccountName"], "instrument": self._instrument(r["Inst"])["name"] if r["Inst"] else None,
            "executions": r["NExec"], "terminal": bool(r["IsTerminal"]),
        } for r in rows]

    # -- operaciones reconstruidas ----------------------------------------
    def trades(self, account: str | None = None, instrument: str | None = None, since: str | None = None,
               until: str | None = None, include_commission: bool = True) -> list[Trade]:
        """Operaciones de ida y vuelta (FIFO por cuenta+instrumento) desde las ejecuciones reales."""
        where, params = [], []
        self._in("Account", self._account_ids(account), where, params)
        self._in("Instrument", self._instrument_ids(instrument), where, params)
        # Para no partir operaciones, se lee un colchon de 3 dias antes de 'since'
        s = parse_when(since)
        if s:
            where.append("Time >= ?")
            params.append(local_to_ticks(s - timedelta(days=3)))
        u = until_bound(until)
        if u:
            where.append("Time < ?")
            params.append(local_to_ticks(u))
        cond = ("WHERE " + " AND ".join(where)) if where else ""
        rows = self.query(f"SELECT * FROM Executions {cond} ORDER BY Time ASC, Id ASC", params)

        lots: dict[tuple, list[dict]] = defaultdict(list)
        trades: list[Trade] = []
        for r in rows:
            ins = self._instrument(r["Instrument"])
            key = (r["Account"], r["Instrument"])
            side = "long" if r["MarketPosition"] == 0 else "short"
            qty = int(r["Quantity"] or 0)
            price = float(r["Price"])
            when = ticks_to_local(r["Time"])
            fee = float(r["Commission"] or 0) + float(r["Fee"] or 0)
            fee_per = fee / qty if qty else 0.0
            book = lots[key]
            remaining = qty
            while remaining > 0 and book and book[0]["side"] != side:
                lot = book[0]
                matched = min(lot["qty"], remaining)
                diff = price - lot["price"]
                if lot["side"] == "short":
                    diff = -diff
                gross = diff * matched * float(ins["point_value"])
                cost = (lot["fee_per"] + fee_per) * matched if include_commission else 0.0
                t = Trade(
                    index=0, profit=gross - cost,
                    direction="Long" if lot["side"] == "long" else "Short",
                    quantity=matched, entry_time=lot["time"], exit_time=when,
                    entry_price=lot["price"], exit_price=price, instrument=ins["name"],
                )
                t.account = self._ref()["accounts"].get(r["Account"], {}).get("Name")  # type: ignore[attr-defined]
                t.commission = round(cost, 2)  # type: ignore[attr-defined]
                trades.append(t)
                lot["qty"] -= matched
                remaining -= matched
                if lot["qty"] <= 0:
                    book.pop(0)
            if remaining > 0:
                book.append({"side": side, "qty": remaining, "price": price, "time": when, "fee_per": fee_per})

        if s:
            trades = [t for t in trades if (t.exit_time or t.entry_time) and (t.exit_time or t.entry_time) >= s]
        for i, t in enumerate(trades, start=1):
            t.index = i
        return trades

    # -- instrumentos --------------------------------------------------------
    def instrument_search(self, query: str = "", limit: int = 50) -> list[dict]:
        q = (query or "").lower()
        out = []
        for m in self._ref()["masters"].values():
            if q and q not in (m["Name"] or "").lower() and q not in (m.get("Description") or "").lower():
                continue
            out.append({
                "name": m["Name"], "description": m.get("Description"),
                "type": self.enums.name("InstrumentType", m.get("InstrumentType")),
                "tick_size": m.get("TickSize"), "point_value": m.get("PointValue"),
                "tick_value": round((m.get("TickSize") or 0) * (m.get("PointValue") or 0), 6),
                "currency": self.enums.name("Currency", m.get("Currency")),
                "trading_hours": m.get("TradingHours"),
            })
        out.sort(key=lambda x: (0 if x["name"].lower() == q else 1, len(x["name"]), x["name"]))
        return out[: max(1, min(500, limit))]
