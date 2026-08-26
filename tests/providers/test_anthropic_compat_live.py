import os
import unittest

from nanobot.providers import AnthropicCompatProvider, HumanMessage, SystemMessage
from tests.tools.fakes import WeatherTool

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_API_BASE = os.getenv(
    "DEEPSEEK_ANTHROPIC_API_BASE",
    "https://api.deepseek.com/anthropic",
)
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_ANTHROPIC_MODEL", "deepseek-v4-flash")
RUN_LIVE_TESTS = os.getenv("RUN_DEEPSEEK_ANTHROPIC_LIVE_TESTS") == "1"

@unittest.skipUnless(
    DEEPSEEK_API_KEY and RUN_LIVE_TESTS,
    "set DEEPSEEK_API_KEY and RUN_DEEPSEEK_ANTHROPIC_LIVE_TESTS=1 to run live tests",
)
class DeepSeekAnthropicLiveTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.provider = AnthropicCompatProvider(
            api_key=DEEPSEEK_API_KEY or "",
            api_base=DEEPSEEK_API_BASE,
            default_model=DEEPSEEK_MODEL,
            default_max_tokens=64,
            default_thinking={"type": "disabled"},
        )

    async def test_deepseek_anthropic_complete(self) -> None:
        response = await self.provider.complete(
            (
                SystemMessage(content="Reply with one short greeting."),
                HumanMessage(content="Say hello."),
            ),
            max_tokens=16,
            temperature=0,
        )

        self.assertTrue(response.content)

    async def test_deepseek_anthropic_stream(self) -> None:
        deltas: list[str] = []

        async def on_delta(delta: str) -> None:
            deltas.append(delta)

        response = await self.provider.stream(
            (HumanMessage(content="Reply with one short greeting."),),
            max_tokens=16,
            temperature=0,
            on_delta=on_delta,
        )

        self.assertTrue(response.content)
        self.assertTrue(deltas)
        self.assertEqual("".join(deltas), response.content)

    async def test_deepseek_anthropic_tool_call(self) -> None:
        response = await self.provider.complete(
            (
                SystemMessage(
                    content="You must call get_weather for weather questions."
                ),
                HumanMessage(content="What is the weather in Beijing?"),
            ),
            tools=(WeatherTool(),),
            max_tokens=64,
            temperature=0,
        )

        self.assertTrue(response.tool_calls)
        self.assertEqual(response.tool_calls[0].name, "get_weather")
        self.assertEqual(response.tool_calls[0].arguments.get("city"), "Beijing")
