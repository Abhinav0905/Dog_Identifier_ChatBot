"""
Multi-provider AI client abstraction.

Supports Anthropic (Claude) and OpenAI with a unified interface.
Select the provider via the MODEL_PROVIDER environment variable:
  MODEL_PROVIDER=claude   (default) — uses claude-sonnet-4-5-20250929
  MODEL_PROVIDER=openai             — uses gpt-4o
"""

from config import ANTHROPIC_API_KEY, OPENAI_API_KEY, MODEL_PROVIDER

_ANTHROPIC_MODEL = "claude-sonnet-4-5-20250929"
_OPENAI_MODEL = "gpt-4o"
_OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"


def is_available() -> bool:
    """Return True if the configured provider has an API key set."""
    if MODEL_PROVIDER == "openai":
        return bool(OPENAI_API_KEY)
    return bool(ANTHROPIC_API_KEY)


def get_model_name() -> str:
    """Return the active model identifier for logging/audit."""
    if MODEL_PROVIDER == "openai":
        return _OPENAI_MODEL
    return _ANTHROPIC_MODEL


def create_chat_completion(
    system_prompt: str,
    messages: list[dict],
    max_tokens: int = 1024,
) -> str:
    """
    Send a text-only chat request to the configured provider.

    Args:
        system_prompt: Provider-agnostic system instruction.
        messages: List of {"role": "user"|"assistant", "content": str} dicts.
        max_tokens: Maximum tokens in the response.

    Returns:
        The model's reply as a plain string.
    """
    if MODEL_PROVIDER == "openai":
        return _openai_chat(system_prompt, messages, max_tokens)
    return _anthropic_chat(system_prompt, messages, max_tokens)


def create_vision_completion(
    system_prompt: str,
    image_b64: str,
    media_type: str,
    user_text: str,
    max_tokens: int = 1024,
) -> str:
    """
    Send an image + text request to the configured provider.

    Args:
        system_prompt: Provider-agnostic system instruction.
        image_b64: Base64-encoded image bytes.
        media_type: MIME type of the image (e.g. "image/jpeg").
        user_text: Text message accompanying the image.
        max_tokens: Maximum tokens in the response.

    Returns:
        The model's reply as a plain string.
    """
    if MODEL_PROVIDER == "openai":
        return _openai_vision(system_prompt, image_b64, media_type, user_text, max_tokens)
    return _anthropic_vision(system_prompt, image_b64, media_type, user_text, max_tokens)


def create_embedding(text: str) -> list[float]:
    """
    Create a text embedding vector.

    Only supported for OpenAI provider (text-embedding-3-small).
    Returns an empty list for Anthropic — use BM25 retrieval instead.
    """
    if MODEL_PROVIDER == "openai":
        return _openai_embedding(text)
    return []


# ---------------------------------------------------------------------------
# Anthropic (Claude) implementations
# ---------------------------------------------------------------------------

def _anthropic_chat(system_prompt: str, messages: list[dict], max_tokens: int) -> str:
    from anthropic import Anthropic

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=_ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=messages,
    )
    return response.content[0].text


def _anthropic_vision(
    system_prompt: str,
    image_b64: str,
    media_type: str,
    user_text: str,
    max_tokens: int,
) -> str:
    from anthropic import Anthropic

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=_ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_b64,
                        },
                    },
                    {"type": "text", "text": user_text},
                ],
            }
        ],
    )
    return response.content[0].text


# ---------------------------------------------------------------------------
# OpenAI implementations
# ---------------------------------------------------------------------------

def _openai_embedding(text: str) -> list[float]:
    from openai import OpenAI

    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.embeddings.create(
        model=_OPENAI_EMBEDDING_MODEL,
        input=text,
    )
    return response.data[0].embedding


def _openai_chat(system_prompt: str, messages: list[dict], max_tokens: int) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=OPENAI_API_KEY)
    oai_messages = [{"role": "system", "content": system_prompt}] + messages
    response = client.chat.completions.create(
        model=_OPENAI_MODEL,
        max_tokens=max_tokens,
        messages=oai_messages,
    )
    return response.choices[0].message.content


def _openai_vision(
    system_prompt: str,
    image_b64: str,
    media_type: str,
    user_text: str,
    max_tokens: int,
) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model=_OPENAI_MODEL,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{image_b64}"},
                    },
                    {"type": "text", "text": user_text},
                ],
            },
        ],
    )
    return response.choices[0].message.content
