"""
Unified model client — supports Claude (Anthropic) and OpenAI with identical interface.

Switch providers by setting PROVIDER=claude or PROVIDER=openai in .env.
The agentic loop in enrichment.py and discovery.py does not change between providers;
only the API format differs. This module handles all format conversion.

Tool definition format:
  Input  (canonical): Claude format — {"name": str, "description": str, "input_schema": {json_schema}}
  OpenAI format:      {"type": "function", "function": {"name": str, "description": str, "parameters": {json_schema}}}

Message format:
  Both providers start with [{"role": "user", "content": "..."}].
  After tool calls the formats diverge — this module handles the divergence transparently.
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class ModelClient:
    """
    Thin adapter over Anthropic and OpenAI SDKs.

    Usage:
        client = ModelClient(provider="openai", api_key=..., model="gpt-5-nano")
        response = client.chat(system="...", messages=[...], tools=[...])
        # response = {"text": str|None, "tool_calls": [{"id", "name", "input"}], "stop_reason": str}

        # Extend the message list for the next iteration:
        messages.extend(client.make_tool_turn(response, tool_results))
    """

    def __init__(self, provider: str, api_key: str, model: str, max_tokens: int = 8096):
        self.provider = provider.lower()
        self.model = model
        self.max_tokens = max_tokens

        if self.provider == "claude":
            import anthropic
            self._client = anthropic.Anthropic(api_key=api_key)
        elif self.provider == "openai":
            from openai import OpenAI
            self._client = OpenAI(api_key=api_key)
        else:
            raise ValueError(f"Unknown provider: {provider!r}. Use 'claude' or 'openai'.")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def chat(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        """
        Send a chat request. Returns a normalized response dict:
          {
            "text": str | None,          # assistant prose (if no tool calls)
            "tool_calls": [              # list of tool calls (may be empty)
              {"id": str, "name": str, "input": dict}
            ],
            "stop_reason": str,          # "tool_use" | "end_turn" | "max_tokens"
            "_raw": <provider response>  # for debugging
          }
        """
        tokens = max_tokens if max_tokens is not None else self.max_tokens
        if self.provider == "claude":
            return self._chat_claude(system, messages, tools, tokens)
        else:
            return self._chat_openai(system, messages, tools, tokens)

    def make_tool_turn(self, response: dict, tool_results: list[dict]) -> list[dict]:
        """
        Build the messages to append after a tool-use response.
        tool_results: [{"tool_call_id": str, "content": str}, ...]

        Returns a list of messages in provider-correct format to extend the messages list.
        """
        if self.provider == "claude":
            return self._make_tool_turn_claude(response, tool_results)
        else:
            return self._make_tool_turn_openai(response, tool_results)

    # ------------------------------------------------------------------
    # Claude implementation
    # ------------------------------------------------------------------

    def _chat_claude(self, system, messages, tools, tokens=None):
        kwargs = dict(
            model=self.model,
            max_tokens=tokens or self.max_tokens,
            system=system,
            messages=messages,
        )
        if tools:
            kwargs["tools"] = tools  # already in Claude format

        raw = self._client.messages.create(**kwargs)

        text = None
        tool_calls = []
        for block in raw.content:
            if block.type == "tool_use":
                tool_calls.append({"id": block.id, "name": block.name, "input": block.input})
            elif block.type == "text" and block.text:
                text = block.text

        stop = raw.stop_reason  # "tool_use" | "end_turn" | "max_tokens"
        return {"text": text, "tool_calls": tool_calls, "stop_reason": stop, "_raw": raw}

    def _make_tool_turn_claude(self, response, tool_results):
        raw = response["_raw"]
        assistant_msg = {"role": "assistant", "content": raw.content}
        tool_msg = {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tr["tool_call_id"],
                    "content": tr["content"],
                }
                for tr in tool_results
            ],
        }
        return [assistant_msg, tool_msg]

    # ------------------------------------------------------------------
    # OpenAI implementation
    # ------------------------------------------------------------------

    def _claude_tools_to_openai(self, tools: list[dict]) -> list[dict]:
        """Convert Claude tool definitions to OpenAI function format."""
        converted = []
        for t in tools:
            converted.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                },
            })
        return converted

    def _chat_openai(self, system, messages, tools, tokens=None):
        oai_messages = [{"role": "system", "content": system}] + messages

        # GPT-5+ uses max_completion_tokens; older models use max_tokens.
        # max_completion_tokens is accepted by all modern OpenAI models.
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_completion_tokens=tokens or self.max_tokens,
            messages=oai_messages,
        )
        if tools:
            kwargs["tools"] = self._claude_tools_to_openai(tools)
            kwargs["tool_choice"] = "auto"

        raw = self._client.chat.completions.create(**kwargs)
        choice = raw.choices[0]
        msg = choice.message

        text = msg.content or None
        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    input_dict = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    input_dict = {}
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "input": input_dict,
                })

        finish = choice.finish_reason  # "tool_calls" | "stop" | "length"
        # Normalize stop reason to Claude-style for consistent caller logic
        stop_map = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens"}
        stop_reason = stop_map.get(finish, finish)

        return {"text": text, "tool_calls": tool_calls, "stop_reason": stop_reason, "_raw": raw, "_oai_msg": msg}

    def _make_tool_turn_openai(self, response, tool_results):
        oai_msg = response["_oai_msg"]
        # Assistant message with tool_calls attached
        assistant_msg = {
            "role": "assistant",
            "content": oai_msg.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in (oai_msg.tool_calls or [])
            ],
        }
        # One tool message per result
        tool_msgs = [
            {"role": "tool", "tool_call_id": tr["tool_call_id"], "content": tr["content"]}
            for tr in tool_results
        ]
        return [assistant_msg] + tool_msgs
