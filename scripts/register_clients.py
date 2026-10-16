"""Registra el MCP de NinjaTrader 8 en todos los clientes de IA detectados en este PC.

    python scripts/register_clients.py                 # detecta y registra en los instalados
    python scripts/register_clients.py --list          # solo muestra que detecta
    python scripts/register_clients.py --only vscode,cursor
    python scripts/register_clients.py --remove        # quita el registro de todos
    python scripts/register_clients.py --print-http    # instrucciones para clientes HTTP (Open WebUI, GPTs...)

Cada fichero modificado se respalda antes como <fichero>.bak_nt8mcp. Nunca toca otras entradas.
Clientes con limite de herramientas o modelos locales pequenos reciben el perfil 'core' (~30 herramientas).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

NAME = "ninjatrader8"
ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home()
APPDATA = Path(os.environ.get("APPDATA", HOME / "AppData" / "Roaming"))


def server_spec(profile: str = "full") -> dict:
    env = {"PYTHONPATH": str(ROOT / "src"), "NT8_MCP_CONFIG": str(ROOT / "config.json")}
    if profile != "full":
        env["NT8_MCP_PROFILE"] = profile
    return {"command": str(ROOT / ".venv" / "Scripts" / "python.exe"), "args": ["-m", "nt8_mcp.server"], "env": env}


def _backup(path: Path) -> None:
    if path.exists() and path.stat().st_size:
        shutil.copyfile(path, str(path) + ".bak_nt8mcp")


def _load_json(path: Path) -> dict:
    if not path.exists() or not path.read_text(encoding="utf-8-sig").strip():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} no es un objeto JSON")
    return data


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def json_client(path: Path, key: str, spec_fn=lambda s: s):
    def apply(profile: str, remove: bool) -> str:
        data = _load_json(path)
        servers = data.get(key) if isinstance(data.get(key), dict) else {}
        if remove:
            if servers.pop(NAME, None) is None:
                return "no estaba"
        else:
            servers[NAME] = spec_fn(server_spec(profile))
        data[key] = servers
        _backup(path)
        _save_json(path, data)
        return f"{'eliminado de' if remove else 'registrado en'} {path}"
    return apply


def claude_code(profile: str, remove: bool) -> str:
    exe = shutil.which("claude")
    if exe:
        subprocess.run([exe, "mcp", "remove", NAME, "--scope", "user"], capture_output=True, text=True)
        if remove:
            return "eliminado (claude mcp remove --scope user)"
        r = subprocess.run([exe, "mcp", "add-json", NAME, json.dumps({"type": "stdio", **server_spec(profile)}),
                            "--scope", "user"], capture_output=True, text=True)
        if r.returncode == 0:
            return "registrado con 'claude mcp add-json --scope user'"
    return json_client(HOME / ".claude.json", "mcpServers", lambda s: {"type": "stdio", **s})(profile, remove)


def continue_dev(profile: str, remove: bool) -> str:
    path = HOME / ".continue" / "mcpServers" / f"{NAME}.yaml"
    if remove:
        if path.exists():
            path.unlink()
            return f"eliminado {path}"
        return "no estaba"
    s = server_spec(profile)
    env = "\n".join(f"      {k}: {json.dumps(v)}" for k, v in s["env"].items())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "name: NinjaTrader 8 (solo lectura)\nversion: 3.0.0\nschema: v1\nmcpServers:\n"
        f"  - name: {NAME}\n    command: {json.dumps(s['command'])}\n    args:\n"
        + "".join(f"      - {json.dumps(a)}\n" for a in s["args"]) + f"    env:\n{env}\n", encoding="utf-8")
    return f"registrado en {path}"


def codex(profile: str, remove: bool) -> str:
    path = HOME / ".codex" / "config.toml"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    text = re.sub(rf"\n?\[mcp_servers\.{NAME}(\.env)?\][^\[]*", "", text)
    if not remove:
        s = server_spec(profile)
        env = ", ".join(f"{k} = {json.dumps(v)}" for k, v in s["env"].items())
        text = text.rstrip() + (f"\n\n[mcp_servers.{NAME}]\ncommand = {json.dumps(s['command'])}\n"
                                f"args = {json.dumps(s['args'])}\nenv = {{ {env} }}\n")
    _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return f"{'eliminado de' if remove else 'registrado en'} {path}"


# nombre -> (detectado?, perfil, funcion)
CLIENTS = {
    "claude-desktop": (lambda: (APPDATA / "Claude").exists(), "full",
                       json_client(APPDATA / "Claude" / "claude_desktop_config.json", "mcpServers")),
    "claude-code": (lambda: bool(shutil.which("claude")) or (HOME / ".claude.json").exists(), "full", claude_code),
    "vscode": (lambda: (APPDATA / "Code" / "User").exists(), "full",
               json_client(APPDATA / "Code" / "User" / "mcp.json", "servers", lambda s: {"type": "stdio", **s})),
    "cursor": (lambda: (HOME / ".cursor").exists(), "core", json_client(HOME / ".cursor" / "mcp.json", "mcpServers")),
    "windsurf": (lambda: (HOME / ".codeium" / "windsurf").exists(), "core",
                 json_client(HOME / ".codeium" / "windsurf" / "mcp_config.json", "mcpServers")),
    "antigravity": (lambda: (HOME / ".gemini" / "antigravity-ide").exists(), "full",
                    json_client(HOME / ".gemini" / "antigravity-ide" / "mcp_config.json", "mcpServers")),
    "gemini-cli": (lambda: bool(shutil.which("gemini")) or (HOME / ".gemini" / "settings.json").exists(), "full",
                   json_client(HOME / ".gemini" / "settings.json", "mcpServers")),
    "continue": (lambda: (HOME / ".continue").exists(), "core", continue_dev),
    "codex": (lambda: (HOME / ".codex").exists() or bool(shutil.which("codex")), "full", codex),
    "lmstudio": (lambda: (HOME / ".lmstudio").exists(), "core",
                 json_client(HOME / ".lmstudio" / "mcp.json", "mcpServers")),
}


def print_http() -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from nt8_mcp.config import Config

    cfg = Config.load()
    base = f"http://{cfg.http.host}:{cfg.http.port}"
    tok = cfg.http.token or "(se genera al arrancar la pasarela; ver config.json -> http.token)"
    print(f"""
Pasarela HTTP (arrancar con scripts\\start_http.cmd):
  MCP Streamable HTTP : {base}/mcp          cabecera  Authorization: Bearer {tok}
  MCP SSE             : {base}/sse
  REST                : POST {base}/api/tools/<herramienta>   (GET {base}/api/tools para la lista)
  Funciones (OpenAI)  : GET {base}/api/functions
  OpenAPI             : GET {base}/openapi.json

Ejemplos de configuracion:
  Cliente MCP por URL  {{"mcpServers": {{"{NAME}": {{"url": "{base}/mcp", "headers": {{"Authorization": "Bearer {tok}"}}}}}}}}
  Open WebUI           Ajustes -> Herramientas -> Añadir servidor OpenAPI: {base}  (Bearer {tok})
  curl                 curl -H "Authorization: Bearer {tok}" -X POST {base}/api/tools/nt8_positions -d "{{}}"
  Ollama/modelo local  python scripts\\ollama_agent.py --model qwen3.8:27b
""")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only", help="lista separada por comas: " + ",".join(CLIENTS))
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--profile", choices=["full", "core"], help="fuerza el perfil en todos")
    ap.add_argument("--print-http", action="store_true")
    args = ap.parse_args()

    if args.print_http:
        print_http()
        return 0
    only = {c.strip() for c in args.only.split(",")} if args.only else None
    for name, (detect, profile, fn) in CLIENTS.items():
        if only and name not in only:
            continue
        found = detect()
        if args.list or (not found and not only):
            print(f"  {name:15s} {'detectado' if found else '-'}")
            continue
        try:
            print(f"  {name:15s} {fn(args.profile or profile, args.remove)}  [perfil {args.profile or profile}]")
        except Exception as exc:  # noqa: BLE001
            print(f"  {name:15s} ERROR: {exc}")
    if not args.remove and not args.list:
        print("\nReinicia cada aplicacion para que cargue el servidor. Para clientes HTTP: --print-http")
    return 0


if __name__ == "__main__":
    sys.exit(main())
