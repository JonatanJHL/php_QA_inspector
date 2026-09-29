import asyncio
import json
import re
import time
import httpx

from config import settings, DEFAULT_NUM_CTX


def _iter_balanced_braces(text: str):
    """Yield each top-level balanced {...} substring found in text, scanning
    left to right. Last-resort recovery for a tool-call JSON object the model
    wrapped in explanatory prose with no fence or tag at all around it."""
    i, n = 0, len(text)
    while i < n:
        if text[i] == '{':
            depth = 0
            start = i
            matched = False
            for j in range(i, n):
                if text[j] == '{':
                    depth += 1
                elif text[j] == '}':
                    depth -= 1
                    if depth == 0:
                        yield text[start:j + 1]
                        i = j
                        matched = True
                        break
            if not matched:
                break
        i += 1


def _extract_fallback_tool_call(content: str):
    """Ollama's own tool_calls parser depends on the model wrapping its call
    in the exact `<tool_call>...</tool_call>` tags its template expects (see
    `ollama show <model> --modelfile`). Confirmed via testing (2026-07-25)
    that qwen2.5-coder:7b and hermes3 don't reliably do this on this
    hardware/quantization — the raw JSON comes back bare, inside a ```json
    fence, or even mixed with explanatory prose before/after it ("Mis
    disculpas... {\"name\": ...}"), so Ollama silently treats it as plain
    content instead of a tool call. This tries increasingly permissive
    strategies to recover the call from the text. Returns Ollama's own
    tool_calls list shape, or None."""
    if not content or not content.strip():
        return None

    candidates = []
    tag_match = re.search(r'<tool_call>([\s\S]*?)</tool_call>', content)
    if tag_match:
        candidates.append(tag_match.group(1).strip())
    fence_match = re.search(r'```(?:json)?\s*\n?([\s\S]*?)```', content)
    if fence_match:
        candidates.append(fence_match.group(1).strip())
    candidates.append(content.strip())
    candidates.extend(_iter_balanced_braces(content))

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict) and "name" in parsed:
            return [{"function": {"name": parsed.get("name"), "arguments": parsed.get("arguments", {}) or {}}}]
    return None


async def call_ollama_chat(messages: list, model_name: str, label: str,
                             tools: list = None, timeout: float = 300.0,
                             num_ctx: int = DEFAULT_NUM_CTX, heartbeat_interval: float = 4.0):
    """Shared Ollama /api/chat caller (non-streaming per call). Async
    generator: yields a short heartbeat string every `heartbeat_interval`
    seconds while the model is still thinking (CPU/iGPU inference on this
    hardware can take minutes per call), and yields exactly one dict as its
    final item: {"message": {...} | None, "error": str | None}. Callers must
    distinguish plain strings (forward as progress) from the dict (the real
    result).

    Pass `tools` (Ollama tool-calling schema) to let the model request tool
    calls — the returned `message` may then contain a `tool_calls` list
    instead of (or alongside) `content`.

    Retries once automatically on failure: confirmed empirically (2026-07-24)
    that Ollama's backend (llama-server) can crash mid-request on this
    hardware's Vulkan/iGPU backend and auto-restarts itself within seconds —
    most failures here are transient, not permanent, so losing the whole
    call outright would throw away otherwise-recoverable work."""
    async def do_call():
        payload = {
            "model": model_name,
            "messages": messages,
            "stream": False,
            # keep_alive="10m": evita que Ollama descargue el modelo de RAM
            # entre esta llamada y las siguientes (confirmado con `ollama ps`
            # que el default expira en ~1 minuto de inactividad).
            "keep_alive": "10m",
            "options": {"temperature": 0.2, "num_ctx": num_ctx}
        }
        if tools:
            payload["tools"] = tools

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{settings.ollama_url}/api/chat", json=payload)
            if resp.status_code != 200:
                return {"message": None, "error": f"Ollama respondió HTTP {resp.status_code}"}
            data = resp.json()
            message = data.get("message", {})
            if tools and not message.get("tool_calls"):
                fallback = _extract_fallback_tool_call(message.get("content", ""))
                if fallback:
                    message["tool_calls"] = fallback
                    message["content"] = ""  # ya extraído; no mostrarlo como "pensamiento" en crudo
            return {"message": message, "error": None}

    async def attempt():
        task = asyncio.create_task(do_call())
        start_time = time.monotonic()

        while not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=heartbeat_interval)
            except asyncio.TimeoutError:
                elapsed = int(time.monotonic() - start_time)
                # Special prefix (never appended to the markdown body): the
                # frontend intercepts lines starting with this marker and
                # uses them to update a SINGLE status indicator in place.
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
        yield f"__HB__:Error en {label}, reintentando una vez...\n"
        await asyncio.sleep(3)
        async for item in attempt():
            if isinstance(item, dict):
                result = item
            else:
                yield item

    yield result
