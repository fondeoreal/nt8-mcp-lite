"""Despliega el AddOn MCPBridge v3 (solo lectura) en NinjaTrader 8 de forma segura.

    .venv\\Scripts\\python.exe scripts\\deploy_addon.py --check      # solo comprobaciones, no copia nada
    .venv\\Scripts\\python.exe scripts\\deploy_addon.py              # comprueba y copia
    .venv\\Scripts\\python.exe scripts\\deploy_addon.py --force      # copia aunque haya posiciones abiertas

Pasos:
 1. Si NT8 esta abierto, consulta las posiciones de TODAS las cuentas. Si hay alguna posicion abierta en
    una cuenta no simulada, se niega: recompilar NinjaScript con una posicion viva es un riesgo innecesario.
 2. Compila en seco MCPBridge.cs junto con todo tu bin\\Custom contra las DLL de NT8 (si hay .NET SDK),
    para no desplegar nada que rompa la compilacion de NinjaTrader.
 3. Copia de seguridad del AddOn instalado (fuera de bin\\Custom) y escribe mcp_bridge.json (token/puerto).
 4. Copia el AddOn. Despues: NinjaScript Editor -> F5 (o reiniciar NT8) y comprobar con nt8_status.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nt8_mcp.client import NT8Client, NT8Error  # noqa: E402
from nt8_mcp.config import ATI_PORT, BRIDGE_SETTINGS_FILE, Config  # noqa: E402

SIM_PROVIDERS = {"Simulator", "Playback"}

# AddOns del NT8-MCP. Cada edicion trae su juego en ninjatrader\: la Pro MCPSim.cs y MCPVault.cs,
# la Lite MCPSimLite.cs (sustituto sin escritura). Los que no son de esta edicion se retiran.
ALL_ADDONS = ("MCPBridge.cs", "MCPSim.cs", "MCPSimLite.cs", "MCPVault.cs")


def edition_files() -> list[Path]:
    return [ROOT / "ninjatrader" / n for n in ALL_ADDONS if (ROOT / "ninjatrader" / n).exists()]


def open_real_positions(client: NT8Client) -> list[dict] | None:
    """Posiciones en cuentas no simuladas. None si NT8 no responde."""
    try:
        accounts = client.call("accounts", timeout=10)
    except NT8Error:
        return None
    real = []
    for a in accounts:
        try:
            positions = client.call("positions", account=a["name"], timeout=10)
        except NT8Error:
            continue
        if a.get("provider") in SIM_PROVIDERS or a["name"] in ("Backtest", "Playback101"):
            continue
        real += [{**p, "account": a["name"]} for p in positions]
    return real


def dry_compile(files: list[Path], custom: Path) -> tuple[bool, str]:
    sdk_root = Path(r"C:\Program Files\dotnet\sdk")
    cscs = sorted(sdk_root.glob("*/Roslyn/bincore/csc.dll")) if sdk_root.exists() else []
    if not cscs or not shutil.which("dotnet"):
        return True, "sin .NET SDK: se omite la compilacion en seco (NT8 compilara al pulsar F5)"
    fw = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319")
    wpf = Path(r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\WPF")
    nt = Path(r"C:\Program Files\NinjaTrader 8\bin")
    refs = [fw / n for n in ("mscorlib.dll", "System.dll", "System.Core.dll", "System.Xml.dll", "System.Xml.Linq.dll",
                             "System.Xaml.dll", "System.ComponentModel.DataAnnotations.dll", "Microsoft.CSharp.dll",
                             "System.Windows.Forms.dll", "System.Net.Http.dll", "System.Web.Extensions.dll")]
    refs += [wpf / n for n in ("WindowsBase.dll", "PresentationCore.dll", "PresentationFramework.dll",
                               "UIAutomationProvider.dll")]
    refs += [nt / n for n in ("NinjaTrader.Core.dll", "NinjaTrader.Gui.dll", "SharpDX.dll", "SharpDX.Direct2D1.dll",
                              "InfragisticsWPF.DataPresenter.dll", "Infralution.Localization.Wpf.dll")]
    refs += [custom / "NinjaTrader.Vendor.dll"]
    refs = [r for r in refs if r.exists()]
    sources = [p for p in custom.rglob("*.cs")
               if p.name not in ALL_ADDONS and not any(part in ("bin", "obj") for part in p.relative_to(custom).parts[:-1])]
    out = Path(tempfile.gettempdir()) / "nt8mcp_dry_compile.dll"
    rsp = Path(tempfile.gettempdir()) / "nt8mcp_dry_compile.rsp"
    lines = ["-nologo", "-noconfig", "-nostdlib+", "-target:library", "-langversion:13.0", "-nowarn:1591,0618,0436,0108",
             f'-out:"{out}"'] + [f'-r:"{r}"' for r in refs] + [f'"{s}"' for s in sources + files]
    rsp.write_text("\n".join(lines), encoding="utf-8")
    r = subprocess.run(["dotnet", str(cscs[-1]), f"@{rsp}"], capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    errors = [l for l in (r.stdout + r.stderr).splitlines() if " error " in l]
    if r.returncode != 0 or errors:
        return False, "\n".join(errors[:20]) or (r.stdout + r.stderr)[-2000:]
    return True, f"compilacion en seco correcta ({len(sources) + len(files)} ficheros)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="solo comprobar, no copiar")
    ap.add_argument("--force", action="store_true", help="desplegar aunque haya posiciones reales abiertas")
    ap.add_argument("--skip-compile", action="store_true")
    args = ap.parse_args()

    cfg = Config.load()
    if cfg.port == ATI_PORT:
        print(f"ERROR: el puerto configurado es {ATI_PORT} (ATI de NinjaTrader). Usa 36980 en mcp_bridge.json.")
        return 2
    custom = cfg.custom_root
    addons = custom / "AddOns"
    files = edition_files()
    print(f"NT8: {cfg.nt8_root}\nAddOns: {addons}")

    client = NT8Client(cfg.host, cfg.port, cfg.token, 10)
    info = client.info(max_age=0)
    print(f"1) NT8 {'abierto' if info else 'cerrado o sin puente'}; AddOn instalado: "
          f"{info.get('bridge_version') or ('v2 (antiguo)' if info else 'desconocido')}")
    if info:
        real = open_real_positions(client)
        if real:
            print("   POSICIONES REALES ABIERTAS:")
            for p in real:
                print(f"     {p['account']}: {p['market_position']} {p['quantity']} {p['instrument']} @ {p['average_price']}")
            if not args.force:
                print("   Despliegue cancelado: hazlo cuando estes plano (o --force bajo tu responsabilidad).")
                return 3
        else:
            print("   sin posiciones abiertas en cuentas reales")

    if not args.skip_compile:
        ok, msg = dry_compile(files, custom)
        print(f"2) {msg}")
        if not ok:
            print("   Despliegue cancelado: la compilacion fallaria en NinjaTrader.")
            return 4

    if args.check:
        print("3) --check: no se ha copiado nada.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = ROOT / "backups" / f"addon_{stamp}"
    backup.mkdir(parents=True, exist_ok=True)
    for f in addons.glob("MCP*.cs"):
        shutil.copy2(f, backup / f.name)
    settings = cfg.nt8_root / BRIDGE_SETTINGS_FILE
    if not settings.exists():
        token = cfg.token
        if not token or token == "CAMBIA-ESTE-TOKEN" or len(token) < 16:
            import secrets
            token = secrets.token_urlsafe(21)  # el AddOn no atiende con el token por defecto
            print("   token nuevo generado (no habia uno propio)")
        settings.write_text(json.dumps({"token": token, "port": cfg.port}, indent=2), encoding="utf-8")
    for f in files:
        text = f.read_text(encoding="utf-8-sig")
        (addons / f.name).write_text(text, encoding="utf-8-sig", newline="\r\n")
    retirados = []
    for name in ALL_ADDONS:
        if name not in {f.name for f in files} and (addons / name).exists():
            (addons / name).unlink()
            retirados.append(name)
    print(f"3) copia de seguridad en {backup}\n   token/puerto en {settings}\n   AddOns copiados: "
          + ", ".join(f.name for f in files)
          + (f"\n   retirados (de la otra edicion): {', '.join(retirados)}" if retirados else ""))
    print("\nSiguiente paso EN NINJATRADER: Nuevo -> Editor NinjaScript -> F5 (o reinicia NT8).\n"
          "Luego comprueba con la herramienta nt8_status: bridge.addon_version debe ser 3.0.0.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
