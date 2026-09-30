import asyncio
import json
import os
import time
import httpx

NVIDIA_API_BASE = "https://integrate.api.nvidia.com/v1/chat/completions"


def _to_openai_tool_result_message(tool_call_id: str, content: str) -> dict:
    """Shape of a tool-result message NVIDIA's OpenAI-compatible API expects
    — keyed by tool_call_id (correlates to a specific call when several were
    requested in the same turn), unlike Ollama's simpler tool_name-keyed
    message."""
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


async def call_nvidia_chat(messages: list, model_name: str, label: str,
                             tools: list = None, timeout: float = 120.0,
                             heartbeat_interval: float = 4.0, **_ignored):
    """Same contract as ollama_client.call_ollama_chat: async generator that
    yields heartbeat strings while waiting, then exactly one final dict
    {"message": {...} | None, "error": str | None}. `message` is normalized
    to the same shape callers already expect (tool_calls with a parsed
    `arguments` dict, not the raw JSON string OpenAI's API returns it as) so
    agent_loop.py doesn't need to know which provider answered.

    Requires NVIDIA_API_KEY to be set as a real OS environment variable —
    never read from settings/config (which is reflected back in
    GET /api/config) and never written to disk by this project."""
    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key:
        yield {"message": None, "error": "NVIDIA_API_KEY no está configurada como variable de entorno."}
        return

    async def do_call():
        payload = {"model": model_name, "messages": messages}
        if tools:
            payload["tools"] = tools
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(NVIDIA_API_BASE, json=payload, headers=headers)
            if resp.status_code != 200:
                return {"message": None, "error": f"NVIDIA API respondió HTTP {resp.status_code}: {resp.text[:300]}"}
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                return {"message": None, "error": "NVIDIA API no devolvió 'choices' en la respuesta."}
            message = choices[0].get("message", {}) or {}

            # Normalize tool_calls into the same shape ollama_client already
            # produces: [{"id": ..., "function": {"name": ..., "arguments": <dict>}}]
            # — OpenAI's format gives `arguments` as a JSON *string*, Ollama's
            # gives it already parsed, so this is where that difference gets
            # absorbed once, instead of leaking into agent_loop.py.
            raw_tool_calls = message.get("tool_calls") or []
            normalized_calls = []
            for call in raw_tool_calls:
                fn = call.get("function", {}) or {}
                raw_args = fn.get("arguments", "{}")
                try:
                    parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except (json.JSONDecodeError, ValueError):
                    parsed_args = {}
                normalized_calls.append({
                    "id": call.get("id"),
                    "type": call.get("type", "function"),
                    "function": {"name": fn.get("name"), "arguments": parsed_args}
                })
            if normalized_calls:
                message = {**message, "tool_calls": normalized_calls}
            return {"message": message, "error": None}

    async def attempt():
        task = asyncio.create_task(do_call())
        start_time = time.monotonic()
        while not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=heartbeat_interval)
            except asyncio.TimeoutError:
                elapsed = int(time.monotonic() - start_time)
                yield f"__HB__:{label}... ({elapsed}s transcurridos)\n"
            except Exception:
                break
        try:
            result = await task
        except Exception as e:
            err_detail = f"{type(e).__name__}: {e}" if str(e) else f"{type(e).__name__} (sin mensaje adicional)"
            result = {"message": None, "error": err_detail}
        yield result

    result = None
    async for item in attempt():
        if isinstance(item, dict):
            result = item
        else:
            yield item

    if result and result.get("error"):
        # Un reintento: errores de red/HTTP transitorios son razonables aqui
        # tambien, igual que en ollama_client.call_ollama_chat.
        yield f"__HB__:Error en {label}, reintentando una vez...\n"
        await asyncio.sleep(2)
        async for item in attempt():
            if isinstance(item, dict):
                result = item
            else:
                yield item

    yield result
