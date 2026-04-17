"""Codex (OpenAI) agent implementation."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from coral.agents.base import BaseAgent, ExtractedSession

SUMMARY_RE = re.compile(r"^[\s\u25cf\u23fa]*\|\|PULSE:SUMMARY (.*?)\|\|", re.MULTILINE)

FTS_BODY_CAP = 50_000

CODEX_HISTORY_BASE = Path.home() / ".codex" / "sessions"


def _clean_match(text: str) -> str:
    return " ".join(text.split())


def _extract_codex_text(entry: dict) -> str:
    """Extract plain text from a Codex JSONL entry."""
    # Codex stores messages with a "content" field that can be string or list
    msg = entry.get("message", entry)
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


class CodexAgent(BaseAgent):
    """OpenAI Codex agent."""

    @property
    def agent_type(self) -> str:
        return "codex"

    @property
    def supports_resume(self) -> bool:
        return True

    @property
    def history_base_path(self) -> Path:
        return CODEX_HISTORY_BASE

    @property
    def history_glob_pattern(self) -> str:
        return "**/*.jsonl"

    def available_commands(self, working_dir: str | None = None) -> list[dict[str, str]]:
        return [
            {"name": "compact", "command": "/compact", "description": "Compress conversation history"},
            {"name": "clear", "command": "/clear", "description": "Clear conversation and start fresh"},
        ]

    def build_launch_command(
        self,
        session_id: str,
        protocol_path: Path | None,
        resume_session_id: str | None = None,
        flags: list[str] | None = None,
        working_dir: str | None = None,
        board_name: str | None = None,
        role: str | None = None,
        prompt: str | None = None,
        prompt_overrides: dict[str, str] | None = None,
        board_type: str | None = None,
    ) -> str:
        parts = ["codex"]

        # --no-alt-screen: run TUI inline so pipe-pane can capture output.
        # (Codex's alt-screen mode is not captured by tmux pipe-pane.)
        parts.append("--no-alt-screen")

        # Codex uses --dangerously-bypass-approvals-and-sandbox for yolo mode
        parts.append("--dangerously-bypass-approvals-and-sandbox")

        # Build system prompt from protocol + board instructions.
        # Codex CLI has no --system-prompt flag, so we prepend system
        # instructions to the initial prompt text.
        board_prompt = self._build_board_system_prompt(board_name, role, prompt, prompt_overrides=prompt_overrides)
        sys_parts = []
        if protocol_path and protocol_path.exists():
            sys_parts.append(protocol_path.read_text())
        if board_prompt:
            sys_parts.append(board_prompt)

        if flags:
            # Strip auto-approve flags that belong to OTHER agents.
            # Codex already adds --dangerously-bypass-approvals-and-sandbox above.
            _skip = {"--dangerously-skip-permissions", "--yolo", "-y"}
            parts.extend(f for f in flags if f not in _skip)

        # Build the full prompt: system instructions (if any) + user prompt
        cli_prompt = prompt or ""
        if board_name:
            from coral.tools.session_manager import DEFAULT_ORCHESTRATOR_PROMPT, DEFAULT_WORKER_PROMPT
            is_orchestrator = role and "orchestrator" in role.lower()
            overrides = prompt_overrides or {}
            if is_orchestrator:
                template = overrides.get("default_prompt_orchestrator") or DEFAULT_ORCHESTRATOR_PROMPT
            else:
                template = overrides.get("default_prompt_worker") or DEFAULT_WORKER_PROMPT
            action_text = template.replace("{board_name}", board_name)
            cli_prompt = f"{cli_prompt}\n\n{action_text}" if cli_prompt else action_text

        # Prepend system instructions to prompt since codex has no system prompt flag
        if sys_parts:
            system_text = "\n\n".join(sys_parts)
            cli_prompt = f"{system_text}\n\n---\n\n{cli_prompt}" if cli_prompt else system_text

        if cli_prompt:
            prompt_file = Path(f"/tmp/coral_prompt_{session_id}.txt")
            prompt_file.write_text(cli_prompt)
            parts.append(f"\"$(cat '{prompt_file}')\"")

        return " ".join(parts)

    def load_history_sessions(self) -> list[dict[str, Any]]:
        if not CODEX_HISTORY_BASE.exists():
            return []

        result = []
        for jsonl_file in CODEX_HISTORY_BASE.rglob("*.jsonl"):
            try:
                entries = []
                with open(jsonl_file, "r", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue

                if not entries:
                    continue

                # Extract session ID from filename (rollout-DATE-UUID.jsonl)
                session_id = jsonl_file.stem
                first_ts = entries[0].get("timestamp") if entries else None
                last_ts = entries[-1].get("timestamp") if entries else None

                # Find first user message for summary
                first_user = ""
                for entry in entries:
                    role = entry.get("role", entry.get("type", ""))
                    if role in ("user", "human"):
                        text = _extract_codex_text(entry)
                        if text:
                            first_user = text[:100]
                            break

                result.append({
                    "session_id": session_id,
                    "first_timestamp": first_ts,
                    "last_timestamp": last_ts,
                    "source_file": str(jsonl_file),
                    "source_type": "codex",
                    "summary": first_user or "(no messages)",
                    "message_count": len(entries),
                })
            except OSError:
                continue

        return result

    def load_session_messages(self, session_id: str) -> list[dict[str, Any]]:
        if not CODEX_HISTORY_BASE.exists():
            return []

        for jsonl_file in CODEX_HISTORY_BASE.rglob("*.jsonl"):
            if jsonl_file.stem != session_id:
                continue
            messages = []
            try:
                with open(jsonl_file, "r", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            messages.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except OSError:
                continue
            return messages

        return []

    def extract_sessions(self, path: Path) -> list[ExtractedSession]:
        """Parse a Codex JSONL file and return extracted session data."""
        entries = []
        try:
            with open(path, "r", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []

        if not entries:
            return []

        session_id = path.stem
        first_ts = entries[0].get("timestamp")
        last_ts = entries[-1].get("timestamp")

        first_user = ""
        texts: list[str] = []
        for entry in entries:
            text = _extract_codex_text(entry)
            if text.strip():
                texts.append(text)
            role = entry.get("role", entry.get("type", ""))
            if not first_user and role in ("user", "human"):
                first_user = text[:100]

        summary = first_user or "(no messages)"
        body = "\n".join(texts)[:FTS_BODY_CAP]
        return [ExtractedSession(
            session_id=session_id,
            source_type="codex",
            first_timestamp=first_ts,
            last_timestamp=last_ts,
            message_count=len(entries),
            display_summary=summary,
            fts_body=body,
        )]
