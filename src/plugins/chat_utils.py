"""Compatibility facade for shared chat, search, and market services.

New code should import :mod:`src.services`; historical plugin imports stay stable.
"""

from __future__ import annotations

from src.services import register_shutdown_hook
from src.services.config import bounded_env_float, bounded_env_int
from src.services.llm import (
    MAX_INPUT_CHARS,
    chat_service,
    enforce_persona_contract,
    generate_reply,
    get_claude,
    system_prompt,
    try_anthropic,
    validate_fact_citations,
)
from src.services.market import (
    st_get as st_get,
)
from src.services.market import (
    st_post as st_post,
)
from src.state.persona_memory_repository import PersonaScope

_bounded_env_int = bounded_env_int
_bounded_env_float = bounded_env_float
_MAX_INPUT_CHARS = MAX_INPUT_CHARS

_system_prompt = system_prompt
_enforce_persona_contract = enforce_persona_contract
_validate_fact_citations = validate_fact_citations
_get_claude = get_claude
_generate_reply = generate_reply


async def _try_anthropic(
    prompt: str,
    messages: list[dict[str, str]],
    max_tokens: int,
) -> str:
    return await try_anthropic(
        prompt,
        messages,
        max_tokens,
        client_factory=_get_claude,
    )


async def deepseek_chat(
    message: str,
    session_id: str = "",
    *,
    preverified_context: bool = False,
    persona_scope: PersonaScope | None = None,
    tool_actions: list[object] | None = None,
) -> str:
    # Resolve the compatibility store at call time so existing integrations
    # that inject a repository through this module continue to work.
    from .session_store import store

    async def compatible_generator(text: str, key: str) -> str:
        # Keep the historical patch point for integrations/tests. Production
        # calls use the selected repository and propagate the trust flag.
        if _generate_reply is not generate_reply:
            return await _generate_reply(text, key)
        return await chat_service.generate_reply(
            text,
            key,
            session_store=store,
            preverified_context=preverified_context,
            persona_scope=persona_scope,
            tool_actions=tool_actions,
        )

    return await chat_service.chat(
        message,
        session_id,
        session_store=store,
        generate_reply=compatible_generator,
        preverified_context=preverified_context,
        persona_scope=persona_scope,
        tool_actions=tool_actions,
    )


def _register_shutdown_hook() -> bool:
    return register_shutdown_hook()


_register_shutdown_hook()
