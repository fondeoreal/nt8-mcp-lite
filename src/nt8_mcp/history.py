"""Historico de precios: inventario en disco, descarga desde NT8, cache CSV y estadisticas.

- Inventario: lee los nombres de carpeta/fichero de db\\tick, db\\minute y db\\day (no el
  contenido .ncd, que es binario propietario) para decir que datos hay y de que fechas.
- Descarga: via el puente (BarsRequest de NT8). Rangos grandes se exportan a CSV dentro de
  NT8 (bars_export) y se leen del disco, sin pasar megas por el socket.
- Todo CSV exportado queda en NinjaTrader 8\\mcp_data y se puede reutilizar sin NT8 abierto.
"""

from __future__ import annotations

import csv
import math
import re
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .client import NT8Client, NT8Error

_FILE_DATE = re.compile(r"^(\d{8})(\d{4})?\.(\w+)\.ncd$", re.IGNORECASE)
_YEAR_FILE = re.compile(r"^(\d{4})\.(\w+)\.ncd$", re.IGNORECASE)


# ---------------------------------------------------------------------------
#  Inventario de datos en disco
# ---------------------------------------------------------------------------

def data_inventory(nt8_root: Path, instrument: str | None = None) -> dict:
    base = nt8_root / "db"
    result: dict[str, Any] = {"db_folder": str(base), "resolutions": {}}
    root_filter = instrument.strip().split(" ")[0].upper() if instrument else None
    for resolution in ("tick", "minute", "day"):
        folder = base / resolution
        if not folder.exists():
            continue
        entries = []
        for sub in sorted(folder.iterdir()):
            if not sub.is_dir():
                continue
            if root_filter and sub.name.split(" ")[0].upper() != root_filter:
                continue
            if instrument and " " in instrument.strip() and sub.name.upper() != instrument.strip().upper():
                continue
            dates: list[str] = []
            kinds: set[str] = set()
            size = 0
            try:
                for f in sub.iterdir():
                    m = _FILE_DATE.match(f.name)
                    if m:
                        dates.append(m.group(1))
                        kinds.add(m.group(3))
                    else:
                        y = _YEAR_FILE.match(f.name)
                        if y:
                            dates.append(y.group(1) + "0101")
                            kinds.add(y.group(2))
                    try:
                        size += f.stat().st_size
                    except OSError:
                        pass
            except OSError:
                continue
            if not dates:
                continue
            dates.sort()
            entries.append({
                "contract": sub.name,
                "from": f"{dates[0][:4]}-{dates[0][4:6]}-{dates[0][6:]}",
                "to": f"{dates[-1][:4]}-{dates[-1][4:6]}-{dates[-1][6:]}",
                "days": len(set(dates)),
                "types": sorted(kinds),
                "size_mb": round(size / 1e6, 1),
            })
        if entries:
            result["resolutions"][resolution] = entries
    result["note"] = ("NT8 construye cualquier periodo (segundos, rango, renko, volumen...) a partir de tick/minute. "
                      "Usa nt8_bars con from/to dentro de estas fechas.")
    return result


# ---------------------------------------------------------------------------
#  Descarga de barras
# ---------------------------------------------------------------------------

def _bars_args(instrument: str, period_type: str, period_value: int, from_date: str | None, to_date: str | None,
               count: int | None, trading_hours: str | None, market_data: str | None,
               base_period_type: str | None, base_period_value: int | None, value2: int | None) -> dict:
    return {
        "instrument": instrument, "period_type": period_type, "period_value": period_value,
        "from": from_date, "to": to_date, "count": count, "trading_hours": trading_hours,
        "market_data": market_data, "base_period_type": base_period_type,
        "base_period_value": base_period_value, "value2": value2,
    }


def fetch_bars(client: NT8Client, instrument: str, period_type: str = "Minute", period_value: int = 1,
               from_date: str | None = None, to_date: str | None = None, count: int | None = 500,
               trading_hours: str | None = None, market_data: str | None = None,
               base_period_type: str | None = None, base_period_value: int | None = None,
               value2: int | None = None, offset: int = 0, limit: int = 5000, timeout: float = 120) -> dict:
    """Barras en linea (hasta 20000 por llamada). Con un puente v2 solo admite 'count'."""
    if client.supports_v3():
        args = _bars_args(instrument, period_type, period_value, from_date, to_date, count, trading_hours,
                          market_data, base_period_type, base_period_value, value2)
        args.update({"offset": offset, "limit": limit, "timeout_seconds": int(timeout)})
        return client.call("bars", timeout=timeout + 10, **args)
    if from_date:
        raise NT8Error("El AddOn MCPBridge instalado es antiguo (v2) y no admite rangos de fechas. "
                       "Usa 'count', o actualiza el AddOn a v3 (ninjatrader/MCPBridge.cs + F5).")
    if period_type.lower() not in ("tick", "second", "minute", "day", "range"):
        raise NT8Error("El AddOn v2 solo admite Tick, Second, Minute, Day y Range. Actualiza a v3.")
    data = client.call("bars", timeout=timeout, instrument=instrument, period_type=period_type,
                       period_value=period_value, count=min(int(count or 500), 5000))
    data.setdefault("total_available", data.get("count"))
    data.setdefault("has_more", False)
    return data


def export_bars(client: NT8Client, data_root: Path, instrument: str, period_type: str, period_value: int,
                from_date: str | None, to_date: str | None, count: int | None = None,
                trading_hours: str | None = None, market_data: str | None = None,
                base_period_type: str | None = None, base_period_value: int | None = None,
                value2: int | None = None, file: str | None = None, wait_seconds: float = 25) -> dict:
    """Exporta a CSV dentro de NT8 (job asincrono). Espera hasta wait_seconds; si no acaba, devuelve el job."""
    if not client.supports_v3():
        # Compatibilidad: con v2 se hace en linea (maximo 5000) y se guarda aqui
        data = fetch_bars(client, instrument, period_type, period_value, None, None, count or 5000)
        data_root.mkdir(parents=True, exist_ok=True)
        name = file or f"{instrument.replace(' ', '_')}_{period_value}{period_type}_last{len(data['bars'])}.csv"
        path = data_root / Path(name).name
        write_bars_csv(path, data["bars"])
        return {"status": "done", "file": str(path), "rows": len(data["bars"]), "via": "inline (AddOn v2)"}

    args = _bars_args(instrument, period_type, period_value, from_date, to_date, count, trading_hours,
                      market_data, base_period_type, base_period_value, value2)
    args["file"] = file
    job = client.call("bars_export", **args)
    job_id = job["job_id"]
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while time.monotonic() < deadline:
        time.sleep(1.0)
        st = client.call("job_status", job_id=job_id)
        if st.get("status") != "running":
            return st
    return {**job, "status": "running",
            "hint": f"Sigue exportando. Consulta nt8_job_status('{job_id}') y luego lee el CSV con nt8_bars_file."}


def write_bars_csv(path: Path, bars: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow([str(b["time"]).replace("T", " "), b["open"], b["high"], b["low"], b["close"], b["volume"]])


def read_bars_csv(path: Path, start: str | None = None, end: str | None = None,
                  time_column: str | None = None) -> list[dict]:
    """Lee un CSV de barras (el formato de mcp_data o cualquiera con time/open/high/low/close[/volume])."""
    from .vault import parse_datetime, parse_number, read_text_any, _sniff_delimiter  # reutiliza el parser tolerante
    import io

    text = read_text_any(path)
    delim = _sniff_delimiter(text[:2000])
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    if not rows:
        raise ValueError("CSV vacio")
    header = [h.strip().lower() for h in rows[0]]

    def col(*names: str) -> int | None:
        for n in names:
            if n in header:
                return header.index(n)
        return None

    ti = col(time_column.strip().lower()) if time_column else col(
        "time", "datetime", "timestamp", "date", "fecha", "local", "et", "utc")
    oi, hi, li, ci = col("open", "o"), col("high", "h"), col("low", "l"), col("close", "c", "last")
    vi = col("volume", "vol", "v")
    if time_column and ti is None:
        raise ValueError(f"no existe la columna de tiempo '{time_column}'. Cabecera: " + ", ".join(rows[0]))
    if None in (ti, oi, hi, li, ci):
        raise ValueError("el CSV necesita columnas time, open, high, low, close. Cabecera: " + ", ".join(rows[0]))
    t0 = datetime.fromisoformat(start) if start else None
    t1 = datetime.fromisoformat(end) if end else None

    def fast_time(raw: str) -> datetime | None:
        try:
            return datetime.fromisoformat(raw.strip())
        except ValueError:
            return parse_datetime(raw)

    def fast_num(raw: str) -> float | None:
        try:
            return float(raw)
        except ValueError:
            return parse_number(raw)

    out = []
    need = max(ti, oi, hi, li, ci)
    for r in rows[1:]:
        if len(r) <= need:
            continue
        t = fast_time(r[ti])
        if t is None:
            continue
        if (t0 and t < t0) or (t1 and t > t1):
            continue
        o, h, l, c = fast_num(r[oi]), fast_num(r[hi]), fast_num(r[li]), fast_num(r[ci])
        if o is None or h is None or l is None or c is None:
            continue
        v = fast_num(r[vi]) if vi is not None and vi < len(r) else 0.0
        out.append({"time": t, "open": o, "high": h, "low": l, "close": c, "volume": v or 0.0})
    return out


_BARS_CACHE: dict[tuple, list[dict]] = {}


def load_bars_cached(path: Path, time_column: str | None = None) -> list[dict]:
    """read_bars_csv con cache en memoria (clave: ruta + mtime + tamano). Maximo 4 ficheros."""
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size, time_column)
    if key not in _BARS_CACHE:
        if len(_BARS_CACHE) >= 4:
            _BARS_CACHE.pop(next(iter(_BARS_CACHE)))
        _BARS_CACHE[key] = read_bars_csv(path, time_column=time_column)
    return _BARS_CACHE[key]


def normalize_bars(bars: list[dict]) -> list[dict]:
    """Convierte las filas del puente (time ISO) a dict con datetime."""
    out = []
    for b in bars:
        t = b["time"]
        if isinstance(t, str):
            t = datetime.fromisoformat(t.replace("Z", "")[:26])
        out.append({"time": t, "open": float(b["open"]), "high": float(b["high"]), "low": float(b["low"]),
                    "close": float(b["close"]), "volume": float(b.get("volume") or 0)})
    return out


def list_data_files(data_root: Path) -> list[dict]:
    if not data_root.exists():
        return []
    items = []
    for p in sorted(data_root.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True):
        st = p.stat()
        items.append({"file": p.name, "size_mb": round(st.st_size / 1e6, 2),
                      "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")})
    return items


def resolve_data_file(data_root: Path, name: str) -> Path:
    candidate = Path(name)
    if candidate.is_absolute():
        # rutas absolutas: solo ficheros de datos (no cualquier fichero del PC)
        if candidate.suffix.lower() not in (".csv", ".txt"):
            raise ValueError(f"solo se pueden leer ficheros .csv o .txt: {name}")
        if candidate.exists():
            return candidate
    p = data_root / Path(name).name
    if p.exists():
        return p
    matches = [f for f in data_root.glob("*.csv") if name.lower() in f.name.lower()] if data_root.exists() else []
    if matches:
        return sorted(matches, key=lambda f: f.stat().st_mtime, reverse=True)[0]
    raise FileNotFoundError(f"no encuentro '{name}' en {data_root}. Usa nt8_data_files para listarlos.")


# ---------------------------------------------------------------------------
#  Estadisticas de barras
# ---------------------------------------------------------------------------

def bar_stats(bars: list[dict], tick_size: float | None = None) -> dict:
    if not bars:
        raise ValueError("sin barras")
    closes = [b["close"] for b in bars]
    ranges = [b["high"] - b["low"] for b in bars]
    rets = [(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes)) if closes[i - 1]]
    trs = []
    for i, b in enumerate(bars):
        pc = bars[i - 1]["close"] if i else b["open"]
        trs.append(max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc)))
    atr14 = statistics.mean(trs[-14:]) if trs else 0.0

    by_hour: dict[int, list[float]] = {}
    vol_hour: dict[int, list[float]] = {}
    for b, r in zip(bars, ranges):
        by_hour.setdefault(b["time"].hour, []).append(r)
        vol_hour.setdefault(b["time"].hour, []).append(b["volume"])

    by_day: dict[str, dict] = {}
    for b in bars:
        d = b["time"].date().isoformat()
        e = by_day.setdefault(d, {"open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"], "volume": 0.0})
        e["high"] = max(e["high"], b["high"])
        e["low"] = min(e["low"], b["low"])
        e["close"] = b["close"]
        e["volume"] += b["volume"]

    def r2(x: float) -> float:
        return round(x, 4)

    out = {
        "bars": len(bars),
        "from": bars[0]["time"].isoformat(),
        "to": bars[-1]["time"].isoformat(),
        "first_open": bars[0]["open"],
        "last_close": closes[-1],
        "change": r2(closes[-1] - bars[0]["open"]),
        "change_pct": r2((closes[-1] / bars[0]["open"] - 1) * 100) if bars[0]["open"] else None,
        "highest": max(b["high"] for b in bars),
        "lowest": min(b["low"] for b in bars),
        "avg_bar_range": r2(statistics.mean(ranges)),
        "atr14_last": r2(atr14),
        "return_stdev_per_bar_pct": r2(statistics.pstdev(rets) * 100) if len(rets) > 1 else None,
        "total_volume": sum(b["volume"] for b in bars),
        "avg_range_by_hour": {str(h): r2(statistics.mean(v)) for h, v in sorted(by_hour.items())},
        "avg_volume_by_hour": {str(h): round(statistics.mean(v), 1) for h, v in sorted(vol_hour.items())},
        "daily": [{"date": d, **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in e.items()},
                   "range": round(e["high"] - e["low"], 4)} for d, e in list(by_day.items())[-60:]],
    }
    if tick_size:
        out["avg_bar_range_ticks"] = round(out["avg_bar_range"] / tick_size, 2)
        out["atr14_ticks"] = round(atr14 / tick_size, 2)
    return out


def downsample(bars: list[dict], max_points: int) -> list[dict]:
    if len(bars) <= max_points:
        return bars
    step = math.ceil(len(bars) / max_points)
    out = []
    for i in range(0, len(bars), step):
        chunk = bars[i:i + step]
        out.append({"time": chunk[0]["time"], "open": chunk[0]["open"], "high": max(b["high"] for b in chunk),
                    "low": min(b["low"] for b in chunk), "close": chunk[-1]["close"],
                    "volume": sum(b["volume"] for b in chunk)})
    return out
