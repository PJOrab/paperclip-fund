"""
LLM-Aufruf über die Claude Code CLI (headless), NICHT über die Anthropic API.

`claude -p` nutzt die lokale OAuth-/Abo-Auth und liefert Opus 4.7 (bzw. die
gewählte Alias). Wird von n8n per SSH-Node auf dem VPS getriggert. Der Prompt
wird über stdin übergeben (umgeht ARG_MAX bei großen Datenmengen).
"""
import json
import re
import subprocess
import time

# Reine Reasoning-Calls: alle Tools deaktivieren (kein agentisches Verhalten).
DEFAULT_DENY = "Bash Edit Write Read WebFetch WebSearch Task NotebookEdit Glob Grep"


class ClaudeError(RuntimeError):
    pass


_RETRY_ATTEMPTS = 2
_RETRY_SLEEP = 30  # seconds between attempts


def call(user_prompt: str, *, system: str, model: str = "sonnet",
         timeout: int = 300, max_budget_usd: float | None = None) -> str:
    """Feuert einen Headless-Prompt und gibt den Text der Antwort zurück.

    Retries once on transient failures (timeout, non-zero exit, non-JSON envelope)
    so a brief API hiccup does not kill an entire briefing run.
    """
    cmd = [
        "claude", "-p",
        "--model", model,
        "--output-format", "json",
        "--system-prompt", system,
        "--disallowedTools", DEFAULT_DENY,
    ]
    if max_budget_usd is not None:
        cmd += ["--max-budget-usd", str(max_budget_usd)]

    last_err: ClaudeError | None = None
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            proc = subprocess.run(cmd, input=user_prompt, capture_output=True,
                                  text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            last_err = ClaudeError(f"claude timed out after {timeout}s (attempt {attempt})")
            if attempt < _RETRY_ATTEMPTS:
                time.sleep(_RETRY_SLEEP)
            continue

        if proc.returncode != 0:
            last_err = ClaudeError(f"claude exit {proc.returncode} (attempt {attempt}): {proc.stderr[:600]}")
            if attempt < _RETRY_ATTEMPTS:
                time.sleep(_RETRY_SLEEP)
            continue

        try:
            env = json.loads(proc.stdout)
        except json.JSONDecodeError:
            last_err = ClaudeError(f"non-JSON envelope (attempt {attempt}): {proc.stdout[:600]}")
            if attempt < _RETRY_ATTEMPTS:
                time.sleep(_RETRY_SLEEP)
            continue

        if env.get("is_error"):
            # is_error = hard error from Claude (bad model, auth, content policy) — don't retry
            raise ClaudeError(f"claude reported error: {str(env.get('result'))[:400]}")
        return env.get("result", "") or ""

    raise last_err  # type: ignore[misc]


def call_json(user_prompt: str, *, system: str, model: str = "sonnet", **kw):
    """Wie call(), parst die Antwort aber als JSON (mit Fence-/Block-Fallback)."""
    return _parse_json(call(user_prompt, system=system, model=model, **kw))


def _parse_json(text: str):
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        # Strip closing fence and any trailing explanation text after it.
        # Unanchored (\n?```.*) handles "```\nSome extra text" that the
        # end-anchored form \n?```$ would miss.
        t = re.sub(r"\n?```.*", "", t, flags=re.DOTALL).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    # Fallback: ersten {..}- oder [..]-Block extrahieren
    for op, cl in (("[", "]"), ("{", "}")):
        i, j = t.find(op), t.rfind(cl)
        if i != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except json.JSONDecodeError:
                continue
    raise ClaudeError(f"could not parse JSON from result: {text[:300]}")
