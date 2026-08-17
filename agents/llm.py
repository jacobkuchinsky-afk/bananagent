"""Drives Stagehand from a local Ollama model.

Stagehand will take a plain async callback in place of a hosted provider, which
is the whole reason this works without an API key: it hands us messages and a
JSON schema, we hand back an answer, and it never learns the model isn't
OpenAI's. Nothing leaves the machine.

Two shapes come through. Structured calls carry a JSON schema and are how
Stagehand does its real work — deciding which element to click, pulling fields
off a page — so those go to Ollama with `format` set to the schema, which
constrains decoding rather than hoping the model returns valid JSON. Plain
message calls are the fallback and come back as text.

The agent's model runs on the *system* Ollama on 11434, not the pinned 0.20.7
on 11435. That copy exists only because old Ollama can still hear speech; it is
two years behind on everything else, and a 27B tool-caller has no business on it.
"""

import json

import ollama

import config

_client = None
_resolved = None


def client():
    global _client
    if _client is None:
        _client = ollama.AsyncClient(host=config.AGENT_OLLAMA_HOST)
    return _client


def resolve_model():
    """The configured model if it's pulled, else the closest thing installed.

    Same bargain brain.py makes: a half-finished `ollama pull` should leave the
    agent slower, not dead.
    """
    global _resolved
    if _resolved:
        return _resolved

    want = config.AGENT_MODEL
    try:
        listed = ollama.Client(host=config.AGENT_OLLAMA_HOST).list().get("models", [])
        names = [m.get("model") or m.get("name") for m in listed]
        names = [n for n in names if n]
    except Exception as e:
        raise RuntimeError(
            f"The agent's Ollama ({config.AGENT_OLLAMA_HOST}) isn't reachable: {e}")

    if want in names:
        _resolved = want
        return _resolved

    # Never fall back onto a hosted model. The point of this agent is that the
    # pages it reads — forms, accounts, whatever you're logged into — stay on
    # this machine, and a ':...-cloud' tag would quietly ship them off it.
    local = [n for n in names if not n.endswith("-cloud")]
    stem = want.split(":")[0]
    family = stem.split(".")[0]
    pick = (next((n for n in local if n.startswith(stem)), None)
            or next((n for n in local if n.startswith(family)), None))
    if pick is None:
        raise RuntimeError(
            f"'{want}' isn't pulled and no local substitute is installed. "
            f"Run: ollama pull {want}")
    print(f"[browser] '{want}' isn't pulled — driving with local '{pick}' instead")
    _resolved = pick
    return _resolved


def available():
    """Is there a model to drive with? Used for the dashboard's status line."""
    try:
        return resolve_model()
    except Exception:
        return None


# -- translation -------------------------------------------------------------
def _blocks(content):
    return content if isinstance(content, list) else [content]


def _to_ollama(messages, system_prompt):
    """Stagehand's message blocks to Ollama's, images included."""
    out = []
    if system_prompt:
        out.append({"role": "system", "content": system_prompt})
    for message in messages:
        text, images = [], []
        for block in _blocks(message.content):
            kind = getattr(block, "type", None)
            if kind == "image":
                # Ollama carries media in `images` — base64 without a data: prefix.
                data = getattr(block, "data", "") or ""
                images.append(data.split(",", 1)[-1] if data.startswith("data:") else data)
            else:
                piece = getattr(block, "text", None)
                if piece:
                    text.append(piece)
        turn = {"role": str(getattr(message.role, "value", message.role)),
                "content": "\n".join(text)}
        if images:
            turn["images"] = images
        out.append(turn)
    return out


def _options(params):
    options = dict(config.AGENT_OPTIONS)
    if getattr(params, "temperature", None) is not None:
        options["temperature"] = params.temperature
    if getattr(params, "stop_sequences", None):
        options["stop"] = list(params.stop_sequences)
    return options


def _schema_of(params):
    """The JSON schema Stagehand wants the answer to match, if this is a structured call."""
    fmt = getattr(params, "response_format", None)
    if fmt is None:
        return None
    schema = getattr(fmt, "json_schema", None) or getattr(fmt, "schema_", None)
    if schema is None and isinstance(fmt, dict):
        schema = fmt.get("json_schema") or fmt.get("schema")
    if schema is None:
        return None
    # It arrives as a pydantic model or a dict depending on the call.
    if hasattr(schema, "model_dump"):
        schema = schema.model_dump(exclude_none=True, by_alias=True)
    if isinstance(schema, dict) and "schema" in schema and "type" not in schema:
        schema = schema["schema"]
    return schema


def _tools_of(params):
    tools = getattr(params, "tools", None)
    if not tools:
        return None
    out = []
    for tool in tools:
        schema = getattr(tool, "input_schema", None)
        if hasattr(schema, "model_dump"):
            schema = schema.model_dump(exclude_none=True, by_alias=True)
        out.append({"type": "function", "function": {
            "name": tool.name,
            "description": getattr(tool, "description", "") or "",
            "parameters": schema or {"type": "object", "properties": {}},
        }})
    return out


def _usage(response):
    from stagehand._generated.models import LLMUsage

    prompt = int(response.get("prompt_eval_count") or 0)
    completion = int(response.get("eval_count") or 0)
    return LLMUsage(input_tokens=prompt, output_tokens=completion,
                    total_tokens=prompt + completion)


def _strip_thinking(text):
    """Qwen emits <think>…</think> when it feels like it; JSON must not contain it."""
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[-1]
    return text.strip()


async def generate(params):
    """The callback Stagehand calls. One request in, one answer out."""
    from stagehand._generated.models import (
        LLMMessageGenerateResult,
        LLMRole,
        LLMStructuredGenerateResult,
        LLMTextContent,
    )

    schema = _schema_of(params)
    structured = schema is not None and getattr(params, "output_format", None) != "text"

    request = {
        "model": resolve_model(),
        "messages": _to_ollama(params.messages, getattr(params, "system_prompt", None)),
        "options": _options(params),
        "keep_alive": config.AGENT_KEEP_ALIVE,
        "think": config.AGENT_THINK,
    }
    if structured:
        request["format"] = schema
    tools = _tools_of(params)
    if tools:
        request["tools"] = tools

    response = await client().chat(**request)
    message = response.get("message", {}) or {}
    text = _strip_thinking(message.get("content", "") or "")

    if structured:
        try:
            parsed = json.loads(text) if text else {}
        except json.JSONDecodeError:
            # Constrained decoding makes this rare, but a model that ignores the
            # grammar shouldn't take the whole run down with it.
            start, end = text.find("{"), text.rfind("}")
            parsed = json.loads(text[start:end + 1]) if start >= 0 < end else {}
        return LLMStructuredGenerateResult(
            role=LLMRole.assistant,
            content=[LLMTextContent(type="text", text=text)],
            output_format="json_schema",
            structured_content=parsed,
            stop_reason=response.get("done_reason") or "stop",
            usage=_usage(response),
        )

    return LLMMessageGenerateResult(
        role=LLMRole.assistant,
        content=[LLMTextContent(type="text", text=text)],
        output_format="text",
        stop_reason=response.get("done_reason") or "stop",
        usage=_usage(response),
    )
