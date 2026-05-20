"""OpenAI provider implementation using official OpenAI SDK."""

import json
from typing import Any

from openai import AsyncOpenAI

from lomobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class OpenAIProvider(LLMProvider):
    """
    LLM provider using the official OpenAI Python SDK.
    
    Supports OpenAI, OpenRouter, and any OpenAI-compatible API 
    (e.g., Ollama, vLLM, LM Studio) through a unified interface.
    """
    
    def __init__(
        self, 
        api_key: str | None = None, 
        api_base: str | None = None,
        default_model: str = "qwen3.5:4b"
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        
        # Initialize OpenAI client
        self.client = AsyncOpenAI(
            api_key=api_key or "not-needed",
            base_url=api_base,
        )
        print(f"Initialized OpenAIProvider with base_url={api_base}, default_model={default_model}")
    
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 32768,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """
        Send a chat completion request via OpenAI SDK.
        
        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions in OpenAI format.
            model: Model identifier (e.g., 'gpt-4', 'llama3.1').
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.
        
        Returns:
            LLMResponse with content and/or tool calls.
        """
        model = model or self.default_model
        
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        
        
        print(f"OpenAIProvider sending request with model={model}, tools={len(tools) if tools else 0}, max_tokens={max_tokens}, temperature={temperature}")
        #print(f"Sending request to OpenAI: {kwargs}")

        try:
            response = await self.client.chat.completions.create(**kwargs)
            return self._parse_response(response)
        except Exception as e:
            # Log full error for debugging
            logger.error(f"LLM Provider Error: {type(e).__name__}: {e}")
            error_msg = str(e).lower()
            if "timed out" in error_msg or "timeout" in error_msg:
                return LLMResponse(
                    content="⏰ Response timed out. Please try again.",
                    finish_reason="error",
                    metadata={"debug": f"Timeout: {str(e)}"}
                )
            return LLMResponse(
                content="⚠️ Service temporarily unavailable. Please try again later.",
                finish_reason="error",
                metadata={"debug": f"{type(e).__name__}: {str(e)}"}
            )
    
    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse OpenAI SDK response into our standard format."""
        choice = response.choices[0]
        message = choice.message
        
        tool_calls = []
        if message.tool_calls:
            for tc in message.tool_calls:
                args = tc.function.arguments
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"raw": args}
                
                tool_calls.append(ToolCallRequest(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                ))
        
        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        
        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
        )
    
    def get_default_model(self) -> str:
        """Get the default model."""
        return self.default_model
