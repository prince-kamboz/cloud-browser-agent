"""Turn LangGraph 'updates' chunks into small UI events (same event vocabulary as v1)."""
import json

from langchain_core.messages import AIMessage, ToolMessage

from agentcore.agent.tools import text_of


def short(v, n=90):
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s if len(s) <= n else s[:n] + "…"


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
            elif isinstance(m, ToolMessage) and m.status == "error":
                yield {"type": "error", "text": f"{m.name}: {short(text_of(m.content), 300)}"}
