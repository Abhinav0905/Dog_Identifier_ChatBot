"""Compatibility facade for the centralized Ask Dorjee prompt catalog.

Runtime prompt text lives in :mod:`services.prompts`. Existing imports remain
stable so older callers and tests do not need to change during this refactor.
"""

from services.prompts import PromptCatalog


# Previous import-time snapshot retained for reference:
# POLICY = PromptCatalog.SHARED_POLICY


def __getattr__(name):
    if name == "POLICY":
        return PromptCatalog.SHARED_POLICY
    raise AttributeError(name)


def shared_policy(language: str = "en") -> str:
    return PromptCatalog.shared_policy(language)


def final_response_contract(language: str = "en") -> str:
    return PromptCatalog.final_response_contract(language)
