"""Which language model writes the SQL: one setting, any of eleven providers.

TWO IDENTICAL COPIES: pbp/explorer/llm.py and
ec2-nginx/weather-sql-explorer/sql_explorer/llm.py. The two apps are separate repos
with no shared package, so the copies are kept in step by hand, like the footer.
Nothing in here knows which app it is in; the caller passes what differs.

THE SETTING
-----------
    gemini-3.8-flash                  Google      (a bare gemini-* or claude-* name
    claude-sonnet-5-5                 Anthropic    works as it always has)
    openai:gpt-6-astra                provider:model for everything else
    openrouter:qwen/qwen3.8-max
    deepseek:deepseek-flash

Each provider reads its own key, so several can sit in the env file at once and a
switch is the one setting plus a restart. Each provider's package is imported only
when it is chosen, so a venv missing one still runs the others.

WHY ONE BRANCH COVERS NINE PROVIDERS
------------------------------------
Everything except Google and Anthropic speaks OpenAI's chat-completions protocol,
so `langchain-openai` with a different base URL reaches all of them. What differs is
which optional parameters each accepts, and the safe default for a provider not
tested against this app is to send none: no temperature (OpenAI's reasoning models
and several others reject one), no effort (a non-reasoning model rejects it), no
output cap (some providers' caps are lower than anything chosen here). The provider's
defaults are what you get. OpenAI itself is the exception, below.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


class SetupError(RuntimeError):
    """Configuration or connectivity problem, phrased for the operator."""


# Providers reached through the OpenAI-compatible protocol: (base URL, key variable).
# Base URLs checked against each provider's own documentation on 2026-10-12.
COMPATIBLE: dict[str, tuple[str | None, str]] = {
    "openai": (None, "OPENAI_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    "qwen": ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY"),
    "kimi": ("https://api.moonshot.ai/v1", "MOONSHOT_API_KEY"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "together": ("https://api.together.ai/v1", "TOGETHER_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "fireworks": ("https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY"),
}
PROVIDERS = ("google", "anthropic", *COMPATIBLE)

# The family a visitor would recognise, read from the model id -- so a Qwen model served
# by Together still says Qwen. First match wins.
_FAMILIES = (
    ("claude", "Claude"), ("gemini", "Gemini"), ("gemma", "Gemma"), ("gpt", "GPT"),
    ("deepseek", "DeepSeek"), ("qwen", "Qwen"), ("kimi", "Kimi"), ("mistral", "Mistral"),
    ("magistral", "Mistral"), ("devstral", "Mistral"), ("llama", "Llama"), ("glm", "GLM"),
    ("grok", "Grok"),
)
_PROVIDER_NAMES = {"openai": "OpenAI", "openrouter": "OpenRouter", "deepseek": "DeepSeek",
                   "qwen": "Qwen", "kimi": "Kimi", "mistral": "Mistral",
                   "together": "Together", "groq": "Groq", "fireworks": "Fireworks",
                   "google": "Gemini", "anthropic": "Claude"}

# OpenAI models that reason, and so take an effort -- and reject a temperature.
_OPENAI_REASONING = ("gpt-5", "gpt-6", "o1", "o3", "o4")


@dataclass(frozen=True)
class Choice:
    """A parsed model setting."""

    setting: str     # as written in the environment
    provider: str    # one of PROVIDERS, or "" when the setting names none
    model: str       # the id the provider is sent
    family: str      # "Claude", "Qwen", ... for prose on the page
    name: str        # "Claude Sonnet 5.5", "Qwen3.8 Max", ... for the diagram


def _display_name(model: str) -> str:
    """'gemini-3.8-flash' -> 'Gemini 3.8 Flash', 'qwen/qwen3.8-max' -> 'Qwen3.8 Max'."""
    words: list[str] = []
    for part in model.rsplit("/", 1)[-1].split(":")[0].split("-"):
        if words and part.replace(".", "").isdigit() and words[-1][-1].isdigit():
            words[-1] += "." + part           # claude-sonnet-5-5 -> Sonnet 5.5
        elif part.lower() in ("gpt", "glm"):
            words.append(part.upper())
        elif part.lower() == "deepseek":
            words.append("DeepSeek")
        else:
            words.append(part[:1].upper() + part[1:])
    return " ".join(words)


def parse(setting: str) -> Choice:
    """Read a model setting. Never raises: an unusable one fails in `build`, on the page."""
    setting = setting.strip()
    provider, sep, model = setting.partition(":")
    if not (sep and provider in PROVIDERS):
        # No prefix -- or a colon that belongs to the model id. Only the two bare forms
        # this app has always accepted are recognised; anything else needs a provider.
        model = setting
        provider = ("anthropic" if setting.startswith("claude-")
                    else "google" if setting.startswith("gemini-") else "")
    lowered = model.lower()
    family = next((f for key, f in _FAMILIES if key in lowered),
                  _PROVIDER_NAMES.get(provider, "a language model"))
    return Choice(setting, provider, model, family, _display_name(model))


def build(choice: Choice, *, effort: str, where: str,
          google_keys: tuple[str, ...] = ("GOOGLE_API_KEY",)) -> tuple[Any, list]:
    """The chat model a Choice names, and the agent middleware that goes with it.

    `effort` is applied wherever the provider takes one. `where` tells the operator where
    a missing key belongs. `google_keys` are tried in order for Gemini's key.
    """
    if choice.provider == "anthropic":
        return _anthropic(choice, effort, where)
    if choice.provider == "google":
        return _google(choice, effort, where, google_keys)
    if choice.provider in COMPATIBLE:
        return _compatible(choice, effort, where)
    raise SetupError(
        f"The model setting {choice.setting!r} names no provider. Write it as "
        f"provider:model, with provider one of {', '.join(PROVIDERS[2:])} -- or a bare "
        "gemini-* or claude-* name."
    )


def _key(variable: str, choice: Choice, where: str) -> str:
    key = os.environ.get(variable, "").strip()
    if not key:
        raise SetupError(f"The model is {choice.setting} but {variable} is not set. "
                         f"Put it in {where}.")
    return key


def _google(choice: Choice, effort: str, where: str,
            google_keys: tuple[str, ...]) -> tuple[Any, list]:
    key = next((os.environ[k] for k in google_keys if os.environ.get(k, "").strip()), "")
    if not key:
        raise SetupError(f"The model is {choice.setting} but {' / '.join(google_keys)} "
                         f"is not set. Put it in {where}.")
    os.environ.setdefault("GOOGLE_API_KEY", key)
    from langchain_google_genai import ChatGoogleGenerativeAI

    llm = ChatGoogleGenerativeAI(
        model=choice.model,
        temperature=0,        # SQL generation: same question, same query.
        max_retries=2,
        # Flash reasons by default and cannot be told not to -- the API accepts
        # low/medium/high and rejects "minimal".
        reasoning_effort=effort,
    )
    return llm, []


def _anthropic(choice: Choice, effort: str, where: str) -> tuple[Any, list]:
    _key("ANTHROPIC_API_KEY", choice, where)
    from langchain_anthropic import ChatAnthropic
    from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware

    settings: dict[str, Any] = {
        "model": choice.model,
        "max_retries": 2,
        # SET, NOT DEFAULTED. ChatAnthropic defaults to the model's full output cap (128k
        # on Sonnet 5.5), and the SDK refuses a non-streaming request that large because
        # it could outlive the HTTP timeout. An answer here is a few sentences.
        "max_tokens": 16_000,
    }
    if choice.model.startswith("claude-haiku-4-5"):
        # Haiku 4.5 predates effort and rejects it with a 400. It does not think unless
        # asked, so it is also the one Claude here that still accepts temperature=0.
        settings["temperature"] = 0
    else:
        # NO TEMPERATURE. Current Claude models reject a non-default one with a 400, so the
        # same-question-same-query intent is carried by low effort and the prompt.
        # Thinking is adaptive and cannot be switched off on Opus 5.5 / Sonnet 5.5; effort
        # is the control.
        settings["thinking"] = {"type": "adaptive"}
        settings["effort"] = effort
        # If a safety classifier declines a turn, the API retries it on a substitute model
        # instead of returning an empty answer.
        settings["betas"] = ["server-side-fallback-2026-07-01"]
        settings["model_kwargs"] = {"fallbacks": "default"}
    # Caches the system prompt and tool schemas, and the growing conversation between the
    # turns of one question -- each turn resends every describe_table result. The ledger
    # still counts cached tokens at full size (usage_metadata does), which overstates
    # spend rather than hiding it.
    return ChatAnthropic(**settings), [AnthropicPromptCachingMiddleware()]


def _compatible(choice: Choice, effort: str, where: str) -> tuple[Any, list]:
    base_url, variable = COMPATIBLE[choice.provider]
    key = _key(variable, choice, where)
    from langchain_openai import ChatOpenAI

    settings: dict[str, Any] = {"model": choice.model, "api_key": key, "max_retries": 2}
    if choice.provider == "openai":
        # OpenAI's own models get its own handling. Its reasoning models take an effort,
        # and the library moves GPT-6 with tools onto the Responses API by itself -- so
        # use_responses_api is left to it rather than pinned.
        if choice.model.startswith(_OPENAI_REASONING):
            settings["reasoning_effort"] = effort
    else:
        # Everyone else implements chat completions, not OpenAI's Responses API. Pinned,
        # because the library otherwise decides from the model id, and a model id is
        # not a promise about what an endpoint implements.
        settings["base_url"] = base_url
        settings["use_responses_api"] = False
    return ChatOpenAI(**settings), []


def refusal(exc: BaseException) -> str:
    """'billing', 'rate' or '' -- whether the provider refused the request outright.

    'billing' is out of credit or over a spend limit: it bought nothing, so the caller
    should not charge it. 'rate' is too many requests, or a provider too busy to take
    one. '' is anything else, including a 400 that merely contains "429". Anthropic's
    and OpenAI's SDKs raise a class per status, so those are read by type; Google's
    exposes only the message.
    """
    for reader in (_anthropic_refusal, _openai_refusal):
        kind = reader(exc)
        if kind is not None:
            return kind
    text = str(exc)
    if "402" in text or "prepayment" in text.lower() or "billing" in text.lower():
        return "billing"
    if "RESOURCE_EXHAUSTED" in text or "429" in text:
        return "rate"
    return ""


def _anthropic_refusal(exc: BaseException) -> str | None:
    try:
        import anthropic
    except ImportError:
        return None
    if isinstance(exc, (anthropic.RateLimitError, anthropic.OverloadedError)):
        return "rate"
    if isinstance(exc, anthropic.APIStatusError):
        # Out of credit and a Console spend limit are both a 400 that says so.
        text = str(exc).lower()
        if "credit balance" in text or "usage limit" in text or "spend limit" in text:
            return "billing"
        return ""
    return None


def _openai_refusal(exc: BaseException) -> str | None:
    try:
        import openai
    except ImportError:
        return None
    if isinstance(exc, openai.APIStatusError):
        text = str(exc).lower()
        # OpenAI reports an empty balance as a 429 with code insufficient_quota, which
        # must not read as "try again in a minute". OpenRouter and DeepSeek use 402.
        if (exc.status_code == 402 or "insufficient_quota" in text
                or "insufficient balance" in text):
            return "billing"
        if exc.status_code in (429, 503, 529):
            return "rate"
        return ""
    return None
