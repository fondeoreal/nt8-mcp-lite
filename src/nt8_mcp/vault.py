"""Vault de backtests: indexado, parseo tolerante de CSV de NT8 y metricas.

El AddOn MCPVault deja aqui los CSV que compartes desde NinjaTrader.
Este modulo los lee, los normaliza (locale ES/EN, ; o ,) y calcula metricas
orientadas a supervivencia en cuenta fondeada.
"""

from __future__ import annotations

import csv
import io
import json
import math
import random
import re
import statistics
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------
#  Normalizacion de cabeceras
# ---------------------------------------------------------------------

COLUMN_ALIASES: dict[str, list[str]] = {
    "trade_number": ["trade number", "numero de operacion", "n de operacion", "trade", "num"],
    "instrument": ["instrument", "instrumento"],
    "account": ["account", "cuenta"],
    "strategy": ["strategy", "estrategia"],
    "market_position": ["market pos", "market position", "posicion de mercado", "posicion"],
    "quantity": ["quantity", "qty", "cantidad"],
    "entry_price": ["entry price", "precio de entrada"],
    "exit_price": ["exit price", "precio de salida"],
    "entry_time": ["entry time", "hora de entrada", "fecha de entrada"],
    "exit_time": ["exit time", "hora de salida", "fecha de salida"],
    "entry_name": ["entry name", "nombre de entrada"],
    "exit_name": ["exit name", "nombre de salida"],
    "profit": ["profit", "net profit", "beneficio", "beneficio neto", "pnl", "p l", "ganancia"],
    "cum_profit": ["cum profit", "cum net profit", "cumulative profit", "beneficio acumulado"],
    "commission": ["commission", "comision", "comisiones"],
    "mae": ["mae", "max adverse excursion", "excursion adversa maxima"],
    "mfe": ["mfe", "max favorable excursion", "excursion favorable maxima"],
    "etd": ["etd", "end trade drawdown"],
    "bars": ["bars", "barras"],
}


def _slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("%", " pct ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _map_columns(headers: list[str]) -> dict[str, int]:
    slugs = [_slug(h) for h in headers]
    mapping: dict[str, int] = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        for idx, slug in enumerate(slugs):
            if idx in mapping.values():
                continue
            if slug in aliases or any(slug.startswith(a) for a in aliases):
                mapping[canonical] = idx
                break
    return mapping


# ---------------------------------------------------------------------
#  Parseo de numeros y fechas tolerante a locale
# ---------------------------------------------------------------------

_NUM_CLEAN = re.compile(r"[^\d,.\-+]")


def parse_number(raw: Any) -> float | None:
    """Convierte '1.234,56 €', '(1,234.56)' o '-1234.56' a float."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)

    s = str(raw).strip()
    if not s:
        return None

    negative = s.startswith("(") and s.endswith(")")
    s = _NUM_CLEAN.sub("", s)
    if not s or s in "-+.,":
        return None

    has_dot, has_comma = "." in s, "," in s
    if has_dot and has_comma:
        # el separador mas a la derecha es el decimal
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif has_comma:
        tail = s.split(",")[-1]
        # "1,234" con 3 decimales exactos y sin otros signos -> probable millar
        if len(tail) == 3 and s.count(",") == 1 and len(s.split(",")[0]) <= 3 and "-" not in s[1:]:
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")

    try:
        value = float(s)
    except ValueError:
        return None
    return -value if negative else value


_DATE_FORMATS = [
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%d.%m.%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S",
]


def parse_datetime(raw: Any) -> datetime | None:
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = re.sub(r"\.\d+$", "", s)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def read_text_any(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("no puedo decodificar el fichero")


def _sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=";,\t|").delimiter
    except csv.Error:
        counts = {d: sample.count(d) for d in ";,\t|"}
        return max(counts, key=counts.get) if max(counts.values()) else ","


# ---------------------------------------------------------------------
#  Modelo
# ---------------------------------------------------------------------

@dataclass
class Trade:
    index: int
    profit: float
    direction: str | None = None
    quantity: float | None = None
    entry_time: datetime | None = None
    exit_time: datetime | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    instrument: str | None = None
    mae: float | None = None
    mfe: float | None = None
    account: str | None = None
    commission: float | None = None
    entry_name: str | None = None
    exit_name: str | None = None

    def as_dict(self) -> dict:
        extra = {k: getattr(self, k) for k in ("account", "commission", "entry_name", "exit_name")
                 if getattr(self, k) is not None}
        return {
            "n": self.index,
            "instrument": self.instrument,
            "direction": self.direction,
            "quantity": self.quantity,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "profit": round(self.profit, 2),
            "mae": self.mae,
            "mfe": self.mfe,
            **extra,
        }


def load_trades(path: Path) -> tuple[list[Trade], dict]:
    """Lee un CSV de backtest de NT8 y devuelve (trades, metadatos del parseo)."""
    text = read_text_any(path)

    delimiter = _sniff_delimiter(text[:4000])
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [r for r in reader if any(str(c).strip() for c in r)]
    if len(rows) < 2:
        raise ValueError("el CSV no tiene filas de datos")

    # La cabecera puede no ser la primera linea (NT8 a veces mete titulos)
    header_idx, mapping = 0, {}
    for i in range(min(6, len(rows))):
        candidate = _map_columns(rows[i])
        if "profit" in candidate or len(candidate) >= 4:
            header_idx, mapping = i, candidate
            break
    if not mapping:
        raise ValueError(
            "no reconozco las columnas. Cabecera detectada: " + ", ".join(rows[0][:12])
        )
    if "profit" not in mapping:
        raise ValueError(
            "falta la columna de beneficio (Profit / Beneficio). Columnas: " + ", ".join(rows[header_idx])
        )

    def cell(row: list[str], key: str) -> Any:
        idx = mapping.get(key)
        if idx is None or idx >= len(row):
            return None
        return row[idx]

    trades: list[Trade] = []
    skipped = 0
    for row in rows[header_idx + 1:]:
        profit = parse_number(cell(row, "profit"))
        if profit is None:
            skipped += 1
            continue
        direction = cell(row, "market_position")
        trades.append(
            Trade(
                index=len(trades) + 1,
                profit=profit,
                direction=str(direction).strip().title() if direction else None,
                quantity=parse_number(cell(row, "quantity")),
                entry_time=parse_datetime(cell(row, "entry_time")),
                exit_time=parse_datetime(cell(row, "exit_time")),
                entry_price=parse_number(cell(row, "entry_price")),
                exit_price=parse_number(cell(row, "exit_price")),
                instrument=(str(cell(row, "instrument")).strip() or None) if cell(row, "instrument") else None,
                mae=parse_number(cell(row, "mae")),
                mfe=parse_number(cell(row, "mfe")),
            )
        )

    meta = {
        "file": path.name,
        "delimiter": delimiter,
        "columns_detected": sorted(mapping.keys()),
        "rows_parsed": len(trades),
        "rows_skipped": skipped,
    }
    return trades, meta


# ---------------------------------------------------------------------
#  Metricas
# ---------------------------------------------------------------------

def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Intervalo de confianza de Wilson para el win rate (mejor que el normal con n pequena)."""
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return (max(0.0, (centre - margin) / denom), min(1.0, (centre + margin) / denom))


def max_drawdown(equity: list[float]) -> tuple[float, int]:
    peak, mdd, mdd_at = equity[0] if equity else 0.0, 0.0, 0
    for i, value in enumerate(equity):
        peak = max(peak, value)
        dd = peak - value
        if dd > mdd:
            mdd, mdd_at = dd, i + 1
    return mdd, mdd_at


def streaks(trades: list[Trade]) -> tuple[int, int]:
    best = worst = cur_w = cur_l = 0
    for t in trades:
        if t.profit > 0:
            cur_w, cur_l = cur_w + 1, 0
        elif t.profit < 0:
            cur_l, cur_w = cur_l + 1, 0
        best, worst = max(best, cur_w), max(worst, cur_l)
    return best, worst


def simulate_trailing_drawdown(
    trades: list[Trade],
    initial_balance: float,
    trailing_amount: float,
    end_of_day: bool = True,
) -> dict:
    """Simula el trailing drawdown de una prop firm sobre la curva de la estrategia.

    end_of_day=True: el umbral solo sube al cierre de sesion (modelo LucidFlex).
    end_of_day=False: el umbral sigue el maximo intradia (modelo mas estricto).
    """
    balance = initial_balance
    threshold = initial_balance - trailing_amount
    peak = initial_balance
    day_peak = initial_balance
    current_day = None
    breach_at = None

    for t in trades:
        day = (t.exit_time or t.entry_time)
        day = day.date() if day else None

        if end_of_day and current_day is not None and day != current_day:
            peak = max(peak, day_peak)
            threshold = max(threshold, peak - trailing_amount)
            threshold = min(threshold, initial_balance)  # muchas firmas congelan en el inicial
        current_day = day

        balance += t.profit
        day_peak = max(day_peak, balance)

        if not end_of_day:
            peak = max(peak, balance)
            threshold = min(initial_balance, max(threshold, peak - trailing_amount))

        if balance <= threshold and breach_at is None:
            breach_at = t.index

    return {
        "initial_balance": initial_balance,
        "trailing_amount": trailing_amount,
        "model": "end_of_day" if end_of_day else "intraday",
        "final_balance": round(balance, 2),
        "final_threshold": round(threshold, 2),
        "cushion": round(balance - threshold, 2),
        "breached": breach_at is not None,
        "breached_at_trade": breach_at,
        "verdict": (
            f"La cuenta habria reventado en la operacion {breach_at}."
            if breach_at
            else "La cuenta habria sobrevivido a toda la serie."
        ),
    }


def _percentile(sorted_values: list[float], q: float) -> float:
    """Percentil por interpolacion lineal (q en 0..1). Espera una lista ya ordenada."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_values[int(pos)]
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def _pathwise_trailing(seq: list[float], initial: float, trailing: float) -> tuple[float, int | None]:
    """Recorre una secuencia de beneficios devolviendo (max_drawdown, operacion_de_breach).

    Modelo de trailing por operacion: el umbral sube con el pico y se congela en el
    balance inicial (regla tipo Apex). Es el modelo estricto usado en el Monte Carlo,
    donde el orden real de las operaciones se pierde al remuestrear.
    """
    balance = initial
    equity_peak = initial
    peak = initial
    threshold = initial - trailing
    mdd = 0.0
    breach_at: int | None = None
    for i, profit in enumerate(seq, start=1):
        balance += profit
        equity_peak = max(equity_peak, balance)
        mdd = max(mdd, equity_peak - balance)
        peak = max(peak, balance)
        threshold = min(initial, max(threshold, peak - trailing))
        if balance <= threshold and breach_at is None:
            breach_at = i
    return mdd, breach_at


def monte_carlo(
    trades: list[Trade],
    initial_balance: float,
    trailing_amount: float,
    iterations: int = 2000,
    method: str = "bootstrap",
    seed: int | None = None,
) -> dict:
    """Monte Carlo por remuestreo de las operaciones del backtest.

    Devuelve la distribucion de beneficio neto y de max drawdown, y la probabilidad
    de reventar el trailing drawdown de la prop firm sobre miles de ordenaciones.
    method='bootstrap' remuestrea con reemplazo (riesgo de muestreo + secuencia);
    method='shuffle' solo baraja la serie real (riesgo de secuencia puro).
    """
    profits = [t.profit for t in trades]
    n = len(profits)
    if n < 5:
        raise ValueError("hacen falta al menos 5 operaciones para un Monte Carlo util")
    iterations = max(100, min(50_000, iterations))
    rng = random.Random(seed)

    nets: list[float] = []
    dds: list[float] = []
    breaches = 0
    breach_points: list[int] = []

    for _ in range(iterations):
        if method == "shuffle":
            seq = profits[:]
            rng.shuffle(seq)
        else:
            seq = [profits[rng.randrange(n)] for _ in range(n)]
        mdd, breach_at = _pathwise_trailing(seq, initial_balance, trailing_amount)
        nets.append(sum(seq))
        dds.append(mdd)
        if breach_at is not None:
            breaches += 1
            breach_points.append(breach_at)

    nets_sorted = sorted(nets)
    dds_sorted = sorted(dds)
    breach_points.sort()

    def dist(values_sorted: list[float]) -> dict:
        return {
            "mean": round(statistics.mean(values_sorted), 2),
            "p05": round(_percentile(values_sorted, 0.05), 2),
            "p25": round(_percentile(values_sorted, 0.25), 2),
            "median": round(_percentile(values_sorted, 0.50), 2),
            "p75": round(_percentile(values_sorted, 0.75), 2),
            "p95": round(_percentile(values_sorted, 0.95), 2),
            "worst": round(values_sorted[0], 2),
            "best": round(values_sorted[-1], 2),
        }

    return {
        "iterations": iterations,
        "method": method,
        "trades_per_run": n,
        "initial_balance": initial_balance,
        "trailing_amount": trailing_amount,
        "net_profit": dist(nets_sorted),
        "max_drawdown": dist(dds_sorted),
        "prop_trailing_breach": {
            "probability": round(breaches / iterations, 4),
            "runs_breached": breaches,
            "median_breach_trade": (breach_points[len(breach_points) // 2] if breach_points else None),
            "earliest_breach_trade": (breach_points[0] if breach_points else None),
        },
        "verdict": (
            f"En {round(breaches / iterations * 100, 1)}% de las {iterations} simulaciones la cuenta "
            f"habria reventado el trailing de {trailing_amount:g}."
            if breaches
            else f"Ninguna de las {iterations} simulaciones revento el trailing de {trailing_amount:g}."
        ),
        "note": "El breach usa un modelo de trailing por operacion (estricto). "
                "Bootstrap tambien captura riesgo de muestreo; shuffle solo de secuencia.",
    }


def equity_curve(trades: list[Trade], points: int = 200) -> dict:
    """Curva de equity acumulada y curva underwater (distancia al maximo), submuestreadas."""
    profits = [t.profit for t in trades]
    if not profits:
        raise ValueError("sin operaciones")

    equity: list[float] = []
    running = 0.0
    for p in profits:
        running += p
        equity.append(running)

    underwater: list[float] = []
    running_peak = equity[0]
    max_dd = 0.0
    for value in equity:
        running_peak = max(running_peak, value)
        drop = value - running_peak
        underwater.append(drop)
        max_dd = max(max_dd, -drop)

    total = len(equity)
    points = max(2, min(2000, points))
    if total > points:
        step = total / points
        idxs = sorted({int(i * step) for i in range(points)} | {total - 1})
    else:
        idxs = list(range(total))

    return {
        "trades": total,
        "final_equity": round(equity[-1], 2),
        "max_drawdown": round(max_dd, 2),
        "returned_points": len(idxs),
        "curve": [
            {"n": i + 1, "equity": round(equity[i], 2), "underwater": round(underwater[i], 2)}
            for i in idxs
        ],
    }


def write_strategy_template(
    source_path: Path,
    dest_dir: Path,
    new_name: str,
    overrides: dict[str, object],
) -> dict:
    """Clona una plantilla XML de estrategia sobrescribiendo parametros por nombre de tag.

    No inventa el esquema: parte de una plantilla existente (que ya casa con tu version
    de NT8) y solo cambia los valores de los tags indicados. El resultado se guarda en
    templates\\Strategy, listo para cargarlo en el Strategy Analyzer.
    """
    import xml.etree.ElementTree as ET

    source_path = Path(source_path)
    if not source_path.exists():
        raise FileNotFoundError(f"no existe la plantilla base: {source_path}")

    try:
        tree = ET.parse(source_path)
    except ET.ParseError as exc:
        raise ValueError(f"XML invalido: {exc}") from None
    root = tree.getroot()

    wanted = {str(k).lower(): (str(k), v) for k, v in overrides.items()}
    applied: dict[str, dict] = {}
    for element in root.iter():
        tag = element.tag.split("}")[-1]
        key = tag.lower()
        if key in wanted and len(element) == 0:
            applied[tag] = {"from": element.text, "to": str(wanted[key][1])}
            element.text = str(wanted[key][1])

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (new_name if new_name.lower().endswith(".xml") else new_name + ".xml")
    tree.write(dest, encoding="utf-8", xml_declaration=True)

    applied_lower = {t.lower() for t in applied}
    not_found = [str(k) for k in overrides if str(k).lower() not in applied_lower]
    return {
        "written": str(dest),
        "name": dest.name,
        "applied": applied,
        "not_found": not_found,
        "next_step": "Carga esta plantilla en el Strategy Analyzer (o pasa su nombre a sa_open) y ejecuta el backtest.",
    }


def trades_from_executions(executions: list[dict]) -> list[Trade]:
    """Reconstruye operaciones de ida y vuelta (FIFO) desde las ejecuciones de un backtest.

    Cada ejecucion trae {instrument, action (Long/Short), quantity, price, point_value, time}.
    Empareja compras y ventas en orden cronologico y calcula el profit con el point value.
    """
    def _ts(e: dict) -> datetime:
        return parse_datetime(e.get("time")) or datetime.min

    ordered = sorted(executions, key=_ts)
    open_lots: dict[str, list[dict]] = {}
    trades: list[Trade] = []
    idx = 0

    for e in ordered:
        instrument = e.get("instrument") or ""
        pos = str(e.get("action") or "").strip().lower()      # 'long' (compra) / 'short' (venta)
        qty = int(parse_number(e.get("quantity")) or 0)
        price = parse_number(e.get("price")) or 0.0
        point_value = parse_number(e.get("point_value")) or 1.0
        stamp = parse_datetime(e.get("time"))
        if qty <= 0 or pos not in ("long", "short"):
            continue

        lots = open_lots.setdefault(instrument, [])
        remaining = qty
        while remaining > 0 and lots and lots[0]["pos"] != pos:
            lot = lots[0]
            matched = min(lot["qty"], remaining)
            diff = price - lot["price"]
            if lot["pos"] == "short":         # entrada en corto: gana si el precio baja
                diff = -diff
            idx += 1
            trades.append(
                Trade(
                    index=idx,
                    profit=diff * matched * point_value,
                    direction="Long" if lot["pos"] == "long" else "Short",
                    quantity=matched,
                    entry_time=lot["time"],
                    exit_time=stamp,
                    entry_price=lot["price"],
                    exit_price=price,
                    instrument=instrument,
                )
            )
            lot["qty"] -= matched
            remaining -= matched
            if lot["qty"] <= 0:
                lots.pop(0)

        if remaining > 0:
            lots.append({"pos": pos, "qty": remaining, "price": price, "time": stamp})

    return trades


def analyze(trades: list[Trade], gate_n: int = 30, gate_target: float = 0.52) -> dict:
    if not trades:
        return {"error": "sin operaciones"}

    profits = [t.profit for t in trades]
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p < 0]
    scratches = len(profits) - len(wins) - len(losses)

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    net = sum(profits)
    n = len(profits)
    decided = len(wins) + len(losses)
    win_rate = len(wins) / decided if decided else 0.0
    lo, hi = wilson_interval(len(wins), decided)

    avg_win = statistics.mean(wins) if wins else 0.0
    avg_loss = abs(statistics.mean(losses)) if losses else 0.0
    payoff = (avg_win / avg_loss) if avg_loss else None

    equity, running = [], 0.0
    for p in profits:
        running += p
        equity.append(running)
    mdd, mdd_at = max_drawdown(equity)
    best_streak, worst_streak = streaks(trades)

    # R-multiplos usando la perdida media como unidad de riesgo
    r_multiples = [p / avg_loss for p in profits] if avg_loss else []

    # Corte Gate 0: win rate en las primeras N operaciones
    head = trades[:gate_n]
    head_decided = [t for t in head if t.profit != 0]
    head_wins = len([t for t in head_decided if t.profit > 0])
    head_wr = head_wins / len(head_decided) if head_decided else 0.0
    head_lo, head_hi = wilson_interval(head_wins, len(head_decided))

    by_hour: dict[int, list[float]] = {}
    by_weekday: dict[int, list[float]] = {}
    for t in trades:
        stamp = t.entry_time or t.exit_time
        if not stamp:
            continue
        by_hour.setdefault(stamp.hour, []).append(t.profit)
        by_weekday.setdefault(stamp.weekday(), []).append(t.profit)

    days_es = ["Lun", "Mar", "Mie", "Jue", "Vie", "Sab", "Dom"]

    def bucket(values: list[float]) -> dict:
        w = len([v for v in values if v > 0])
        return {
            "trades": len(values),
            "net": round(sum(values), 2),
            "win_rate": round(w / len(values), 4) if values else 0.0,
        }

    stamps = [t.entry_time for t in trades if t.entry_time]

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "scratches": scratches,
        "win_rate": round(win_rate, 4),
        "win_rate_ci95": [round(lo, 4), round(hi, 4)],
        "net_profit": round(net, 2),
        "gross_profit": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "expectancy_per_trade": round(net / n, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "payoff_ratio": round(payoff, 3) if payoff else None,
        "largest_win": round(max(profits), 2),
        "largest_loss": round(min(profits), 2),
        "max_drawdown": round(mdd, 2),
        "max_drawdown_at_trade": mdd_at,
        "return_over_max_dd": round(net / mdd, 3) if mdd else None,
        "max_consecutive_wins": best_streak,
        "max_consecutive_losses": worst_streak,
        "avg_r_multiple": round(statistics.mean(r_multiples), 3) if r_multiples else None,
        "r_multiple_stdev": round(statistics.pstdev(r_multiples), 3) if len(r_multiples) > 1 else None,
        "gate_check": {
            "window": len(head_decided),
            "target_win_rate": gate_target,
            "win_rate": round(head_wr, 4),
            "ci95": [round(head_lo, 4), round(head_hi, 4)],
            "passes": head_wr >= gate_target,
            "note": (
                "El limite inferior del intervalo queda por debajo del objetivo: "
                "la muestra no descarta que el win rate real sea insuficiente."
                if head_lo < gate_target
                else "El intervalo completo supera el objetivo."
            ),
        },
        "by_hour": {str(h): bucket(v) for h, v in sorted(by_hour.items())},
        "by_weekday": {days_es[d]: bucket(v) for d, v in sorted(by_weekday.items())},
        "period": {
            "first": min(stamps).isoformat() if stamps else None,
            "last": max(stamps).isoformat() if stamps else None,
        },
    }


# ---------------------------------------------------------------------
#  Gestion del vault
# ---------------------------------------------------------------------

class Vault:
    def __init__(self, root: Path, extra_dirs: list[Path] | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # Carpetas adicionales de solo lectura con CSV de backtest (p. ej. QuantForge)
        self.extra_dirs = [Path(d) for d in (extra_dirs or []) if Path(d).exists()]

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def read_index(self) -> dict:
        if not self.index_path.exists():
            return {}
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    VISIBLE_SUFFIXES = (".csv", ".xml", ".txt", ".tsv")

    def _files(self) -> list[Path]:
        files = []
        for folder in [self.root, *self.extra_dirs]:
            try:
                files += [p for p in folder.iterdir()
                          if p.is_file() and p.suffix.lower() in self.VISIBLE_SUFFIXES and p.name != "index.json"]
            except OSError:
                continue
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)

    def list_files(self) -> list[dict]:
        index = self.read_index()
        items = []
        for path in self._files():
            entry = index.get(path.name, {})
            items.append(
                {
                    "name": path.name,
                    "size_kb": round(path.stat().st_size / 1024, 1),
                    "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
                    "note": entry.get("note"),
                    "source": entry.get("source") or (path.parent.name if path.parent != self.root else None),
                    "kind": _guess_kind(path),
                }
            )
        return items

    def resolve(self, name: str) -> Path:
        """Resuelve un nombre parcial; 'ultimo'/'last' devuelve el mas reciente."""
        files = self._files()
        if not files:
            raise FileNotFoundError(
                f"El vault esta vacio ({self.root}). Comparte un CSV desde la ventana MCP Vault de NT8."
            )
        if not name or name.lower() in ("ultimo", "last", "latest", "reciente"):
            return files[0]

        target = _slug(name)
        exact = [f for f in files if f.name.lower() == name.lower()]
        if exact:
            return exact[0]
        partial = [f for f in files if target in _slug(f.name)]
        if partial:
            return partial[0]
        raise FileNotFoundError(
            f"No encuentro '{name}' en el vault. Disponibles: " + ", ".join(f.name for f in files[:10])
        )


# ---------------------------------------------------------------------
#  Lectura generica de tablas (resumen, ordenes, ejecuciones, optimizacion)
# ---------------------------------------------------------------------

def read_table(path: Path, max_rows: int = 40) -> dict:
    """Lee cualquier CSV delimitado sin exigir esquema de operaciones."""
    text = read_text_any(path)
    delimiter = _sniff_delimiter(text[:4000])
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter)
            if any(str(c).strip() for c in r)]
    if not rows:
        raise ValueError("fichero vacio")

    header = rows[0]
    body = rows[1:]

    # Formato clave;valor tipico del panel de resumen de NT8
    if len(header) == 2 and len(body) > 3:
        pairs = {}
        for row in rows:
            if len(row) >= 2 and str(row[0]).strip():
                pairs[str(row[0]).strip()] = str(row[1]).strip()
        return {"kind": "key_value", "delimiter": delimiter, "fields": len(pairs), "data": pairs}

    return {
        "kind": "table",
        "delimiter": delimiter,
        "columns": header,
        "total_rows": len(body),
        "returned_rows": min(len(body), max_rows),
        "rows": [dict(zip(header, r)) for r in body[:max_rows]],
    }


# Metricas que NT8 emite en las rejillas de optimizacion (ES / EN)
_METRIC_SLUGS = {
    "net profit", "beneficio neto", "profit factor", "factor de beneficio",
    "total net profit", "max drawdown", "maximo drawdown", "max drawdown pct",
    "trades", "operaciones", "num trades", "percent profitable", "pct profitable",
    "porcentaje rentable", "win pct", "sharpe ratio", "sortino ratio",
    "avg trade", "operacion media", "cum profit", "beneficio acumulado",
    "gross profit", "gross loss", "beneficio bruto", "perdida bruta",
    "max consec losers", "max consec winners", "r squared", "ulcer index",
}


def read_optimization(path: Path, top: int = 15, sort_by: str | None = None) -> dict:
    """Interpreta una rejilla de optimizacion: separa columnas de parametros de las de metricas."""
    table = read_table(path, max_rows=10_000)
    if table["kind"] != "table":
        raise ValueError("esto no parece una rejilla de optimizacion")

    columns = table["columns"]
    metric_cols, param_cols = [], []
    for col in columns:
        slug = _slug(col)
        if any(slug.startswith(m) or m in slug for m in _METRIC_SLUGS):
            metric_cols.append(col)
        else:
            param_cols.append(col)

    if not metric_cols:
        raise ValueError(
            "no encuentro columnas de metricas. Columnas detectadas: " + ", ".join(columns[:15])
        )

    rows = []
    for raw in table["rows"]:
        entry = {"params": {}, "metrics": {}}
        for col in param_cols:
            value = parse_number(raw.get(col))
            entry["params"][col] = value if value is not None else raw.get(col)
        for col in metric_cols:
            value = parse_number(raw.get(col))
            entry["metrics"][col] = value if value is not None else raw.get(col)
        rows.append(entry)

    key = sort_by or next(
        (c for c in metric_cols if "net" in _slug(c) or "neto" in _slug(c)), metric_cols[0]
    )
    rows.sort(key=lambda r: (r["metrics"].get(key) if isinstance(r["metrics"].get(key), (int, float)) else float("-inf")),
              reverse=True)

    # Robustez: cuanto se degrada la metrica al movernos por cada parametro
    stability = {}
    for col in param_cols:
        buckets: dict = {}
        for r in rows:
            v = r["params"].get(col)
            m = r["metrics"].get(key)
            if isinstance(m, (int, float)):
                buckets.setdefault(str(v), []).append(m)
        if len(buckets) > 1:
            means = {k: statistics.mean(v) for k, v in buckets.items()}
            best_val = max(means, key=means.get)
            stability[col] = {
                "values_tested": len(means),
                "best_value": best_val,
                "best_mean": round(means[best_val], 2),
                "worst_mean": round(min(means.values()), 2),
                "spread": round(max(means.values()) - min(means.values()), 2),
                "positive_share": round(
                    len([m for m in means.values() if m > 0]) / len(means), 3
                ),
            }

    return {
        "combinations": len(rows),
        "parameter_columns": param_cols,
        "metric_columns": metric_cols,
        "sorted_by": key,
        "top": rows[:top],
        "parameter_stability": stability,
        "caution": (
            "El mejor resultado de una optimizacion es el mas sobreajustado por construccion. "
            "Mira parameter_stability: un parametro cuyo entorno se desploma no es una senal, es ruido."
        ),
    }


def read_strategy_template(path: Path) -> dict:
    """Extrae los parametros de una plantilla XML de estrategia de NT8."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(read_text_any(path))
    except ET.ParseError as exc:
        raise ValueError(f"XML invalido: {exc}") from None

    skip = {"Name", "Template", "IsVisible", "Id"}
    params: dict[str, str] = {}
    for element in root.iter():
        if len(element) == 0 and element.text and element.text.strip():
            tag = element.tag.split("}")[-1]
            if tag in skip or tag in params:
                continue
            params[tag] = element.text.strip()

    return {
        "file": path.name,
        "strategy": params.get("StrategyName") or root.tag.split("}")[-1],
        "parameters": params,
        "count": len(params),
    }


def _guess_kind(path: Path) -> str:
    """Etiqueta orientativa segun el nombre y la cabecera del fichero."""
    if path.suffix.lower() == ".xml":
        return "plantilla"
    name = _slug(path.stem)
    if "optimi" in name:
        return "optimizacion"
    try:
        head = _slug(read_text_any(path)[:600])
    except (ValueError, OSError):
        return "desconocido"
    if "profit" in head and ("entry" in head or "entrada" in head):
        return "operaciones"
    if "optimi" in head:
        return "optimizacion"
    if "order" in head or "orden" in head:
        return "ordenes"
    if "execution" in head or "ejecucion" in head:
        return "ejecuciones"
    return "tabla"
