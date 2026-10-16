# NT8-MCP Lite — let any AI read your NinjaTrader 8 (read-only)

**Ask Claude, ChatGPT, Copilot, Cursor, Gemini or a local Ollama model about your NinjaTrader 8 accounts, trades and charts — without giving it the power to place a single order.**

> "Why did I lose money last week?" · "Show me my win rate by hour on MNQ" · "What's open right now on all my accounts?" · "Explain what this strategy's OnBarUpdate does"

NT8-MCP is a [Model Context Protocol](https://modelcontextprotocol.io) server plus a small NinjaTrader 8 AddOn. Your AI assistant gets **37 read-only tools**; NinjaTrader stays in your hands.

*Documentación en español: [README.es.md](README.es.md)*

## Safe by design

Placing, changing or cancelling orders is **impossible** — not a setting you could flip by mistake:

1. The MCP server has **no order tools** at all.
2. The local TCP client **rejects** order commands before they leave your PC.
3. The AddOn inside NinjaTrader **contains no order code**; old order commands answer `BLOCKED`.

Everything runs on `127.0.0.1`. No cloud, no telemetry, no account credentials.

## What your AI can read (37 tools)

| Group | Tools |
|---|---|
| **Status** | `nt8_help`, `nt8_status`, `nt8_ping`, `nt8_paths`, `nt8_security_policy` |
| **Live accounts** | accounts, all-accounts overview, account status, every AccountItem (incl. prop-firm trailing drawdown fields), open positions, session orders & fills, connections, real-time events, AddOn log |
| **Trade history** *(works with NinjaTrader closed — reads its database read-only)* | database summary, accounts, months of executions and orders, **round-trip trades (FIFO) with net P&L**, **performance**: win rate with confidence interval, profit factor, expectancy, drawdown, streaks, by day/hour |
| **Market** | instrument specs (tick size/value, expiry), instrument search, trading hours, quotes |
| **Price history** | data inventory, OHLCV bars (tick/volume/range/second/minute/day…), bars from CSV, price statistics (ATR, ranges, volatility) |
| **Strategies & code** | running strategies, ATM templates, list / outline / read / search your NinjaScript `.cs` files |

## Requirements

- Windows 10/11 with **NinjaTrader 8** (8.1.x).
- **Python 3.10+** ([python.org](https://www.python.org/downloads/), tick *Add python.exe to PATH*).
- An MCP-capable AI app: Claude Desktop, Claude Code, VS Code (Copilot agent), Cursor, Windsurf, Google Antigravity, Continue (Ollama), Gemini CLI, Codex CLI or LM Studio.

## Install (5 minutes)

1. Download and unzip this repository.
2. Double-click **`Instalar.cmd`**. It creates a Python environment in `%LOCALAPPDATA%\NT8-MCP`, writes a random local token, installs the AddOn and registers the server in every AI app it finds.
   - If NinjaTrader is **open**, the AddOn is not touched. When you are **flat**, run `"%LOCALAPPDATA%\NT8-MCP\.venv\Scripts\python.exe" "%LOCALAPPDATA%\NT8-MCP\scripts\deploy_addon.py"` (it refuses if a real position is open and dry-compiles first).
3. In NinjaTrader: **New → NinjaScript Editor → F5** (or restart NinjaTrader).
4. Restart your AI app and ask: *"Run nt8_status"*.

Manual setup for any client: see [CLIENTES_IA.md](CLIENTES_IA.md). Uninstall: `install.ps1 -Uninstall`.

## Example prompts

- *"Using nt8_history_performance, analyse my last 30 days on account X grouped by hour. Where do I lose money?"*
- *"List my open positions on all accounts and the distance to each account's trailing drawdown."*
- *"Read my strategy MyBreakout.cs and explain the entry logic in plain English."*
- *"Compare MNQ's average 1-minute ATR in the first 30 minutes after the open vs. the rest of the session (last 3 months)."*

## Want more? NT8-MCP Pro

The Pro edition adds **51 tools** for serious analysis: Monte Carlo of your real trades, **prop-firm account simulation** (trailing drawdown, daily loss), order timelines and exact rejection reasons, a backtest vault with Strategy Analyzer exports, a fast rule-based backtester, parameter grids, background data exports, strategy templates, safe code editing with backups, reflection and a simulator-only trading mode. → [fondeoreal.com/en/nt8-mcp](https://fondeoreal.com/en/nt8-mcp/)

## Disclaimer

This software is provided "as is" (MIT License). It is a tool, not investment advice. Futures trading involves substantial risk of loss. NinjaTrader is a trademark of its owner; this project is not affiliated with or endorsed by NinjaTrader, LLC.
