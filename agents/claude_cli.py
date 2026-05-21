"""
LLM-Aufruf über die Claude Code CLI (headless), NICHT über die Anthropic API.

`claude -p` nutzt die lokale OAuth-/Abo-Auth und liefert Opus 4.7 (bzw. die
gewählte Alias). Wird von n8n per SSH-Node auf dem VPS getriggert. Der Prompt
wird über stdin übergeben (umgeht ARG_MAX bei großen Datenmengen).
"""
import json
import re
import subprocess

# Reine Reasoning-Calls: alle Tools deaktivieren (kein agentisches Verhalten).
DEFAULT_DENY = "Bash Edit Write Read WebFetch WebSearch Task NotebookEdit Glob Grep"


class ClaudeError(RuntimeError):
    pass


def call(user_prompt: str, *, system: str, model: str = "sonnet",
         timeout: int = 300, max_budget_usd: float | None = None) -> str:
    """Feuert einen Headless-Prompt und gibt den Text der Antwort zurück."""
    cmd = [
        "claude", "-p",
        "--model", model,
        "--output-format", "json",
        "--system-prompt", system,
        "--disallowedTools", DEFAULT_DENY,
    ]
    if max_budget_usd is not None:
        cmd += ["--max-budget-usd", str(max_budget_usd)]

    try:
        proc = subprocess.run(cmd, input=user_prompt, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ClaudeError(f"claude timed out after {timeout}s")

    if proc.returncode != 0:
        raise ClaudeError(f"claude exit {proc.returncode}: {proc.stderr[:600]}")

    try:
        env = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise ClaudeError(f"non-JSON envelope: {proc.stdout[:600]}")

    if env.get("is_error"):
        raise ClaudeError(f"claude reported error: {str(env.get('result'))[:400]}")
    return env.get("result", "") or ""


def call_json(user_prompt: str, *, system: str, model: str = "sonnet", **kw):
    """Wie call(), parst die Antwort aber als JSON (mit Fence-/Block-Fallback)."""
    return _parse_json(call(user_prompt, system=system, model=model, **kw))


def _parse_json(text: str):
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t).strip()
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
