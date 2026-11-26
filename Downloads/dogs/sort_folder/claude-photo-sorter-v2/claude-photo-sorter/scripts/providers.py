"""Every vision AI the sorter can use, in one place (standard library only, no SDKs).

  gemini      Google Gemini         free tier   GEMINI_API_KEY      aistudio.google.com
  groq        Groq (Llama 4)        free tier   GROQ_API_KEY        console.groq.com
  mistral     Mistral               free tier   MISTRAL_API_KEY     console.mistral.ai
  openrouter  OpenRouter :free      free models OPENROUTER_API_KEY  openrouter.ai/keys
  ollama      Ollama on this PC     local, free no key              ollama.com
  claude      Claude Code (claude -p) your Claude plan, no key
  openai      OpenAI                paid        OPENAI_API_KEY
  anthropic   Anthropic API         paid        ANTHROPIC_API_KEY
  custom      any OpenAI-compatible endpoint (LM Studio, vLLM, Together, DeepSeek...)
              CUSTOM_BASE_URL + CUSTOM_MODEL (+ CUSTOM_API_KEY)

Keys come from the environment or the .env file in this folder. Only the 256px previews
(packed into contact sheets) are ever sent, never your original files.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from common import WORK, load_env

load_env()  # so OLLAMA_HOST (and anything else) set in .env is visible before we read it

UA = "photo-sorter/2.0 (+local)"
OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
if not OLLAMA.startswith("http"):
    OLLAMA = "http://" + OLLAMA


class ProviderError(Exception):
    """kind: rate (wait and retry), quota (out for today), auth (bad key), model (try next model),
    transient (network/5xx), bad_output (answer unusable), fatal."""

    def __init__(self, msg, kind="transient", retry_after=None):
        super().__init__(msg)
        self.kind = kind
        self.retry_after = retry_after


@dataclass
class Provider:
    name: str
    label: str
    api: str                      # openai | anthropic | gemini | ollama | claude
    base: str = ""
    models: list = field(default_factory=list)   # tried in order; a dead model falls through to the next
    key_envs: tuple = ()
    paid: bool = False
    rpm: int = 0                  # requests per minute we allow ourselves (0 = unlimited)
    workers: int = 3
    json_mode: bool = False
    signup: str = ""
    hint: str = ""

    def key(self, env=os.environ):
        for n in self.key_envs:
            if env.get(n, "").strip():
                return env[n].strip()
        if env.get("AI_PROVIDER", "").strip().lower() == self.name and env.get("AI_KEY", "").strip():
            return env["AI_KEY"].strip()  # key typed into the dashboard
        return ""


REGISTRY = {p.name: p for p in [
    Provider("gemini", "Google Gemini (free tier)", "gemini",
             base="https://generativelanguage.googleapis.com/v1beta",
             models=["gemini-3.6-flash", "gemini-2.5-flash", "gemini-2.0-flash"],
             key_envs=("GEMINI_API_KEY", "GOOGLE_API_KEY"), rpm=10, workers=2,
             signup="https://aistudio.google.com/apikey", hint="Free key. About 10 requests a minute."),
    Provider("groq", "Groq - Llama 4 (free tier, very fast)", "openai",
             base="https://api.groq.com/openai/v1",
             models=["meta-llama/llama-4-scout-17b-16e-instruct", "meta-llama/llama-4-maverick-17b-128e-instruct"],
             key_envs=("GROQ_API_KEY",), rpm=25, workers=3, json_mode=True,
             signup="https://console.groq.com/keys", hint="Free key. Very fast."),
    Provider("mistral", "Mistral (free tier)", "openai",
             base="https://api.mistral.ai/v1",
             models=["mistral-small-latest", "pixtral-12b-2409"],
             key_envs=("MISTRAL_API_KEY",), rpm=30, workers=2, json_mode=True,
             signup="https://console.mistral.ai/api-keys", hint="Free 'Experiment' plan key."),
    Provider("openrouter", "OpenRouter (free models)", "openai",
             base="https://openrouter.ai/api/v1",
             models=["google/gemini-2.0-flash-exp:free", "meta-llama/llama-3.2-11b-vision-instruct:free",
                     "qwen/qwen2.5-vl-72b-instruct:free"],
             key_envs=("OPENROUTER_API_KEY",), rpm=15, workers=2,
             signup="https://openrouter.ai/keys", hint="One key, many models. Free ones are slow at busy times."),
    Provider("ollama", "Ollama (on this computer, free)", "ollama", base=OLLAMA,
             models=["llama3.2-vision", "qwen2.5vl", "gemma3", "llava"], workers=1,
             signup="https://ollama.com/download", hint="No key. Run `ollama pull llama3.2-vision` once. Nothing leaves your computer."),
    Provider("claude", "Claude Code (your Claude plan)", "claude", models=["sonnet", "haiku"], workers=4,
             hint="Uses the `claude` command and your plan. No key."),
    Provider("openai", "OpenAI (paid)", "openai", base="https://api.openai.com/v1",
             models=["gpt-4o-mini", "gpt-4.1-mini"], key_envs=("OPENAI_API_KEY",), paid=True, workers=4, json_mode=True,
             signup="https://platform.openai.com/api-keys", hint="Billed per use, about $0.001 per 30 files."),
    Provider("anthropic", "Anthropic API (paid)", "anthropic", base="https://api.anthropic.com/v1",
             models=["claude-haiku-5-5", "claude-haiku-4-5"], key_envs=("ANTHROPIC_API_KEY",), paid=True, workers=4,
             signup="https://console.anthropic.com/settings/keys", hint="Billed per use."),
    Provider("custom", "Custom OpenAI-compatible endpoint", "openai",
             base="", models=[], key_envs=("CUSTOM_API_KEY",), workers=2,
             hint="Set CUSTOM_BASE_URL and CUSTOM_MODEL in .env (LM Studio, vLLM, Together, DeepSeek...)."),
]}

# Order used when several engines are ready: best free quality/speed first, paid last.
AUTO_ORDER = ["claude", "gemini", "groq", "mistral", "openrouter", "ollama", "custom", "openai", "anthropic"]
FREE = [n for n in AUTO_ORDER if not REGISTRY[n].paid]


def _apply_env(env):
    """<NAME>_BASE_URL overrides an engine's address (proxies, regional endpoints, tests); CUSTOM_* defines the custom one."""
    for n, p in REGISTRY.items():
        url = (env.get(f"{n.upper()}_BASE_URL") or "").strip().rstrip("/")
        if url:
            p.base = url
    c = REGISTRY["custom"]
    if env.get("CUSTOM_MODEL"):
        c.models = [m.strip() for m in env["CUSTOM_MODEL"].split(",") if m.strip()]


def ollama_models(timeout=1.0):
    """Names of installed Ollama models, or None if Ollama is not running."""
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=timeout) as r:
            return [m["name"] for m in json.load(r).get("models", [])]
    except Exception:
        return None


_VISION = ("vision", "llava", "gemma3", "qwen2.5vl", "qwen2-vl", "minicpm-v", "moondream", "bakllava", "pixtral", "llama4")


def ready(env=os.environ):
    """{name: reason-or-''}: engines that can run right now. A model override applies to the first engine only."""
    _apply_env(env)
    out = {}
    for n, p in REGISTRY.items():
        if p.api == "claude":
            if shutil.which("claude"):
                out[n] = ""
        elif p.api == "ollama":
            have = ollama_models()
            if have is not None:
                good = [m for m in have if any(v in m.lower() for v in _VISION)]
                if good:
                    p.models = good + [m for m in p.models if m not in good]
                    out[n] = ""
        elif n == "custom":
            if p.base and p.models:
                out[n] = ""
        elif p.key(env):
            out[n] = ""
    return out


def choose(spec, env=os.environ):
    """Turn 'auto', 'gemini,groq' or one name into a list of ready Provider objects, in order."""
    spec = (spec or "auto").strip().lower()
    avail = ready(env)
    if spec in ("", "auto", "free"):
        allow_paid = env.get("AI_ALLOW_PAID", "").strip() in ("1", "true", "yes")
        names = [n for n in AUTO_ORDER if n in avail and (allow_paid or not REGISTRY[n].paid) and n != "custom"]
        if "custom" in avail and env.get("CUSTOM_IN_AUTO", "1") != "0":
            names.insert(len([n for n in names if n in FREE]), "custom")
    else:
        names = [n.strip() for n in spec.split(",") if n.strip()]
        unknown = [n for n in names if n not in REGISTRY]
        if unknown:
            raise ValueError("Unknown AI: " + ", ".join(unknown) + ". Choose from: " + ", ".join(REGISTRY))
        missing = [n for n in names if n not in avail]
        if missing:
            raise ValueError("Not ready: " + ", ".join(missing) + ". " + "; ".join(
                f"{n}: {REGISTRY[n].hint}" for n in missing))
    if not names:
        raise ValueError("No AI is ready. Add a free key (GEMINI_API_KEY or GROQ_API_KEY) to .env, "
                         "start Ollama, or install Claude Code. See README.")
    model_override = (env.get("AI_MODEL") or "").strip()
    out = []
    for i, n in enumerate(names):
        p = REGISTRY[n]
        if model_override and (len(names) == 1 or i == 0 and spec not in ("", "auto", "free")):
            p = Provider(**{**p.__dict__, "models": [m.strip() for m in model_override.split(",") if m.strip()]})
        out.append(p)
    return out


# ---------------------------------------------------------------- HTTP

_RETRY_RE = re.compile(r"(?:retry(?:Delay)?\"?[:\s]+\"?|try again in |retry in )([\d.]+)\s*(ms|s|m)?", re.I)


def _retry_after(headers, body):
    ra = headers.get("Retry-After") if headers else None
    if ra:
        try:
            return min(float(ra), 600)
        except ValueError:
            pass
    m = _RETRY_RE.search(body or "")
    if m:
        v = float(m.group(1))
        v = v / 1000 if m.group(2) == "ms" else v * 60 if m.group(2) == "m" else v
        return min(v, 600)
    return None


def post_json(url, body, headers, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": UA, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        low = text.lower()
        msg = f"HTTP {e.code}: {' '.join(text.split())[:240]}"
        ra = _retry_after(e.headers, text)
        if e.code == 429 or e.code == 503 and ra:
            if any(w in low for w in ("per day", "perday", "daily", "tokens per day", "quota exceeded for metric")) \
                    and (ra is None or ra > 120):
                raise ProviderError(msg, "quota", ra)
            raise ProviderError(msg, "rate", ra or 20)
        if e.code in (401, 403):
            raise ProviderError(msg, "auth")
        if e.code == 404 or e.code == 400 and any(w in low for w in ("model", "image", "vision", "multimodal", "modalit")):
            raise ProviderError(msg, "model")
        if e.code in (408, 409, 425, 500, 502, 503, 504, 520, 522, 524):
            raise ProviderError(msg, "transient", ra)
        raise ProviderError(msg, "fatal")
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError) as e:
        raise ProviderError(f"network: {e}", "transient")


def _b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def ask(provider, model, key, prompt, images, timeout=120):
    """One request: a prompt plus image file paths in, the model's text out."""
    a = provider.api
    if a == "openai":
        if not provider.base:
            raise ProviderError("CUSTOM_BASE_URL is not set", "auth")
        content = [{"type": "text", "text": prompt}] + [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _b64(p)}} for p in images]
        body = {"model": model, "temperature": 0, "max_tokens": 4096, "messages": [{"role": "user", "content": content}]}
        if provider.json_mode:
            body["response_format"] = {"type": "json_object"}
        h = {"Authorization": "Bearer " + key} if key else {}
        if provider.name == "openrouter":
            h.update({"HTTP-Referer": "http://localhost:8765", "X-Title": "Photo Sorter"})
        j = post_json(provider.base + "/chat/completions", body, h, timeout)
        try:
            return j["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise ProviderError("unexpected reply: " + str(j)[:200], "bad_output")
    if a == "anthropic":
        content = [{"type": "text", "text": prompt}] + [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _b64(p)}} for p in images]
        body = {"model": model, "max_tokens": 4096, "temperature": 0, "messages": [{"role": "user", "content": content}]}
        j = post_json(provider.base + "/messages", body, {"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout)
        return "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
    if a == "gemini":
        parts = [{"text": prompt}] + [{"inline_data": {"mime_type": "image/jpeg", "data": _b64(p)}} for p in images]
        body = {"contents": [{"parts": parts}],
                "generationConfig": {"temperature": 0, "responseMimeType": "application/json", "maxOutputTokens": 8192}}
        j = post_json(f"{provider.base}/models/{model}:generateContent", body, {"x-goog-api-key": key}, timeout)
        cands = j.get("candidates") or []
        if not cands:
            raise ProviderError("blocked or empty: " + str(j.get("promptFeedback") or j)[:200], "bad_output")
        return "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", []) if not p.get("thought"))
    if a == "ollama":
        body = {"model": model, "stream": False, "format": "json", "keep_alive": "15m", "options": {"temperature": 0},
                "messages": [{"role": "user", "content": prompt, "images": [_b64(p) for p in images]}]}
        j = post_json(provider.base + "/api/chat", body, {}, max(timeout, 600))
        return (j.get("message") or {}).get("content", "")
    if a == "claude":
        return _ask_claude_cli(model, prompt, images)
    raise ProviderError(f"unknown api {a}", "fatal")


def _ask_claude_cli(model, prompt, images):
    claude = shutil.which("claude")
    if not claude:
        raise ProviderError("`claude` command not found", "auth")
    full = prompt + "\n\nUse the Read tool to look at each of these contact sheet images:\n" + "\n".join(images)
    try:
        r = subprocess.run([claude, "-p", full, "--model", model, "--allowedTools", "Read", "--strict-mcp-config",
                            "--setting-sources", "project", "--no-session-persistence", "--output-format", "json"],
                           cwd=WORK, capture_output=True, text=True, timeout=900, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        raise ProviderError("claude took longer than 15 minutes", "transient")
    try:
        res = json.loads(r.stdout)
    except ValueError:
        res = {"result": r.stdout or r.stderr}
    text = str(res.get("result", ""))
    if res.get("is_error") or r.returncode and not text.strip().startswith(("{", "```")):
        low = text.lower()
        if "limit" in low or "usage" in low or "overloaded" in low:
            raise ProviderError(text[:160], "rate", 90)
        raise ProviderError((text or r.stderr or "claude failed")[:160], "transient")
    return text


def parse_json(text):
    """Pull the first JSON object out of a model answer (handles code fences and chatter)."""
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    for m in re.finditer(r"\{", text):  # first '{' that starts a valid object
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[m.start():])
            if isinstance(obj, dict):
                return obj
        except ValueError:
            continue
    return {}