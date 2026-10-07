"""Drive the running agent service the way the UI will: POST /chat, read the event stream."""
import json
import sys
import urllib.request

MSG = sys.argv[1]
req = urllib.request.Request("http://127.0.0.1:8200/chat", method="POST", headers={"content-type": "application/json"},
                             data=json.dumps({"user": "alice", "message": MSG, "thread_id": "e2e"}).encode())
actions, answer, errors = [], [], []
with urllib.request.urlopen(req, timeout=300) as r:
    buf = ""
    for raw in r:
        line = raw.decode().strip()
        if not line.startswith("data: "):
            continue
        ev = json.loads(line[6:])
        if ev["type"] == "action": actions.append(f"{ev['agent']}: {ev['text'][:80]}")
        elif ev["type"] == "assistant": answer.append(ev["text"])
        elif ev["type"] == "error": errors.append(ev["text"][:300])
        elif ev["type"] == "done": break
print("steps the agent took:"); [print("  -", a) for a in actions]
print("errors:", errors or "none")
print("ANSWER:", "\n".join(answer)[:600])
