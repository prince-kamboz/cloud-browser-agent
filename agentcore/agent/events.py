"""Turn LangGraph 'updates' chunks into small UI events (same event vocabulary as v1)."""
import json
import re

from langchain_core.messages import AIMessage, ToolMessage

from agentcore.agent.tools import text_of


def short(v, n=90):
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s if len(s) <= n else s[:n] + "…"


# Playwright MCP lists tabs in its replies: "- 1: (current) [Title](https://url)". The current one is the tab the agent is on.
CURRENT_TAB = re.compile(r"^- \d+: \(current\) \[(.*)\]\((\S*?)\)( \[crashed\])?\s*$", re.M)


def current_tab(text: str):
    m = CURRENT_TAB.search(text or "")
    return {"type": "agent_tab", "title": m.group(1), "url": m.group(2)} if m else None


def events_from(ns, update):
    who = "browser" if ns else "main"
    for _node, upd in update.items():
        msgs = upd.get("messages") if isinstance(upd, dict) else None
        if not isinstance(msgs, list):
            continue
        for m in msgs:
            if isinstance(m, AIMessage):
                text = text_of(m.content).strip()
                for c in m.tool_calls or []:
                    args = ", ".join(short(v) for v in (c.get("args") or {}).values())
                    yield {"type": "action", "agent": who, "text": f"{c['name']}({args})"}
                if text and not m.tool_calls:
                    yield {"type": "assistant" if who == "main" else "thinking", "text": text}
            elif isinstance(m, ToolMessage):
                if m.status == "error":
                    yield {"type": "error", "text": f"{m.name}: {short(text_of(m.content), 300)}"}
                else:
                    tab = current_tab(text_of(m.content))
                    if tab:
                        yield tab
