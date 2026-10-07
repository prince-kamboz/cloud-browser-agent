import asyncio
import os
import stat
import unittest

from cloud_browser_agent.agent import connection as c

try:
    from cloud_browser_agent.agent.gate import Gate, TOOK_CONTROL
except ImportError:                       # `mcp` is only installed in the agent image
    Gate = None


class ConfigTests(unittest.TestCase):
    HEADERS = {"Host": "h", "X-Amz-Date": "20261006T120000Z", "Authorization": "AWS4-HMAC-SHA256 ...", "X-Amz-Security-Token": "tok",
               "Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Key": "k", "Sec-WebSocket-Version": "13",
               "User-Agent": "BrowserAgentV2/1.0"}

    def test_only_credentials_are_passed_on_the_handshake_headers_are_not(self):
        cfg = c.mcp_config("wss://x/automation", self.HEADERS)["browser"]
        self.assertEqual(cfg["cdpEndpoint"], "wss://x/automation")
        self.assertEqual(set(cfg["cdpHeaders"]), {"X-Amz-Date", "Authorization", "X-Amz-Security-Token", "User-Agent"})

    def test_config_file_is_private_and_removable(self):
        path = c.write_private_config(c.mcp_config("wss://x", self.HEADERS))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        c.remove_private_config(path)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(os.path.dirname(path)))

    def test_static_source_returns_copies(self):
        s = c.StaticSource("wss://x", {"A": "1"})
        ws, h = s.get("alice"); h["A"] = "changed"
        self.assertEqual(s.get("alice")[1]["A"], "1")


@unittest.skipIf(Gate is None, "needs the mcp package (agent image)")
class GateTests(unittest.TestCase):
    def run_async(self, coro): return asyncio.run(coro)

    def test_passes_through_when_not_paused(self):
        async def go():
            async def handler(req): return "ran"
            return await Gate()(object(), handler)
        self.assertEqual(self.run_async(go()), "ran")

    def test_paused_call_waits_then_is_not_executed(self):
        async def go():
            g, ran = Gate(poll_s=0.01), []
            async def handler(req): ran.append(1); return "ran"
            g.pause()
            task = asyncio.create_task(g(object(), handler))
            await asyncio.sleep(0.1)
            still_waiting = not task.done()
            g.resume()
            out = await task
            return still_waiting, ran, out.content[0].text
        waiting, ran, text = self.run_async(go())
        self.assertTrue(waiting)
        self.assertEqual(ran, [])                  # the stale action was never executed
        self.assertEqual(text, TOOK_CONTROL)


if __name__ == "__main__":
    unittest.main()


class CurrentTabTests(unittest.TestCase):
    def test_current_tab_is_read_from_the_tab_list(self):
        try:
            from cloud_browser_agent.agent.events import current_tab
        except ImportError:
            self.skipTest("langchain/mcp not installed")
        text = "### Open tabs\n- 0: [Hacker News](https://news.ycombinator.com/)\n- 1: (current) [Example Domain](https://example.com/)\n"
        self.assertEqual(current_tab(text), {"type": "agent_tab", "title": "Example Domain", "url": "https://example.com/"})
        self.assertIsNone(current_tab("### Open tabs\n- 0: [A](https://a.example/)"))
        self.assertEqual(current_tab("- 2: (current) [A [b]](https://a.example/x?y=1) [crashed]")["url"], "https://a.example/x?y=1")
        self.assertIsNone(current_tab(""))


class ChatRejectionTests(unittest.IsolatedAsyncioTestCase):
    """A message that cannot start must never resume a paused agent (the 'Take over ends by itself' bug)."""

    def make(self):
        try:
            import asyncio
            from cloud_browser_agent.agent.connection import StaticSource
            from cloud_browser_agent.agent.service import AgentService
        except ImportError:
            self.skipTest("langchain/mcp not installed")
        release = asyncio.Event()

        class FakeMcp:
            alive, ws_url = True, "ws://x"
            async def start(self, c=None): pass
            async def stop(self): pass

        class FakeAgent:
            async def astream(self, *a, **k):
                await release.wait()
                return
                yield

        svc = AgentService(StaticSource("ws://x"), agent_builder=lambda rt: FakeAgent(), mcp_factory=lambda u, g: FakeMcp())
        return svc, release

    async def first_event(self, svc, user="alice"):
        async for ev in svc.chat(user, "hi", "t"):
            return ev

    async def test_a_second_message_while_a_task_runs_is_rejected_and_does_not_resume(self):
        import asyncio
        svc, release = self.make()
        run = asyncio.create_task(self.first_event(svc))        # task 1 starts and waits
        await asyncio.sleep(0.05)
        svc.pause("alice")                                        # the user takes over
        ev = await self.first_event(svc)                          # then sends another message
        self.assertEqual((ev["type"], ev["code"]), ("error", "busy"))
        self.assertTrue(svc.runtime("alice").gate.paused)         # still paused: the user keeps control
        release.set()
        run.cancel()

    async def test_a_message_while_the_user_has_control_is_rejected_and_stays_paused(self):
        svc, release = self.make()
        svc.pause("alice")
        ev = await self.first_event(svc)
        self.assertEqual((ev["type"], ev["code"]), ("error", "paused"))
        self.assertTrue(svc.runtime("alice").gate.paused)

    async def test_a_normal_message_still_starts_a_run_and_clears_the_pause_state(self):
        svc, release = self.make()
        release.set()
        types = [ev["type"] async for ev in svc.chat("alice", "hi", "t")]
        self.assertEqual(types[-1], "done")
        self.assertFalse(svc.runtime("alice").gate.paused)
