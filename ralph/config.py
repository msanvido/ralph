"""Shared mutable singletons. ralph.main() mutates these at startup; everything else reads."""
import time
from pathlib import Path

import litellm
from dotenv import load_dotenv

load_dotenv()

# LiteLLM unconditionally prints a red "Provider List:" banner whenever its cost
# calculator (a post-completion hook) calls get_llm_provider() on the stripped
# model name in the response and fails to detect a provider. The exception is
# then swallowed by cost_calculator, but the banner has already hit stdout. This
# flag silences that print (and one cosmetic "Give Feedback" message). LiteLLM's
# own router and proxy modules set it the same way.
litellm.suppress_debug_info = True

# LiteLLM model string. Examples:
#   openrouter/qwen/qwen3-coder              (default)
#   openrouter/deepseek/deepseek-chat-v3.1
#   anthropic/claude-sonnet-4-6
#   openai/gpt-4o
#   gemini/gemini-2.0-flash
# All models MUST support forced tool_choice — validate_model() enforces this at startup.
# Some OpenRouter routes (e.g., qwen3.5-flash-02-23) accept completions but reject
# forced tool_choice, so they're not usable with Ralph.
# API keys are read from env: OPENROUTER_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY.
MODEL = "openrouter/qwen/qwen3-coder"
MAX_ITERATIONS = 50

# Short phrase describing this Ralph's expertise (e.g., "Sudoku solving").
# Auto-extracted from the prompt at startup by memory.extract_expertise.
EXPERTISE = ""

WORKSPACE = Path("workspace")
PROMPT_FILE = Path("prompt.md")
TOOLS_DIR = WORKSPACE / "tools"
MEMORY_DIR = WORKSPACE / "memory"
DONE_MARKER = WORKSPACE / "DONE"

completion = litellm.completion

# Errors that mean the configured model/credentials can't possibly work —
# rate limits / network blips are NOT in this set and stay transient.
FATAL_MODEL_ERRORS = (
    litellm.BadRequestError,
    litellm.AuthenticationError,
    litellm.NotFoundError,
    litellm.PermissionDeniedError,
)

COMPLETION_RETRIES = 3  # attempts per completion call before giving up


def completion_with_retry(**kwargs):
    """Call `completion`, retrying transient failures with exponential backoff.
    Fatal misconfiguration is caught by validate_model at startup, so anything
    that fails here is most likely a rate limit or a network blip — worth a
    couple of retries. Used by every LLM call path (iteration loop, memory
    select/learn, expertise extraction)."""
    for attempt in range(COMPLETION_RETRIES):
        try:
            return completion(**kwargs)
        except FATAL_MODEL_ERRORS:
            raise
        except Exception as e:
            if attempt == COMPLETION_RETRIES - 1:
                raise
            delay = 2 ** attempt
            print(f"  ⚠️  completion failed ({type(e).__name__}); retrying in {delay}s...")
            time.sleep(delay)


bus = None  # bus.Bus instance, set by main()
