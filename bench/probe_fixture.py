"""probe_fixture -- the one owner of the disposable-session machinery shared by
bench/pretooluse_ordering_probe.py, bench/pretooluse_rewrite_deny_probe.py and
bench/dispatch_probe.py (TOP-0144 L1): the disposable HOME / CLAUDE_CONFIG_DIR /
TMPDIR layout, project-scoped settings writing, per-run nonce creation and the
nonce-never-in-the-project-tree scan, the stream-json `claude -p` argv and
environment, launching the CLI, and reading the transcript / a JSONL log.

Scenario-specific pieces (what the hook is, which files the project holds, the
prompt, the preflight, the analysis) stay in each probe. Stdlib only.
"""
from __future__ import annotations

import json
import os
import secrets
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any


def new_nonce(prefix: str, nbytes: int) -> str:
    """Per-run unguessable token. Each probe keeps its own `generate_nonce`
    seam (patchable by tests) that calls this with its prefix and size."""
    return f"{prefix}{secrets.token_hex(nbytes)}"


class BaseFixture:
    """The disposable layout every probe builds under `root`."""

    def __init__(self, root: Path, nonce: str):
        self.root = root
        self.nonce = nonce
        self.home = root / "home"
        self.claude_config = root / "claude-config"
        self.tmp = root / "tmp"
        self.project = root / "project"
        self.settings = self.project / ".claude" / "settings.json"


class ProbeHookFixture(BaseFixture):
    """BaseFixture plus the stand-in hook's state files (rewrite-deny and
    dispatch probes). The nonce file is a sibling of project/, never under it."""

    def __init__(self, root: Path, scenario: str, nonce: str):
        super().__init__(root, nonce)
        self.scenario = scenario
        self.hook_log = root / "hook-calls.jsonl"
        self.hook_counter = root / "hook-counter.txt"
        self.nonce_file = root / "nonce.txt"

    def seed_hook_state(self) -> None:
        self.hook_counter.write_text("0", encoding="utf-8")
        self.nonce_file.write_text(self.nonce, encoding="utf-8")


def make_dirs(*paths: Path) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def write_settings(path: Path, settings: dict[str, Any]) -> None:
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def env_prefix(env: dict[str, str]) -> str:
    """`K=v K2=v2` shell prefix, values quoted, in dict order."""
    return " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())


def hook_command(env: dict[str, str], *argv: str) -> str:
    """The settings `command` string: quoted env prefix, then the quoted argv."""
    return env_prefix(env) + " " + " ".join(shlex.quote(a) for a in argv)


def nonce_in_project_tree(project: Path, nonce: str) -> list[str]:
    hits = []
    for path in project.rglob("*"):
        if path.is_file():
            try:
                if nonce in path.read_text(encoding="utf-8", errors="ignore"):
                    hits.append(str(path))
            except OSError:
                continue
    return hits


def session_env(fixture: BaseFixture, extra: dict[str, str] | None = None) -> dict[str, str]:
    """The ONLY environment a real session sees (nothing inherited but PATH and
    the API key); `extra` entries sit between TMPDIR and PATH."""
    return {
        "HOME": str(fixture.home),
        "CLAUDE_CONFIG_DIR": str(fixture.claude_config),
        "TMPDIR": str(fixture.tmp),
        **(extra or {}),
        "PATH": os.environ.get("PATH", ""),
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
    }


def claude_argv(claude_binary: Path, prompt: str, tools: str, model: str, effort: str, max_budget_usd: float,
                allowed_tools: str | None = None, extra: list[str] | None = None) -> list[str]:
    return [
        str(claude_binary), "-p", prompt,
        "--output-format", "stream-json", "--include-hook-events", "--verbose",
        "--strict-mcp-config", "--setting-sources", "project",
        "--tools", tools,
        *(["--allowedTools", allowed_tools] if allowed_tools else []),
        "--permission-mode", "acceptEdits",
        "--model", model, "--effort", effort,
        "--max-budget-usd", str(max_budget_usd),
        *(extra or []),
    ]


def run_claude(argv: list[str], fixture: BaseFixture, env: dict[str, str], timeout: int,
               catch_timeout: bool = False, timeout_detail: bool = False, stdin_devnull: bool = False) -> subprocess.CompletedProcess:
    """Run the CLI in the fixture project. With `catch_timeout` a hang becomes a
    synthetic CompletedProcess (returncode -1, stderr 'TIMEOUT after Ns'[: exc])
    because a hang can itself be a measurement."""
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL} if stdin_devnull else {}
    try:
        return subprocess.run(argv, cwd=str(fixture.project), env=env, capture_output=True, text=True, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired as exc:
        if not catch_timeout:
            raise
        out = exc.stdout
        text = out.decode("utf-8", "ignore") if isinstance(out, (bytes, bytearray)) else (out or "")
        return subprocess.CompletedProcess(argv, returncode=-1, stdout=text,
                                           stderr=f"TIMEOUT after {timeout}s" + (f": {exc}" if timeout_detail else ""))


def parse_transcript(stdout: str) -> list[dict[str, Any]]:
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    # strict UTF-8 on purpose: a corrupted evidence log must raise, not shrink
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def default_claude_binary() -> Path:
    which = shutil.which("claude")
    if which:
        resolved = Path(which)
        try:
            resolved = resolved.resolve()
        except OSError:
            pass
        return resolved
    raise RuntimeError("no `claude` binary found on PATH; pass --claude-binary")
