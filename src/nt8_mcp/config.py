"""Configuracion del servidor MCP de NT8 (v3, solo lectura de ordenes).

El servidor NO puede enviar, modificar ni cancelar ordenes. No hay ninguna clave de
configuracion que lo habilite: el bloqueo esta en el codigo (aqui y en MCPBridge.cs).
Las claves antiguas de trading (trading_enabled, max_quantity...) se ignoran.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# El puerto 36973 es el ATI (Automated Trading Interface) de NinjaTrader: nunca es el puente.
ATI_PORT = 36973
BRIDGE_SETTINGS_FILE = "mcp_bridge.json"

DEFAULTS: dict = {
    "host": "127.0.0.1",
    "port": 36980,
    "token": "CAMBIA-ESTE-TOKEN",
    "timeout_seconds": 30.0,
    "default_account": None,
    # --- Rutas (None = autodetectar Documentos\NinjaTrader 8) ---
    "ninjatrader_path": None,
    "vault_path": None,
    "data_path": None,                 # exportaciones de historico (None = NinjaTrader 8\mcp_data)
    "extra_vault_dirs": None,          # carpetas extra con CSV de backtest (solo lectura)
    # --- Permisos que NO afectan a ordenes ---
    "allow_source_write": False,       # escribir .cs (nunca en AddOns) y plantillas de estrategia
    "allow_reflection": True,          # herramientas reflect_*/sa_* (el puente limita la escritura)
    # --- Escritura SOLO en cuentas del simulador (v3.1: herramientas nt8_sim_*; el AddOn MCPSim.cs
    #     rechaza cualquier cuenta que no sea Provider.Simulator; las cuentas reales siguen bloqueadas) ---
    "allow_sim_trading": False,
    # --- Edicion: "lite" o "pro" (la Pro necesita licencia; sin ella se queda en Lite) ---
    "edition": "lite",
    "license_key": None,
    # --- Pasarela HTTP para otras IAs (python -m nt8_mcp.http_gateway) ---
    "http": {
        "host": "127.0.0.1",
        "port": 8765,
        "token": None,                 # None = se genera y guarda en config.json al arrancar
        "enable_rest": True,
        "cors_origins": [],
    },
}

_IGNORED_LEGACY = {
    "trading_enabled", "allowed_accounts", "allowed_instruments", "max_quantity",
    "require_bracket", "max_orders_per_session",
}


def config_path() -> Path:
    env = os.environ.get("NT8_MCP_CONFIG")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "config.json"


def default_nt8_path() -> Path:
    """Ruta habitual de la carpeta de usuario de NinjaTrader 8."""
    home = Path.home()
    candidates = [
        home / "Documents" / "NinjaTrader 8",
        home / "Documentos" / "NinjaTrader 8",
        home / "OneDrive" / "Documents" / "NinjaTrader 8",
        home / "OneDrive" / "Documentos" / "NinjaTrader 8",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def _read_json(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


@dataclass
class HttpConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    token: str | None = None
    enable_rest: bool = True
    cors_origins: list = field(default_factory=list)


@dataclass
class Config:
    host: str = DEFAULTS["host"]
    port: int = DEFAULTS["port"]
    token: str = DEFAULTS["token"]
    timeout_seconds: float = DEFAULTS["timeout_seconds"]
    default_account: str | None = None
    ninjatrader_path: str | None = None
    vault_path: str | None = None
    data_path: str | None = None
    extra_vault_dirs: list | None = None
    allow_source_write: bool = False
    allow_reflection: bool = True
    allow_sim_trading: bool = False
    edition: str = "lite"
    license_key: str | None = None
    http: HttpConfig = field(default_factory=HttpConfig)
    source: str = "defaults"
    warnings: list = field(default_factory=list)

    # -----------------------------------------------------------------
    @classmethod
    def load(cls) -> "Config":
        data = json.loads(json.dumps(DEFAULTS))
        path = config_path()
        warnings: list[str] = []
        file_data = _read_json(path) if path.exists() else {}
        legacy = sorted(k for k in file_data if k in _IGNORED_LEGACY)
        if legacy:
            warnings.append("Claves de trading ignoradas en config.json (el servidor es de solo lectura): "
                            + ", ".join(legacy))
        http = dict(data["http"])
        http.update(file_data.get("http") or {})
        data.update({k: v for k, v in file_data.items() if k not in _IGNORED_LEGACY and k != "http"})
        data["http"] = http

        # Variables de entorno (utiles en la config de cada cliente MCP)
        env_map = {
            "NT8_MCP_HOST": ("host", str),
            "NT8_MCP_PORT": ("port", int),
            "NT8_MCP_TOKEN": ("token", str),
            "NT8_MCP_DEFAULT_ACCOUNT": ("default_account", str),
            "NT8_MCP_PATH": ("ninjatrader_path", str),
            "NT8_MCP_VAULT": ("vault_path", str),
            "NT8_MCP_EDITION": ("edition", str),
            "NT8_MCP_LICENSE": ("license_key", str),
        }
        for env_key, (name, caster) in env_map.items():
            raw = os.environ.get(env_key)
            if raw:
                data[name] = caster(raw)

        cfg = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k != "http"})
        cfg.http = HttpConfig(**{k: v for k, v in http.items() if k in HttpConfig.__dataclass_fields__})
        cfg.source = str(path) if path.exists() else "defaults"

        # Fuente unica de verdad para token/puerto: mcp_bridge.json (lo lee tambien el AddOn v3).
        bridge_file = cfg.nt8_root / BRIDGE_SETTINGS_FILE
        if bridge_file.exists() and not os.environ.get("NT8_MCP_TOKEN"):
            shared = _read_json(bridge_file)
            if shared.get("token"):
                cfg.token = str(shared["token"])
            if shared.get("port") and not os.environ.get("NT8_MCP_PORT"):
                cfg.port = int(shared["port"])
            cfg.source += f" + {bridge_file}"

        if cfg.port == ATI_PORT:
            warnings.append(f"El puerto {ATI_PORT} es el ATI de NinjaTrader, no el puente MCP. "
                            "Revisa 'port' en config.json / mcp_bridge.json (por defecto 36980).")
        cfg.warnings = warnings
        return cfg

    # -----------------------------------------------------------------
    @property
    def nt8_root(self) -> Path:
        return Path(self.ninjatrader_path) if self.ninjatrader_path else default_nt8_path()

    @property
    def custom_root(self) -> Path:
        return self.nt8_root / "bin" / "Custom"

    @property
    def vault_root(self) -> Path:
        return Path(self.vault_path) if self.vault_path else self.nt8_root / "mcp_vault"

    @property
    def data_root(self) -> Path:
        return Path(self.data_path) if self.data_path else self.nt8_root / "mcp_data"

    @property
    def db_path(self) -> Path:
        return self.nt8_root / "db" / "NinjaTrader.sqlite"

    @property
    def vault_extra(self) -> list[Path]:
        if self.extra_vault_dirs is not None:
            return [Path(p) for p in self.extra_vault_dirs]
        return []

    def save_http_token(self, token: str) -> None:
        """Persiste el token HTTP generado en config.json (sin tocar el resto)."""
        path = config_path()
        data = _read_json(path) if path.exists() else {}
        http = data.get("http") or {}
        http["token"] = token
        data["http"] = http
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        self.http.token = token


def resolve_paths(cfg: Config) -> tuple[Path, Path, Path]:
    """Compatibilidad: (raiz NT8, carpeta Custom, carpeta vault)."""
    return cfg.nt8_root, cfg.custom_root, cfg.vault_root
