"""Agente de consola: un modelo LOCAL de Ollama (Qwen, Llama, Gemma...) con las herramientas de NT8.

    .venv\\Scripts\\python.exe scripts\\ollama_agent.py --model qwen3.8:27b
    .venv\\Scripts\\python.exe scripts\\ollama_agent.py --model qwen2.5-coder:7b-instruct-q6_K --ask "¿que posiciones tengo?"

Usa las mismas herramientas de solo lectura que el MCP (perfil 'core' por defecto, ~30), llamandolas
en el propio proceso: no necesita la pasarela HTTP. El modelo debe soportar tool calling en Ollama.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("NT8_MCP_CONFIG", str(ROOT / "config.json"))

SYSTEM = ("Eres un asistente de trading con acceso de SOLO LECTURA a NinjaTrader 8 mediante herramientas. "
          "Usa las herramientas para responder con datos reales; no inventes cifras. No puedes enviar, modificar "
          "ni cancelar ordenes: si te lo piden, explica que debe hacerse en NinjaTrader. Responde en espanol.")


def _calls_in_text(text: str) -> list[dict]:
    """Algunos modelos (p. ej. Qwen coder) escriben la llamada como JSON en el texto en vez de tool_calls."""
    calls = []
    for m in re.finditer(r"\{[^{}]*\"name\"\s*:\s*\"(nt8_\w+)\"[^{}]*\"arguments\"\s*:\s*(\{[^{}]*\})[^{}]*\}", text):
        try:
            calls.append({"function": {"name": m.group(1), "arguments": json.loads(m.group(2))}})
        except ValueError:
            continue
    return calls


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen3.8:27b")
    ap.add_argument("--host", default=os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"))
    ap.add_argument("--profile", default="core", choices=["core", "full"])
    ap.add_argument("--ask", help="pregunta unica (sin modo interactivo)")
    ap.add_argument("--max-steps", type=int, default=8)
    args = ap.parse_args()
    os.environ["NT8_MCP_PROFILE"] = args.profile

    import httpx
    from nt8_mcp import server as srv

    logging.getLogger("httpx").setLevel(logging.WARNING)

    tools = asyncio.run(srv.mcp.list_tools())
    schema = [{"type": "function", "function": {"name": t.name, "description": (t.description or "")[:600],
                                                "parameters": t.inputSchema}} for t in tools]
    manager = srv.mcp._tool_manager
    host = args.host if args.host.startswith("http") else "http://" + args.host
    messages = [{"role": "system", "content": SYSTEM}]
    print(f"Modelo {args.model} con {len(schema)} herramientas NT8 (solo lectura). Ctrl+C para salir.", file=sys.stderr)

    def turn(question: str) -> str:
        messages.append({"role": "user", "content": question})
        for _ in range(args.max_steps):
            r = httpx.post(f"{host}/api/chat", json={"model": args.model, "messages": messages, "tools": schema,
                                                     "stream": False, "options": {"num_ctx": 32768}}, timeout=600)
            r.raise_for_status()
            msg = r.json()["message"]
            messages.append(msg)
            calls = msg.get("tool_calls") or _calls_in_text(msg.get("content") or "")
            if not calls:
                return msg.get("content", "")
            for call in calls:
                fn = call["function"]
                name, raw = fn["name"], fn.get("arguments") or {}
                if isinstance(raw, str):
                    raw = json.loads(raw or "{}")
                tool = manager.get_tool(name)
                if tool is None:
                    result = f"ERROR: herramienta desconocida {name}"
                else:
                    try:
                        result = tool.fn(**tool.fn_metadata.arg_model.model_validate(raw).model_dump_one_level())
                    except Exception as exc:  # noqa: BLE001
                        result = f"ERROR: {exc}"
                print(f"  -> {name}({json.dumps(raw, ensure_ascii=False)[:120]})", file=sys.stderr)
                messages.append({"role": "tool", "content": str(result)[:12000], "tool_name": name})
        return "(limite de pasos alcanzado)"

    if args.ask:
        print(turn(args.ask))
        return 0
    try:
        while True:
            q = input("\nTu> ").strip()
            if q:
                print("\nIA> " + turn(q))
    except (KeyboardInterrupt, EOFError):
        return 0


if __name__ == "__main__":
    sys.exit(main())
