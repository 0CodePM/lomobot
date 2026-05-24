"""Agent loop: the core processing engine."""

import asyncio
import json
from pathlib import Path
from typing import Any

from loguru import logger

from lomobot.bus.events import InboundMessage, OutboundMessage
from lomobot.bus.queue import MessageBus
from lomobot.providers.base import LLMProvider
from lomobot.agent.context import ContextBuilder
from lomobot.agent.tools.registry import ToolRegistry
from lomobot.agent.tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool, ListDirTool
from lomobot.agent.tools.shell import ExecTool
from lomobot.agent.tools.web import WebSearchTool, WebFetchTool
from lomobot.agent.tools.message import MessageTool
from lomobot.session.manager import SessionManager


class AgentLoop:
    """
    The agent loop is the core processing engine.
    
    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """
    
    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_tokens: int = 32768,
        temperature: float = 0.7,
        max_tool_iterations: int = 20,
        brave_api_key: str | None = None,
        debug_level: int = 0
    ):
        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_iterations = max_tool_iterations
        self.brave_api_key = brave_api_key
        self.debug_level = debug_level
        self.context = ContextBuilder(workspace)
        self.sessions = SessionManager(workspace)
        self.tools = ToolRegistry()
        
        self._debug_callback = None  # async callback(chat_id, msg) for debug messages
        self._typing_start = None  # callable(chat_id) to start typing
        self._typing_stop = None   # callable(chat_id) to stop typing
        self._running = False
        self._register_default_tools()

    async def _debug(self, type_: str, msg: str):
        """Send typed debug message with icon."""
        icons = {
            "ERROR": "🔴",
            "CALL": "📡",
            "TOOL": "🔧",
            "RESULT": "✅",
            "THINK": "🧠",
            "MEMORY": "📝",
            "FINAL": "📨",
            "SESSION": "💾",
            "CONTEXT": "📊",
        }
        icon = icons.get(type_, "🔧")
        debug_msg = f"{icon} {type_}: {msg}"
        print(f"{debug_msg}")
        if self._debug_callback:
            try:
                await self._debug_callback(debug_msg)
            except Exception as e:
                logger.debug(f"Debug callback error: {e}")

    def set_debug_callback(self, callback):
        """Set callback for sending debug messages to user."""
        self._debug_callback = callback
    
    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        # File tools
        self.tools.register(ReadFileTool())
        self.tools.register(WriteFileTool())
        self.tools.register(EditFileTool())
        self.tools.register(ListDirTool())
        
        # Shell tool
        self.tools.register(ExecTool(working_dir=str(self.workspace)))
        
        # Web tools
        self.tools.register(WebSearchTool(api_key=self.brave_api_key))
        self.tools.register(WebFetchTool())
        
        # Message tool
        message_tool = MessageTool(send_callback=self.bus.publish_outbound)
        self.tools.register(message_tool)
    
    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus."""
        self._running = True
        logger.info("Agent loop started")
        
        while self._running:
            try:
                # Wait for next message
                msg = await asyncio.wait_for(
                    self.bus.consume_inbound(),
                    timeout=1.0
                )
                
                # Process it
                try:
                    response = await self._process_message(msg)
                    if response:
                        await self.bus.publish_outbound(response)
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
                    # Send error response

                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=f"Sorry, I encountered an error: {str(e)}",
                        msg_type="error"
                    ))
            except asyncio.TimeoutError:
                continue
    
    def stop(self) -> None:
        """Stop the agent loop."""
        self._debug_callback = None  # async callback(chat_id, msg) for debug messages

        self._running = False
        logger.info("Agent loop stopping")
    
    async def _process_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a single inbound message.
        
        Args:
            msg: The inbound message to process.
        
        Returns:
            The response message, or None if no response needed.
        """
        logger.info(f"Processing message from {msg.channel}:{msg.sender_id}")

        # Start typing indicator
        if self._typing_start:
            try:
                self._typing_start(msg.chat_id)
            except Exception:
                pass

        # Set debug callback for this message (Telegram only)
        if msg.channel == "telegram":
            async def debug_to_telegram(text: str):
                debug_msg = f"{text}"
                await self.bus.publish_outbound(OutboundMessage(
                    channel="telegram",
                    chat_id=msg.chat_id,
                    msg_type="debug",
                    content=debug_msg
                ))
            self._debug_callback = debug_to_telegram
        else:
            self._debug_callback = None
        
        # Get or create session
        session = self.sessions.get_or_create(msg.session_key)
        await self._debug("SESSION", msg.session_key)
        
        # Update message tool context
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(msg.channel, msg.chat_id)
        
        # Build initial messages (use get_history for LLM-formatted messages)
        messages = self.context.build_messages(
            history=session.get_history(),
            current_message=msg.content
        )
        
        if self.debug_level >= 5:
            from lomobot.channels.telegram import _strip_md_block
            lines = [f"Context: {len(messages)} messages"]
            for i, m in enumerate(messages, 1):
                role = m.get('role', '?')
                raw = str(m.get('content', ''))
                clean = _strip_md_block(raw).replace('\n', ' ').strip()[:60]
                if len(clean) > 60:
                    clean += '...'
                lines.append(f"  {i}. [{role}] {clean}")
            await self._debug("MEMORY", "\n".join(lines))

        # Agent loop
        iteration = 0
        final_content = None
        
        while iteration < self.max_iterations:
            iteration += 1
            
            # Debug: calling LLM
            # Context info
            msg_count = len(messages)
            await self._debug("CONTEXT", f"{msg_count} messages")

            await self._debug("CALL", f"{self.model} (iter {iteration})")

            # Call LLM
            try: 
                response = await self.provider.chat(
                    messages=messages,
                    tools=self.tools.get_definitions(),
                    model=self.model,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature
                )
            except Exception as e:
                
                await self._debug("ERROR", f"LLM call failed: {e}")
                logger.error(f"LLM call error: {e}")
            
            logger.info(f"LLM response (iteration {iteration}): {response.content}")

            # Send debug message if error with debug metadata
            if response.finish_reason == "error" and response.metadata.get("debug"):
                debug_info = response.metadata["debug"]
                await self._debug("ERROR", debug_info)

            if self.debug_level >= 5:
                debug_response = response.content.replace('\n', ' ').strip()[:200]
                if len(response.content) > 200:
                    debug_response += '...'
                await self._debug("RESPONSE", f"{debug_response} (finish_reason: {response.finish_reason})")


            # Handle tool calls
            if response.has_tool_calls:
                # Add assistant message with tool calls
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments)  # Must be JSON string
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts
                )
                
                # Execute tools
                for tool_call in response.tool_calls:
                    logger.debug(f"Executing tool: {tool_call.name}")
                    await self._debug("TOOL", f"{tool_call.name}({json.dumps(tool_call.arguments, ensure_ascii=False)})")
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    result_preview = str(result)[:100] + ("..." if len(str(result)) > 100 else "")
                    await self._debug("RESULT", f"{tool_call.name} → {result_preview}")
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                # No tool calls, we're done
                final_content = response.content
                break
        
        if final_content is None:
            final_content = "I've completed processing but have no response to give."
        
        # Debug final response
        await self._debug("FINAL", f"{len(final_content)} chars")

        # Save to session
        session.add_message("user", msg.content)
        session.add_message("assistant", final_content)
        self.sessions.save(session)
        

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            msg_type="reply"
        )
    
    async def process_direct(self, content: str, session_key: str = "cli:direct") -> str:
        """
        Process a message directly (for CLI usage).
        
        Args:
            content: The message content.
            session_key: Session identifier.
        
        Returns:
            The agent's response.
        """
        msg = InboundMessage(
            channel="cli",
            sender_id="user",
            chat_id="direct",
            content=content
        )
        
        response = await self._process_message(msg)
        return response.content if response else ""
