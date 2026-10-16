"""Pasarela HTTP: expone el MCP de NT8 a CUALQUIER IA, no solo a clientes MCP por stdio.

    python -m nt8_mcp.http_gateway [--host 127.0.0.1] [--port 8765] [--profile full|core]

Rutas (todas con 'Authorization: Bearer <token>' salvo /health):
    /mcp                      MCP Streamable HTTP  (Claude, Cursor, VS Code, Windsurf, Gemini CLI, Codex, LM Studio...)
    /sse  + /messages/        MCP por SSE (clientes antiguos)
    GET  /api/tools           lista de herramientas con su JSON Schema
    POST /api/tools/{nombre}  ejecuta una herramienta: body JSON con los argumentos
    GET  /api/functions       las herramientas en formato "function calling" (OpenAI / Ollama / Gemini / LangChain)
    GET  /openapi.json        OpenAPI 3.1 (Custom GPTs via tunel, Open WebUI, n8n, Postman...)
    GET  /health              comprobacion sin token

Por defecto solo escucha en 127.0.0.1. El token se genera la primera vez y se guarda en config.json
("http": {"token": ...}). Todas las herramientas son las mismas que por stdio: SOLO LECTURA de ordenes.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import secrets
import sys
from typing import Any


def _is_loopback(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def build_app(host: str, port: int, token: str, enable_rest: bool = True, cors_origins: list[str] | None = None):
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.concurrency import run_in_threadpool
    from starlette.middleware.cors import CORSMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from . import server as srv

    mcp = srv.mcp
    mcp.settings.host = host
    mcp.settings.port = port
    allowed_hosts = [f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost"]
    if not _is_loopback(host):
        allowed_hosts += [f"{host}:{port}", host]
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts,
        allowed_origins=[f"http://{h}" for h in allowed_hosts] + list(cors_origins or []))

    app = mcp.streamable_http_app()
    sse = mcp.sse_app()
    app.router.routes.extend(sse.routes)

    tool_manager = mcp._tool_manager  # API interna estable en mcp 1.x: validacion de argumentos de FastMCP

    async def tools_schema() -> list[dict]:
        return [{"name": t.name, "title": t.title, "description": t.description, "input_schema": t.inputSchema,
                 "read_only": bool(t.annotations and t.annotations.readOnlyHint)} for t in await mcp.list_tools()]

    async def health(_: Request) -> JSONResponse:
        info = srv.NT8.info()
        return JSONResponse({"ok": True, "service": "nt8-mcp", "version": srv.VERSION, "nt8_reachable": bool(info),
                             "addon_version": info.get("bridge_version"), "read_only_orders": True})

    async def list_tools(_: Request) -> JSONResponse:
        return JSONResponse({"tools": await tools_schema()})

    async def functions(_: Request) -> JSONResponse:
        return JSONResponse([{"type": "function", "function": {
            "name": t["name"], "description": (t["description"] or "")[:1024], "parameters": t["input_schema"]}}
            for t in await tools_schema()])

    async def call_tool(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        tool = tool_manager.get_tool(name)
        if tool is None:
            return JSONResponse({"ok": False, "error": f"herramienta desconocida: {name}"}, status_code=404)
        try:
            body = await request.json() if (await request.body()) else {}
        except ValueError:
            return JSONResponse({"ok": False, "error": "el cuerpo debe ser JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "el cuerpo debe ser un objeto JSON"}, status_code=400)
        try:
            args = tool.fn_metadata.arg_model.model_validate(body).model_dump_one_level()
        except Exception as exc:  # pydantic.ValidationError
            return JSONResponse({"ok": False, "error": f"argumentos invalidos: {exc}"}, status_code=422)
        text = await run_in_threadpool(lambda: tool.fn(**args))
        if isinstance(text, str) and text.startswith("ERROR:"):
            return JSONResponse({"ok": False, "error": text[7:]}, status_code=200)
        try:
            result: Any = json.loads(text)
        except (TypeError, ValueError):
            result = text
        return JSONResponse({"ok": True, "result": result})

    async def openapi(_: Request) -> JSONResponse:
        paths = {}
        for t in await tools_schema():
            paths[f"/api/tools/{t['name']}"] = {"post": {
                "operationId": t["name"], "summary": t["title"] or t["name"],
                "description": (t["description"] or "")[:300],
                "requestBody": {"required": False, "content": {"application/json": {"schema": t["input_schema"]}}},
                "responses": {"200": {"description": "Resultado", "content": {"application/json": {"schema": {
                    "type": "object", "properties": {"ok": {"type": "boolean"}, "result": {}, "error": {"type": "string"}}}}}}},
            }}
        return JSONResponse({
            "openapi": "3.1.0",
            "info": {"title": "NinjaTrader 8 (solo lectura)", "version": srv.VERSION,
                     "description": "Cuentas, historial, historico de precios, backtests y codigo de NinjaTrader 8. "
                                    "Enviar/modificar/cancelar ordenes esta bloqueado."},
            "servers": [{"url": f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"}],
            "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
            "security": [{"bearer": []}],
            "paths": paths,
        })

    app.router.routes.append(Route("/health", health, methods=["GET"]))
    if enable_rest:
        app.router.routes.extend([
            Route("/api/tools", list_tools, methods=["GET"]),
            Route("/api/functions", functions, methods=["GET"]),
            Route("/api/tools/{name}", call_tool, methods=["POST"]),
            Route("/openapi.json", openapi, methods=["GET"]),
        ])

    expected = f"Bearer {token}".encode()

    class BearerAuth:
        """Exige el token en todo salvo /health. Tambien acepta ?token= (clientes sin cabeceras)."""

        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http" or scope.get("path") == "/health" or scope.get("method") == "OPTIONS":
                return await self.inner(scope, receive, send)
            headers = dict(scope.get("headers") or [])
            given = headers.get(b"authorization", b"")
            if not given:
                from urllib.parse import parse_qs
                qs = parse_qs((scope.get("query_string") or b"").decode())
                if qs.get("token"):
                    given = f"Bearer {qs['token'][0]}".encode()
            if not hmac.compare_digest(given, expected):
                body = json.dumps({"ok": False, "error": "token invalido o ausente (Authorization: Bearer <token>)"}).encode()
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": body})
                return
            return await self.inner(scope, receive, send)

    if cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=cors_origins, allow_methods=["GET", "POST", "DELETE"],
                           allow_headers=["*"], expose_headers=["mcp-session-id"])
    app.add_middleware(BearerAuth)
    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="Pasarela HTTP del MCP de NinjaTrader 8 (solo lectura de ordenes)")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--profile", choices=["full", "core"])
    ap.add_argument("--no-rest", action="store_true")
    ap.add_argument("--show-token", action="store_true", help="muestra el token y sale")
    args = ap.parse_args()

    if args.profile:
        os.environ["NT8_MCP_PROFILE"] = args.profile
    from .config import Config

    cfg = Config.load()
    host = args.host or cfg.http.host
    port = args.port or cfg.http.port
    token = cfg.http.token
    if not token:
        token = secrets.token_urlsafe(24)
        cfg.save_http_token(token)
    if args.show_token:
        print(token)
        return
    if not _is_loopback(host):
        print(f"AVISO: escuchando en {host}, accesible desde la red. Protege el puerto (firewall/VPN) y el token.",
              file=sys.stderr)

    import uvicorn

    app = build_app(host, port, token, enable_rest=cfg.http.enable_rest and not args.no_rest,
                    cors_origins=cfg.http.cors_origins)
    print(f"NT8-MCP HTTP en http://{host}:{port}  (MCP: /mcp · SSE: /sse · REST: /api/tools · OpenAPI: /openapi.json)",
          file=sys.stderr)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
