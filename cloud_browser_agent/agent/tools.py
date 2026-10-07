from __future__ import annotations

from langchain_core.messages import HumanMessage
from langchain_core.tools import tool

from cloud_browser_agent.agent.mcp_session import McpBrowser
from cloud_browser_agent.agent.prompts import VISION_PROMPT


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") in ("text", "output_text"))
    return str(content)


def make_look_tool(mcp: McpBrowser, vision_llm_factory):
    """look(question): screenshot the page, ask a vision model, return TEXT (images never enter the agent's context)."""
    @tool
    async def look(question: str) -> str:
        """Take a screenshot of the current page and have a vision model answer a question about it, in text.
        Use only when the accessibility snapshot cannot answer (visual-only controls, date pickers, canvas, layout,
        CAPTCHA/popup checks) or to find the pixel position of something that has no ref."""
        r = await mcp.call("browser_take_screenshot", {"type": "png"})
        img = next((c for c in r.content if getattr(c, "type", "") == "image"), None)
        if img is None:
            return "Error: screenshot failed: " + " ".join(getattr(c, "text", "") for c in r.content)[:300]
        msg = HumanMessage(content=[
            {"type": "text", "text": VISION_PROMPT.format(question=question)},
            {"type": "image_url", "image_url": {"url": f"data:{img.mimeType};base64,{img.data}", "detail": "high"}},
        ])
        out = await vision_llm_factory().ainvoke([msg])
        return text_of(out.content) or "(vision model returned no text)"
    return look
