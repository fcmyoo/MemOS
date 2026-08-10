"""
Suggestion handler for generating suggestion queries.

This module handles suggestion query generation based on user's recent memories
or further suggestions from chat history.
"""

import json

from typing import Any

from memos.api.middleware.auth import AuthContext
from memos.api.product_models import SuggestionResponse
from memos.log import get_logger
from memos.mem_os.utils.format_utils import clean_json_response
from memos.templates.mos_prompts import (
    FURTHER_SUGGESTION_PROMPT,
    SUGGESTION_QUERY_PROMPT_EN,
    SUGGESTION_QUERY_PROMPT_ZH,
)
from memos.types import MessageList, MessagesType


logger = get_logger(__name__)


def _get_further_suggestion(
    llm: Any,
    message: MessageList | str,
) -> list[str]:
    """
    Get further suggestion based on recent dialogue.

    Args:
        llm: LLM instance for generating suggestions
        message: Recent chat messages (can be a list of message dicts or a plain string)

    Returns:
        List of suggestion queries
    """
    try:
        if isinstance(message, str):
            dialogue_info = message
        else:
            dialogue_info = "\n".join(
                [
                    f"{msg['role']}: {msg['content']}"
                    for msg in message[-2:]
                    if isinstance(msg, dict)
                ]
            )
        further_suggestion_prompt = FURTHER_SUGGESTION_PROMPT.format(dialogue=dialogue_info)
        message_list = [
            {
                "role": "system",
                "content": "You are a helpful assistant that generates suggestion queries based on dialogue context.",
            },
            {"role": "user", "content": further_suggestion_prompt},
        ]
        response = llm.generate(message_list)
        clean_response = clean_json_response(response)
        response_json = json.loads(clean_response)
        return response_json["query"]
    except Exception as e:
        logger.error(f"Error getting further suggestion: {e}", exc_info=True)
        return []


def handle_get_suggestion_queries(
    user_id: str,
    language: str,
    message: MessagesType | None,
    llm: Any,
    naive_mem_cube: Any,
    cube_id: str | None = None,
    current_user: AuthContext | None = None,
    access_control: Any = None,
) -> SuggestionResponse:
    """
    Main handler for suggestion queries endpoint.

    Generates suggestion queries based on user's recent memories or chat history.

    Args:
        user_id: User ID
        language: Language preference ("zh" or "en")
        message: Optional chat message list for further suggestions
        llm: LLM instance
        naive_mem_cube: Memory cube instance
        cube_id: Optional memory cube id (suggestion_req.mem_cube_id)
        current_user: Authenticated identity from the router.
        access_control: Shared CubeAccessControl instance.

    Returns:
        SuggestionResponse with generated queries
    """
    # Actor -> cube validation before any side effect (plan 5.3 #12).
    # The router previously passed mem_cube_id as user_id; keep both inputs
    # explicit and resolve the actor before searching the cube.
    target_cube = cube_id or user_id
    if current_user is not None and access_control is not None:
        actor_user_id = access_control.resolve_actor(current_user, user_id)
        access_control.require_cube_access(current_user, actor_user_id, [target_cube])
        user_id = actor_user_id

    try:
        # If message is provided, get further suggestions based on dialogue
        if message:
            suggestions = _get_further_suggestion(llm, message)
            return SuggestionResponse(
                message="Suggestions retrieved successfully",
                data={"query": suggestions},
            )

        # Otherwise, generate suggestions based on recent memories
        if language == "zh":
            suggestion_prompt = SUGGESTION_QUERY_PROMPT_ZH
        else:  # English
            suggestion_prompt = SUGGESTION_QUERY_PROMPT_EN

        # Search for recent memories (scoped by cube, not the raw user id)
        text_mem_results = naive_mem_cube.text_mem.search(
            query="my recently memories",
            user_name=target_cube,
            top_k=3,
            mode="fast",
            info={"user_id": target_cube},
        )

        # Extract memory content
        memories = ""
        if text_mem_results:
            memories = "\n".join([m.memory[:200] for m in text_mem_results])

        # Generate suggestions using LLM
        message_list = [
            {
                "role": "system",
                "content": "You are a helpful assistant that generates suggestion queries based on the user's recent memories.",
            },
            {"role": "user", "content": suggestion_prompt.format(memories=memories)},
        ]
        response = llm.generate(message_list)
        clean_response = clean_json_response(response)
        response_json = json.loads(clean_response)

        return SuggestionResponse(
            message="Suggestions retrieved successfully",
            data={"query": response_json["query"]},
        )

    except Exception as e:
        logger.error(f"Failed to get suggestions: {e}", exc_info=True)
        raise
