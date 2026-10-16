"""Diagnostico rapido (solo lectura): puente NT8, base de datos de historial y vault.

Uso:  .venv\Scripts\python.exe scripts\smoke_test.py
No envia ninguna orden (el cliente ni siquiera puede).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nt8_mcp.client import NT8Client, NT8Error  # noqa: E402
from nt8_mcp.config import Config  # noqa: E402
from nt8_mcp.nt8db import NT8Database  # noqa: E402


def show(label: str, value) -> None:
    print(f"\n--- {label} ---")
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str)[:1500])


def main() -> int:
    cfg = Config.load()
    for w in cfg.warnings:
        print("AVISO:", w)
    print(f"Config: {cfg.source}\nPuente: {cfg.host}:{cfg.port}")
    client = NT8Client(cfg.host, cfg.port, cfg.token, cfg.timeout_seconds)
    rc = 0
    try:
        ping = client.call("ping", timeout=8)
        show("ping", ping)
        print("\nAddOn:", ping.get("bridge_version") or "v2 (antiguo: actualiza con scripts\deploy_addon.py)")
        accounts = client.call("accounts")
        show("cuentas", [a["name"] for a in accounts])
        for a in accounts:
            pos = client.call("positions", account=a["name"])
            if pos:
                show(f"posiciones {a['name']}", pos)
        show("instrument_info MNQ", client.call("instrument_info", instrument="MNQ"))
        show("barras MNQ 1m (3)", client.call("bars", instrument="MNQ", period_type="Minute", period_value=1, count=3))
    except NT8Error as exc:
        rc = 1
        print(f"\nPuente NT8 no disponible: {exc}")
        print("Checklist: NT8 abierto · AddOn MCPBridge compilado (F5) · token/puerto de mcp_bridge.json · "
              "puerto distinto de 36973 (ATI)")
    db = NT8Database(cfg.db_path)
    if db.available():
        show("historial (base de datos NT8)", db.summary())
        show("ultimas 2 operaciones reales", [t.as_dict() for t in db.trades(since="30d")[-2:]])
    else:
        print(f"\nNo encuentro la base de datos en {cfg.db_path}")
    print("\nDiagnostico completado.")
    client.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
