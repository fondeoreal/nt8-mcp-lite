# NT8-MCP Lite — que cualquier IA lea tu NinjaTrader 8 (solo lectura)

**Pregunta a Claude, ChatGPT, Copilot, Cursor, Gemini o a un modelo local de Ollama por tus cuentas, operaciones y gráficos de NinjaTrader 8, sin darle el poder de mandar ni una orden.**

> «¿Por qué perdí dinero la semana pasada?» · «Enséñame mi acierto por hora en MNQ» · «¿Qué tengo abierto ahora en todas mis cuentas?» · «Explícame qué hace el OnBarUpdate de esta estrategia»

NT8-MCP es un servidor [MCP](https://modelcontextprotocol.io) más un pequeño AddOn de NinjaTrader 8. Tu asistente de IA recibe **37 herramientas de solo lectura**; NinjaTrader sigue en tus manos.

*English: [README.md](README.md)*

## Seguro por diseño

Mandar, modificar o cancelar órdenes es **imposible**, no una opción que se pueda activar por error:

1. El servidor MCP **no tiene ninguna herramienta de órdenes**.
2. El cliente TCP local **rechaza** los comandos de órdenes antes de que salgan del PC.
3. El AddOn dentro de NinjaTrader **no contiene código de órdenes**; los comandos antiguos responden `BLOQUEADO`.

Todo funciona en `127.0.0.1`. Sin nube, sin telemetría, sin pedir contraseñas de cuentas.

## Qué puede leer tu IA (37 herramientas)

| Grupo | Herramientas |
|---|---|
| **Estado** | `nt8_help`, `nt8_status`, `nt8_ping`, `nt8_paths`, `nt8_security_policy` |
| **Cuentas en vivo** | cuentas, resumen de todas, estado de una cuenta, todos los AccountItem (incluidos los de trailing drawdown de las prop firms), posiciones, órdenes y ejecuciones de la sesión, conexiones, eventos en tiempo real, log del AddOn |
| **Historial** *(funciona con NinjaTrader cerrado: lee su base de datos en solo lectura)* | resumen de la base de datos, cuentas, meses de ejecuciones y órdenes, **operaciones de ida y vuelta (FIFO) con PnL neto**, **rendimiento**: acierto con intervalo de confianza, factor de beneficio, esperanza, drawdown, rachas, por día y hora |
| **Mercado** | ficha del instrumento (tick, valor, vencimiento), búsqueda, horarios, cotizaciones |
| **Histórico de precios** | inventario de datos, barras OHLCV (tick, volumen, rango, segundos, minutos, días…), barras desde CSV, estadísticas (ATR, rangos, volatilidad) |
| **Estrategias y código** | estrategias en marcha, plantillas ATM, listar / esquema / leer / buscar en tus `.cs` de NinjaScript |

## Requisitos

- Windows 10/11 con **NinjaTrader 8** (8.1.x).
- **Python 3.10 o superior** ([python.org](https://www.python.org/downloads/), marcando *Add python.exe to PATH*).
- Una app de IA compatible con MCP: Claude Desktop, Claude Code, VS Code (agente de Copilot), Cursor, Windsurf, Google Antigravity, Continue (Ollama), Gemini CLI, Codex CLI o LM Studio.

## Instalación (5 minutos)

1. Descarga y descomprime este repositorio.
2. Doble clic en **`Instalar.cmd`**. Crea un entorno de Python en `%LOCALAPPDATA%\NT8-MCP`, genera un token local aleatorio, instala el AddOn y registra el servidor en todas las apps de IA que encuentre.
   - Si NinjaTrader está **abierto**, no toca el AddOn. Cuando estés **plano**, ejecuta `"%LOCALAPPDATA%\NT8-MCP\.venv\Scripts\python.exe" "%LOCALAPPDATA%\NT8-MCP\scripts\deploy_addon.py"` (se niega si hay una posición real abierta y antes compila en seco).
3. En NinjaTrader: **Nuevo → Editor NinjaScript → F5** (o reinicia NinjaTrader).
4. Reinicia tu app de IA y pídele: *«Ejecuta nt8_status»*.

Configuración manual para cualquier cliente: [CLIENTES_IA.md](CLIENTES_IA.md). Desinstalar: `install.ps1 -Uninstall`.

## Ejemplos de preguntas

- *«Con nt8_history_performance, analiza mis últimos 30 días en la cuenta X agrupados por hora. ¿Dónde pierdo dinero?»*
- *«Lista mis posiciones abiertas en todas las cuentas y la distancia al trailing drawdown de cada una.»*
- *«Lee mi estrategia MiRuptura.cs y explícame la lógica de entrada en lenguaje llano.»*
- *«Compara el ATR medio de 1 minuto del MNQ en los 30 minutos tras la apertura con el resto de la sesión (últimos 3 meses).»*

## ¿Quieres más? NT8-MCP Pro

La edición Pro añade **51 herramientas** para analizar en serio: Monte Carlo de tus operaciones reales, **simulación de cuentas de fondeo** (trailing drawdown, pérdida diaria), línea de tiempo de cada orden y motivo exacto de los rechazos, vault de backtests con los exports del Strategy Analyzer, motor rápido de backtest por reglas, barridos de parámetros, exportaciones en segundo plano, plantillas de estrategia, edición de código segura con copias, reflexión y un modo de trading solo en simulador. → [fondeoreal.com/nt8-mcp](https://fondeoreal.com/nt8-mcp/)

## Aviso

Software "tal cual" (licencia MIT). Es una herramienta, no asesoramiento de inversión. El trading de futuros implica un riesgo elevado de pérdida. NinjaTrader es marca de su titular; este proyecto no está afiliado ni respaldado por NinjaTrader, LLC.
