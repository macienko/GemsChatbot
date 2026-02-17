"""OpenAI chatbot with function-calling for inventory search."""

import json
import logging
import os

from openai import OpenAI

logger = logging.getLogger(__name__)

from sheets import search_inventory

SYSTEM_PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompt.md")

SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_inventory",
        "description": (
            "Search the gemstone inventory in Google Sheets. "
            "Provide gemstone type (required) and optional carat weight filters."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "gemstone": {
                    "type": "string",
                    "description": "Gemstone type, e.g. 'emerald', 'ruby', 'sapphire'",
                },
                "caratWeightMin": {
                    "type": "number",
                    "description": "Minimum carat weight to search with",
                },
                "caratWeightMax": {
                    "type": "number",
                    "description": "Maximum carat weight to search with",
                },
                "pair": {
                    "type": "boolean",
                    "description": "Whether to search for pairs (true) or singles (false)",
                },
            },
            "required": ["gemstone"],
        },
    },
}


def _load_system_prompt() -> str:
    with open(SYSTEM_PROMPT_PATH, "r") as f:
        return f.read()


# In-memory conversation store keyed by phone number.
# For production, replace with Redis or a database.
_conversations: dict[str, list[dict]] = {}

MAX_HISTORY = 20  # max message pairs to keep per user


def _get_history(user_id: str) -> list[dict]:
    if user_id not in _conversations:
        _conversations[user_id] = []
    return _conversations[user_id]


def _trim_history(user_id: str) -> None:
    history = _conversations.get(user_id, [])
    # Keep system prompt (first entry is added on the fly) + last N messages
    if len(history) > MAX_HISTORY * 2:
        _conversations[user_id] = history[-(MAX_HISTORY * 2):]


def _is_greeting_only(text: str) -> bool:
    greetings = {"hi", "hello", "hey", "hola", "greetings", "good morning", "good evening", "good afternoon"}
    return text.strip().lower().rstrip("!.,") in greetings


def reset_conversation(user_id: str) -> None:
    """Clear conversation history for a user."""
    _conversations.pop(user_id, None)


def handle_message(user_id: str, user_text: str) -> list[dict]:
    """Process a user message and return a list of response messages.

    Each message dict has: body, image, video
    For items with video, we split into two messages:
      1. Video-only message
      2. Text + image message
    """
    logger.info("Received message from %s: %s", user_id, user_text)

    # Reset on greeting-only messages
    if _is_greeting_only(user_text):
        reset_conversation(user_id)

    history = _get_history(user_id)
    history.append({"role": "user", "content": user_text})

    system_prompt = _load_system_prompt()
    messages = [{"role": "system", "content": system_prompt}] + history

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    # Loop to handle function calls
    while True:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            tools=[SEARCH_TOOL],
            temperature=0.3,
        )

        choice = response.choices[0]

        if choice.finish_reason == "tool_calls":
            # Process each tool call
            assistant_msg = choice.message
            messages.append(assistant_msg)

            for tool_call in assistant_msg.tool_calls:
                args = json.loads(tool_call.function.arguments)
                logger.info("Tool call search_inventory: %s", args)
                results = search_inventory(
                    gemstone=args["gemstone"],
                    carat_weight_min=args.get("caratWeightMin"),
                    carat_weight_max=args.get("caratWeightMax"),
                    pair=args.get("pair", False),
                )
                logger.info("Search returned %d results", len(results))
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(results),
                })
            # Continue the loop so the model can process results
            continue

        # Normal text response - done
        assistant_content = choice.message.content or ""
        logger.info("AI response for %s: %s", user_id, assistant_content)
        history.append({"role": "assistant", "content": assistant_content})
        _trim_history(user_id)

        return _parse_response(assistant_content)


def _parse_response(raw: str) -> list[dict]:
    """Parse the JSON response from the model into message dicts.

    Splits each item into separate messages:
      1. Video message (if video URL exists)
      2. Text + image message
    """
    # Strip markdown code fences if present
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json) and last line (```)
        lines = [l for l in lines[1:] if l.strip() != "```"]
        text = "\n".join(lines)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # If model didn't return valid JSON, wrap as plain text
        return [{"body": raw, "image": "", "video": ""}]

    raw_messages = data.get("messages", [])
    output = []

    for msg in raw_messages:
        body = msg.get("body", "")
        image = msg.get("image", "")
        video = msg.get("video", "")

        if video:
            # Send video as its own message first
            output.append({"body": "", "image": "", "video": video})
            # Then send text + image
            output.append({"body": body, "image": image, "video": ""})
        else:
            output.append({"body": body, "image": image, "video": ""})

    return output
