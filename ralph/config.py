"""Shared mutable singletons. ralph.main() mutates these at startup; everything else reads."""
from pathlib import Path

import litellm
from dotenv import load_dotenv

load_dotenv()

# LiteLLM model string. Examples:
#   openrouter/deepseek/deepseek-chat-v3.1   (default)
#   openrouter/qwen/qwen3-coder
#   anthropic/claude-sonnet-4-6
#   openai/gpt-4o
#   gemini/gemini-2.0-flash
# API keys are read from env: OPENROUTER_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY.
MODEL = "openrouter/deepseek/deepseek-chat-v3.1"
MAX_ITERATIONS = 50

WORKSPACE = Path("workspace")
PROMPT_FILE = Path("prompt.md")
TOOLS_DIR = WORKSPACE / "tools"
MEMORY_DIR = WORKSPACE / "memory"
DONE_MARKER = WORKSPACE / "DONE"

completion = litellm.completion

bus = None  # bus.Bus instance, set by main()
