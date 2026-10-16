"""Ediciones del NT8-MCP: Lite (gratis, codigo abierto) y Pro (de pago, con licencia).

La Lite deja a cualquier IA LEER NinjaTrader: cuentas, posiciones, ordenes, mercado, historico de
precios, historial de operaciones con rendimiento basico y codigo NinjaScript (solo lectura).

La Pro anade el analisis avanzado y las herramientas de trabajo: Monte Carlo y simulacion de cuentas
de fondeo, linea de tiempo y rechazos de ordenes, vault de backtests, motor rapido de reglas,
Strategy Analyzer, exportaciones, plantillas, escritura de codigo con copia de seguridad, reflexion
y modo simulador.

Ninguna de las dos puede enviar, modificar ni cancelar ordenes en cuentas reales.
"""

from __future__ import annotations

LITE_TOOLS: frozenset[str] = frozenset({
    # meta
    "nt8_help", "nt8_status", "nt8_ping", "nt8_paths", "nt8_security_policy",
    # cuentas en vivo
    "nt8_accounts", "nt8_accounts_overview", "nt8_account_status", "nt8_account_items",
    "nt8_positions", "nt8_orders", "nt8_executions", "nt8_connections", "nt8_events", "nt8_bridge_log",
    # mercado
    "nt8_instrument_info", "nt8_instrument_search", "nt8_trading_hours", "nt8_quote", "nt8_quotes",
    # historico de precios (lectura; las exportaciones en segundo plano son Pro)
    "nt8_data_inventory", "nt8_bars", "nt8_bars_stats", "nt8_data_files", "nt8_bars_file",
    # historial de la cuenta (lo basico)
    "nt8_history_summary", "nt8_history_accounts", "nt8_history_executions", "nt8_history_orders",
    "nt8_history_trades", "nt8_history_performance",
    # estrategias y codigo (solo lectura)
    "nt8_strategies", "nt8_atm_templates",
    "nt8_source_list", "nt8_source_outline", "nt8_source_read", "nt8_source_search",
})

# Prompts que solo tienen sentido con herramientas Pro
PRO_PROMPTS: frozenset[str] = frozenset({"probar_idea", "analizar_backtest"})
# Recursos que documentan herramientas Pro
PRO_RESOURCES: frozenset[str] = frozenset({"quicktest_help"})

EDITIONS = ("lite", "pro")


def is_lite_tool(name: str) -> bool:
    return name in LITE_TOOLS
