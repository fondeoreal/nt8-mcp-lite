# Conectar cualquier IA al MCP de NinjaTrader 8

Todas las vías exponen las mismas herramientas de **solo lectura**. Ninguna puede enviar, modificar ni cancelar
órdenes.

Rutas usadas abajo:
- `PY` = `%LOCALAPPDATA%\NT8-MCP\.venv\Scripts\python.exe` (la ruta real, p. ej. `C:\Users\<tu usuario>\AppData\Local\NT8-MCP\.venv\Scripts\python.exe`)
- `ROOT` = `%LOCALAPPDATA%\NT8-MCP`

## 1. Registro automático (stdio)

```
PY scripts\register_clients.py            # detecta y registra
PY scripts\register_clients.py --list     # qué detecta
PY scripts\register_clients.py --remove   # deshacer
```

| Cliente | Fichero | Perfil |
|---|---|---|
| Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json` | full |
| Claude Code | `claude mcp add-json --scope user` | full |
| VS Code (Copilot agente) | `%APPDATA%\Code\User\mcp.json` | full |
| Google Antigravity | `~\.gemini\antigravity-ide\mcp_config.json` | full |
| Continue (modelos Ollama) | `~\.continue\mcpServers\ninjatrader8.yaml` | core |
| Cursor | `~\.cursor\mcp.json` | core (límite de ~40 herramientas) |
| Windsurf | `~\.codeium\windsurf\mcp_config.json` | core |
| Gemini CLI | `~\.gemini\settings.json` | full |
| Codex CLI | `~\.codex\config.toml` | full |
| LM Studio | `~\.lmstudio\mcp.json` | core |

Cada fichero se respalda como `*.bak_nt8mcp` antes de tocarlo. Reinicia la aplicación después.

Entrada manual equivalente (JSON estándar `mcpServers`):

```json
{
  "mcpServers": {
    "ninjatrader8": {
      "command": "PY",
      "args": ["-m", "nt8_mcp.server"],
      "env": { "PYTHONPATH": "ROOT\\src", "NT8_MCP_CONFIG": "ROOT\\config.json", "NT8_MCP_PROFILE": "full" }
    }
  }
}
```

## 2. Pasarela HTTP (cualquier cliente por URL, apps web, scripts)

```
scripts\start_http.cmd                 # http://127.0.0.1:8765
scripts\start_http.cmd --profile core
PY -m nt8_mcp.http_gateway --show-token
PY scripts\register_clients.py --print-http   # URLs y ejemplos con tu token
```

| Ruta | Uso |
|---|---|
| `/mcp` | MCP Streamable HTTP: `{"url": "http://127.0.0.1:8765/mcp", "headers": {"Authorization": "Bearer TOKEN"}}` |
| `/sse` | MCP por SSE (clientes antiguos) |
| `GET /api/tools` | Lista de herramientas con JSON Schema |
| `POST /api/tools/<nombre>` | Ejecuta la herramienta. Body = argumentos JSON. Respuesta `{"ok":…, "result":…}` |
| `GET /api/functions` | Herramientas en formato *function calling* (OpenAI, Ollama, Gemini, LangChain, n8n) |
| `GET /openapi.json` | OpenAPI 3.1: Open WebUI (servidor de herramientas OpenAPI), Postman, GPT Actions (con túnel) |
| `GET /health` | Sin token |

Autenticación: cabecera `Authorization: Bearer <token>`, o `?token=` para clientes que no admiten cabeceras.
Solo escucha en `127.0.0.1`. Si la expones (`--host 0.0.0.0` o un túnel), protege el token: da acceso de lectura a
tus cuentas.

```
curl -H "Authorization: Bearer TOKEN" -X POST http://127.0.0.1:8765/api/tools/nt8_history_performance ^
     -H "Content-Type: application/json" -d "{\"since\":\"30d\",\"group_by\":\"day\"}"
```

## 3. Modelos locales (Ollama) sin cliente MCP

```
PY scripts\ollama_agent.py --model qwen3.8:27b
PY scripts\ollama_agent.py --model qwen3.6:35b-a3b-coding-mtp-q4_K_M --ask "¿cómo va mi cuenta Lucid este mes?"
```

Usa el perfil `core` (33 herramientas) y tool calling nativo de Ollama. Si el modelo escribe la llamada como texto
(pasa con algunos Qwen), también la reconoce.

## 4. Desde tu propio código Python

```python
import httpx
H = {"Authorization": "Bearer TOKEN"}
r = httpx.post("http://127.0.0.1:8765/api/tools/nt8_history_trades", headers=H, json={"since": "7d"})
print(r.json()["result"]["trades"])
```
