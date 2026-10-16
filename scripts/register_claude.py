"""Registra (o quita) el servidor MCP de NT8 en la configuracion de Claude Desktop.

Se invoca desde install.ps1; tambien sirve suelto:
    python scripts/register_claude.py --root C:\\ruta\\a\\NT8-MCP
    python scripts/register_claude.py --root C:\\ruta\\a\\NT8-MCP --remove

Preserva el resto del fichero de configuracion y deja una copia .bak_nt8mcp.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

SERVER_NAME = "ninjatrader8"


def config_path() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Claude" / "claude_desktop_config.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="Carpeta de instalacion de NT8-MCP")
    ap.add_argument("--remove", action="store_true", help="Quitar el servidor en vez de anadirlo")
    args = ap.parse_args()

    cfg = config_path()
    if cfg is None or not cfg.parent.exists():
        print("  Claude Desktop no esta instalado; omito el registro.")
        return 0

    data: dict = {}
    if cfg.exists():
        shutil.copyfile(str(cfg), str(cfg) + ".bak_nt8mcp")
        try:
            # utf-8-sig: tolera un BOM si otra herramienta lo dejo ahi
            with cfg.open(encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except ValueError as exc:
            print(f"  ERROR: claude_desktop_config.json ilegible ({exc}); no lo modifico.")
            return 1
        if not isinstance(data, dict):
            print("  ERROR: claude_desktop_config.json no es un objeto JSON; no lo modifico.")
            return 1

    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    data["mcpServers"] = servers

    if args.remove:
        if servers.pop(SERVER_NAME, None) is None:
            print(f"  '{SERVER_NAME}' no estaba registrado.")
        else:
            print(f"  '{SERVER_NAME}' eliminado de Claude Desktop.")
        if not servers:
            data.pop("mcpServers", None)
    else:
        root = Path(args.root).resolve()
        servers[SERVER_NAME] = {
            "command": str(root / ".venv" / "Scripts" / "python.exe"),
            "args": ["-m", "nt8_mcp.server"],
            "env": {
                "PYTHONPATH": str(root / "src"),
                "NT8_MCP_CONFIG": str(root / "config.json"),
            },
        }
        print(f"  '{SERVER_NAME}' registrado -> {cfg}")

    cfg.parent.mkdir(parents=True, exist_ok=True)
    with cfg.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
