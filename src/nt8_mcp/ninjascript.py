"""Acceso al codigo fuente NinjaScript en Documentos\\NinjaTrader 8\\bin\\Custom.

Toda escritura crea antes una copia de seguridad con marca de tiempo.
Nada fuera de la carpeta Custom es accesible.

Proteccion de la politica de solo lectura: la carpeta AddOns (donde vive MCPBridge.cs, que
bloquea el envio de ordenes) y cualquier fichero MCP*.cs NUNCA se pueden escribir ni restaurar
desde aqui, sea cual sea la configuracion. Asi ninguna IA puede reescribir el puente para
quitarse el bloqueo. Las estrategias e indicadores si se pueden editar (con allow_source_write).
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

FOLDERS = ["Strategies", "Indicators", "AddOns", "BarsTypes", "DrawingTools", "Optimizers", "Types",
           "ChartStyles", "OptimizationFitnesses", "PerformanceMetrics", "MarketAnalyzerColumns", "SuperDomColumns",
           "ImportTypes", "ShareServices"]
# Carpetas donde se puede escribir. AddOns, ShareServices, etc. quedan fuera a proposito.
WRITABLE_FOLDERS = {"strategies", "indicators", "barstypes", "drawingtools", "optimizers",
                    "optimizationfitnesses", "performancemetrics", "chartstyles", "marketanalyzercolumns"}
MAX_BYTES = 400_000


class SourceError(RuntimeError):
    pass


class NinjaScriptFiles:
    def __init__(self, custom_root: Path, backup_root: Path | None = None) -> None:
        self.root = Path(custom_root)
        self.backups = Path(backup_root) if backup_root else self.root.parent / "mcp_backups"

    # -- helpers ---------------------------------------------------
    def _check_writable(self, path: Path) -> None:
        rel = path.relative_to(self.root.resolve())
        top = rel.parts[0].lower() if len(rel.parts) > 1 else ""
        if path.name.lower().startswith("mcp") or top not in WRITABLE_FOLDERS:
            raise SourceError(
                f"escritura BLOQUEADA en '{rel.as_posix()}': solo se pueden escribir ficheros de "
                f"{', '.join(sorted(WRITABLE_FOLDERS))}. AddOns y los ficheros MCP* (el puente de solo "
                "lectura) estan protegidos siempre."
            )

    def _resolve(self, relative: str) -> Path:
        if not relative:
            raise SourceError("ruta vacia")
        candidate = (self.root / relative.replace("\\", "/")).resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError:
            raise SourceError(f"ruta fuera de la carpeta Custom: {relative}") from None
        if candidate.suffix.lower() != ".cs":
            raise SourceError("solo se permiten ficheros .cs")
        return candidate

    def _check_root(self) -> None:
        if not self.root.exists():
            raise SourceError(
                f"No encuentro la carpeta Custom en {self.root}. "
                "Ajusta 'ninjatrader_custom_path' en config.json."
            )

    # -- API -------------------------------------------------------
    def list_files(self, folder: str | None = None, pattern: str | None = None) -> list[dict]:
        self._check_root()
        if folder:  # solo carpetas conocidas de Custom: nada de '..' ni rutas absolutas
            match = [f for f in FOLDERS if f.lower() == str(folder).strip().strip("/\\").lower()]
            if not match:
                raise SourceError(f"carpeta no valida: {folder!r}. Usa una de: {', '.join(FOLDERS)}")
            folders = match
        else:
            folders = FOLDERS
        results: list[dict] = []
        for name in folders:
            base = self.root / name
            if not base.exists():
                continue
            for path in sorted(base.rglob("*.cs")):
                rel = path.relative_to(self.root).as_posix()
                if pattern and pattern.lower() not in rel.lower():
                    continue
                stat = path.stat()
                results.append(
                    {
                        "path": rel,
                        "folder": name,
                        "size_kb": round(stat.st_size / 1024, 1),
                        "lines": _count_lines(path),
                        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                    }
                )
        return results

    def read(self, relative: str, start_line: int = 1, max_lines: int = 800) -> dict:
        self._check_root()
        path = self._resolve(relative)
        if not path.exists():
            raise SourceError(f"no existe: {relative}")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, start_line)
        chunk = lines[start - 1 : start - 1 + max_lines]
        return {
            "path": relative,
            "total_lines": len(lines),
            "start_line": start,
            "returned_lines": len(chunk),
            "truncated": start - 1 + len(chunk) < len(lines),
            "content": "\n".join(chunk),
        }

    def outline(self, relative: str) -> dict:
        """Esqueleto del fichero: clases, metodos y propiedades con su linea."""
        self._check_root()
        path = self._resolve(relative)
        if not path.exists():
            raise SourceError(f"no existe: {relative}")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        pattern = re.compile(
            r"^\s*(?:\[.*\]\s*)?(?:public|private|protected|internal)\s+"
            r"(?:static\s+|override\s+|virtual\s+|sealed\s+|partial\s+|async\s+)*"
            r"(?:class|struct|enum|interface|void|int|double|bool|string|[A-Z][\w<>,\[\] ]*)\s+"
            r"([A-Za-z_]\w*)\s*[\(\{:]"
        )
        symbols = []
        for i, line in enumerate(lines, start=1):
            match = pattern.match(line)
            if match:
                symbols.append({"line": i, "name": match.group(1), "signature": line.strip()[:160]})
        return {"path": relative, "total_lines": len(lines), "symbols": symbols}

    def write(self, relative: str, content: str) -> dict:
        self._check_root()
        if len(content.encode("utf-8")) > MAX_BYTES:
            raise SourceError(f"contenido demasiado grande (limite {MAX_BYTES} bytes)")
        path = self._resolve(relative)
        self._check_writable(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        backup = None
        if path.exists():
            self.backups.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = self.backups / f"{path.stem}_{stamp}{path.suffix}"
            shutil.copy2(path, backup)

        path.write_text(content, encoding="utf-8", newline="\r\n")
        return {
            "path": relative,
            "bytes_written": len(content.encode("utf-8")),
            "backup": backup.name if backup else None,
            "next_step": "Pulsa F5 en el NinjaScript Editor de NT8 para compilar. "
                         "NinjaTrader no compila desde fuera.",
        }

    def list_backups(self, stem: str | None = None) -> list[dict]:
        if not self.backups.exists():
            return []
        items = []
        for path in sorted(self.backups.glob("*.cs"), key=lambda p: p.stat().st_mtime, reverse=True):
            if stem and stem.lower() not in path.stem.lower():
                continue
            items.append(
                {
                    "name": path.name,
                    "size_kb": round(path.stat().st_size / 1024, 1),
                    "created": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
                }
            )
        return items

    def restore_backup(self, backup_name: str, relative: str) -> dict:
        name = str(backup_name or "")
        if not name or Path(name).name != name or name in (".", "..") or not name.lower().endswith(".cs"):
            raise SourceError(f"nombre de copia no valido: {backup_name!r} (usa un nombre de nt8_source_backups)")
        source = self.backups / name
        if not source.exists():
            raise SourceError(f"copia no encontrada: {backup_name}")
        target = self._resolve(relative)
        self._check_writable(target)
        if target.exists():
            self.write(relative, target.read_text(encoding="utf-8", errors="replace"))
        shutil.copy2(source, target)
        return {"restored": relative, "from": backup_name}


def _count_lines(path: Path) -> int:
    try:
        with path.open("rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0
