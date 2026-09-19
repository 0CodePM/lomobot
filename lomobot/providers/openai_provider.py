"""OpenAI provider implementation using official OpenAI SDK."""

import asyncio
import json
import random
import time
import traceback
from typing import Any

from loguru import logger
from openai import (
    APIConnectionError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)

from lomobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

# --- File-based debug log (diagnose intermittent connection errors) ---
_DEBUG_LOG = "/tmp/lomobot_llm_debug.log"

def _dbg(msg: str):
    try:
        with open(_DEBUG_LOG, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


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
        
        # Throttle: minimum gap between LLM requests (avoids hammering proxy/vLLM)
        self._last_request_ts = 0.0
        self._min_request_gap = 1.0
        # Burst throttle: the upstream resets the connection when it sees 3
        # LLM calls within ~20s. Track recent call times; when about to make
        # a 3rd call inside a 20s window, wait until 15s have passed since
        # the last call.
        self._recent_calls = []
        self._burst_window = 20.0   # calls within this span count as one burst
        self._burst_min_gap = 15.0  # required gap after the last call in a burst
        
        # Initialize OpenAI client
        self.client = self._create_client()
        print(f"Initialized OpenAIProvider with base_url={api_base}, default_model={default_model}")

    def _create_client(self):
        """Create a fresh AsyncOpenAI client (new connection pool).

        Recreating the client on connection errors clears any poisoned /
        half-closed (CLOSE-WAIT) connections left in the httpx pool, which
        otherwise get reused and cause repeated 'Connection error' failures.
        """
        return AsyncOpenAI(
            api_key=self.api_key or "not-needed",
            base_url=self.api_base,
        )
    
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
        _total_chars = sum(len(m.get("content") or "") for m in messages)
        _dbg(f"=== CHAT START: msgs={len(messages)} chars={_total_chars} max_tokens={max_tokens} model={model} tools={len(tools) if tools else 0} ===")
        
        # Burst throttle: avoid 3 LLM calls within _burst_window seconds, which
        # makes the upstream reset the connection. If 2 calls already happened in
        # the window, wait until _burst_min_gap seconds have passed since the last.
        _now = time.time()
        self._recent_calls = [t for t in self._recent_calls if _now - t < self._burst_window]
        if len(self._recent_calls) >= 2:
            _last_call = self._recent_calls[-1]
            _wait_until = _last_call + self._burst_min_gap
            if _now < _wait_until:
                _wait = _wait_until - _now
                _dbg(f"  BURST THROTTLE: 2 calls in last {self._burst_window:.0f}s; waiting {_wait:.1f}s (15s after last call)")
                await asyncio.sleep(_wait)
                _now = time.time()
                self._recent_calls = [t for t in self._recent_calls if _now - t < self._burst_window]
        # Simple throttle: if the last request was too recent, wait a random 2-3s
        # before sending. Back-to-back requests keep the proxy/vLLM continuously
        # busy and can make the upstream reset the connection mid-response.
        _gap = _now - self._last_request_ts
        if _gap < self._min_request_gap:
            _wait = random.uniform(2.0, 3.0)
            _dbg(f"  THROTTLE: last request {_gap:.2f}s ago (< {self._min_request_gap}s), waiting {_wait:.2f}s")
            await asyncio.sleep(_wait)
        self._last_request_ts = time.time()
        self._recent_calls.append(time.time())

        # Retry transient errors (connection reset, 5xx, 429, timeout) with backoff.
        # Chat completion requests are idempotent, so retrying is safe.
        # On connection errors we also recreate the client so the next attempt
        # uses a fresh connection pool (clears any poisoned / CLOSE-WAIT conn).
        max_attempts = 5
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            _dbg(f"  ATTEMPT {attempt}/{max_attempts} start")
            _t0 = time.time()
            try:
                response = await self.client.chat.completions.create(**kwargs)
                _dbg(f"  ATTEMPT {attempt}/{max_attempts} SUCCESS in {time.time()-_t0:.2f}s")
                return self._parse_response(response)
            except (APIConnectionError, InternalServerError, RateLimitError) as e:
                # APIConnectionError covers connection reset + APITimeoutError
                last_error = e
                _dbg(f"  ATTEMPT {attempt}/{max_attempts} ERROR in {time.time()-_t0:.2f}s: {type(e).__name__}: {e}")
                _dbg(f"    args: {e.args}")
                # Walk the FULL exception chain to find the root network error
                _chain = []
                _exc = e
                _seen = set()
                while _exc is not None and id(_exc) not in _seen:
                    _seen.add(id(_exc))
                    _chain.append(f"{type(_exc).__name__}({getattr(_exc, 'args', '')})")
                    _exc = _exc.__context__ or _exc.__cause__
                _dbg(f"    chain: {' <- '.join(_chain)}")
                _dbg(f"    traceback:\n{traceback.format_exc()}")
                if attempt < max_attempts:
                    delay = 3  # fixed 3s between retries
                    if isinstance(e, (APIConnectionError, InternalServerError)):
                        # Fresh client => fresh connection pool for next attempt
                        self.client = self._create_client()
                        _dbg(f"    CLIENT RECREATED (fresh pool), retry in {delay}s")
                        logger.warning(
                            f"LLM connection error (attempt {attempt}/{max_attempts}): "
                            f"{type(e).__name__}: {e} — recreating client, retrying in {delay}s"
                        )
                    else:
                        logger.warning(
                            f"LLM transient error (attempt {attempt}/{max_attempts}): "
                            f"{type(e).__name__}: {e} — retrying in {delay}s"
                        )
                    await asyncio.sleep(delay)
                else:
                    _dbg(f"  ALL {max_attempts} ATTEMPTS FAILED. last={type(e).__name__}: {e}")
                    logger.error(
                        f"LLM Provider Error after {max_attempts} attempts: "
                        f"{type(e).__name__}: {e}"
                    )
            except Exception as e:
                # Non-transient error (4xx bad request, auth, etc.) — fail immediately
                logger.error(f"LLM Provider Error: {type(e).__name__}: {e}")
                last_error = e
                break

        # All retries exhausted (or non-transient error)
        assert last_error is not None
        error_msg = str(last_error).lower()
        if "timed out" in error_msg or "timeout" in error_msg:
            return LLMResponse(
                content="⏰ Response timed out. Please try again.",
                finish_reason="error",
                metadata={"debug": f"Timeout: {str(last_error)}"}
            )
        return LLMResponse(
            content="⚠️ Service temporarily unavailable. Please try again later.",
            finish_reason="error",
            metadata={"debug": f"{type(last_error).__name__}: {str(last_error)}"}
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
