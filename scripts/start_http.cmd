@echo off
rem Pasarela HTTP del MCP de NinjaTrader 8 (solo lectura): MCP /mcp, SSE /sse, REST /api, OpenAPI /openapi.json
rem Uso: start_http.cmd [--port 8765] [--profile core]
set ROOT=%~dp0..
set PYTHONPATH=%ROOT%\src
set NT8_MCP_CONFIG=%ROOT%\config.json
"%ROOT%\.venv\Scripts\python.exe" -m nt8_mcp.http_gateway %*
