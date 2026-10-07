"""Per-user agent runtime: a remote-browser connection, a Deep Agent wired to it, and pause / stop controls."""
from __future__ import annotations

import asyncio
import os
from typing import AsyncIterator, Callable, Dict, Optional

from langchain_core.messages import HumanMessage

from agentcore.agent.connection import ConnectionSource
from agentcore.agent.events import events_from
from agentcore.agent.gate import Gate
from agentcore.agent.mcp_session import ConnectError, McpBrowser
from agentcore.agent.prompts import BROWSER_PROMPT, MAIN_PROMPT
from agentcore.agent.tools import make_look_tool


class UserRuntime:
    def __init__(self, user: str, mcp: McpBrowser, gate: Gate):
        self.user, self.mcp, self.gate = user, mcp, gate
        self.agent = None
        self.agent_for = None          # ws_url the current agent graph was built for
        self.run: Optional[asyncio.Task] = None
        self.lock = asyncio.Lock()


class AgentService:
    def __init__(self, source: ConnectionSource, main_model: str = "gpt-6.1-sol", vision_model: str = "gpt-6.1-sol",
                 checkpointer=None, agent_builder: Optional[Callable] = None, mcp_factory: Optional[Callable] = None):
        self.source = source
        self.main_model, self.vision_model = main_model, vision_model
        self.saver = checkpointer
        self._builder = agent_builder or self._default_builder
        self._mcp_factory = mcp_factory or (lambda user, gate: McpBrowser(user, source, gate))
        self.rts: Dict[str, UserRuntime] = {}

    def runtime(self, user: str) -> UserRuntime:
        if user not in self.rts:
            gate = Gate()
            self.rts[user] = UserRuntime(user, self._mcp_factory(user, gate), gate)
        return self.rts[user]

    # -------------------------------------------------------------------------------- connection
    async def ensure_connected(self, user: str) -> UserRuntime:
        """Connect (or reconnect) to the user's CURRENT remote browser. A new AgentCore session means a new address."""
        rt = self.runtime(user)
        ws_url, headers = await asyncio.to_thread(self.source.get, user)
        if not rt.mcp.alive or rt.mcp.ws_url != ws_url:
            await rt.mcp.stop()
            await rt.mcp.start((ws_url, headers))
            rt.agent = None
        return rt

    async def disconnect(self, user: str) -> None:
        """The browser session was released: drop the MCP process so nothing keeps a dead connection."""
        rt = self.rts.get(user)
        if rt:
            await self.stop(user)
            await rt.mcp.stop()
            rt.agent = None

    # ------------------------------------------------------------------------------------ agent
    def _default_builder(self, rt: UserRuntime):
        from deepagents import create_deep_agent
        from langchain.chat_models import init_chat_model
        look = make_look_tool(rt.mcp, lambda: init_chat_model(f"openai:{self.vision_model}"))
        return create_deep_agent(
            model=init_chat_model(f"openai:{self.main_model}"),
            system_prompt=MAIN_PROMPT,
            subagents=[{"name": "browser",
                        "description": "Operates the web browser: opens sites, reads pages, clicks, types, extracts information. "
                                       "Give it a complete task and say what to return.",
                        "system_prompt": BROWSER_PROMPT, "tools": [*rt.mcp.tools, look]}],
            checkpointer=self.saver)

    async def chat(self, user: str, message: str, thread_id: str) -> AsyncIterator[dict]:
        rt = self.runtime(user)
        if rt.lock.locked():
            yield {"type": "error", "text": "the agent is already working on a task for this user"}
            yield {"type": "done"}
            return
        async with rt.lock:
            queue: asyncio.Queue = asyncio.Queue()
            rt.gate.resume()
            rt.run = asyncio.create_task(self._run(rt, message, thread_id, queue))
            try:
                while True:
                    ev = await queue.get()
                    yield ev
                    if ev["type"] in ("done", "stopped"):
                        break
            finally:                                           # the client went away: stop driving the browser
                if rt.run and not rt.run.done():
                    rt.run.cancel()

    async def _run(self, rt: UserRuntime, message: str, thread_id: str, queue: asyncio.Queue) -> None:
        try:
            await self.ensure_connected(rt.user)
            if rt.agent is None:
                rt.agent = self._builder(rt)
            cfg = {"configurable": {"thread_id": f"{rt.user}:{thread_id}"}, "recursion_limit": 150}
            async for ns, chunk in rt.agent.astream({"messages": [HumanMessage(content=message)]}, cfg,
                                                    stream_mode="updates", subgraphs=True):
                for ev in events_from(ns, chunk):
                    await queue.put(ev)
            await queue.put({"type": "done"})
        except asyncio.CancelledError:
            await queue.put({"type": "stopped"})
            raise
        except ConnectError as e:
            await queue.put({"type": "error", "text": f"could not reach the browser: {e}"})
            await queue.put({"type": "done"})
        except Exception as e:
            await queue.put({"type": "error", "text": f"{type(e).__name__}: {e}"})
            await queue.put({"type": "done"})

    # ------------------------------------------------------------------------------ takeover
    def pause(self, user: str): self.runtime(user).gate.pause()
    def resume(self, user: str): self.runtime(user).gate.resume()

    async def stop(self, user: str) -> None:
        rt = self.rts.get(user)
        if rt:
            rt.gate.resume()
            if rt.run and not rt.run.done():
                rt.run.cancel()
