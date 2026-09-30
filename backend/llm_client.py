from ollama_client import call_ollama_chat
from nvidia_client import call_nvidia_chat


async def call_llm_chat(messages: list, model_name: str, label: str, provider: str = "ollama", **kwargs):
    """Thin dispatcher: routes to the Ollama (local) or NVIDIA (cloud)
    client based on `provider`, so callers (agent_loop.py, desktop_test.py)
    don't need to know which backend is behind a given request. Both
    clients share the same generator contract: yields heartbeat strings,
    then exactly one final dict {"message": {...} | None, "error": ...}."""
    if provider == "nvidia":
        async for item in call_nvidia_chat(messages, model_name, label, **kwargs):
            yield item
    else:
        async for item in call_ollama_chat(messages, model_name, label, **kwargs):
            yield item
