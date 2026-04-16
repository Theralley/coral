"""Qwen Code agent implementation."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from coral.agents.base import BaseAgent, ExtractedSession

SUMMARY_RE = re.compile(r"^[\s\u25cf\u23fa]*\|\|PULSE:SUMMARY (.*?)\|\|", re.MULTILINE)

FTS_BODY_CAP = 50_000

QWEN_HISTORY_BASE = Path.home() / ".qwen" / "projects"


def _clean_match(text: str) -> str:
    return " ".join(text.split())


def _extract_qwen_text(entry: dict) -> str:
    """Extract plain text from a Qwen JSONL entry.

    Qwen uses the same JSONL format as Claude Code.
    """
    msg = entry.get("message", {})
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


class QwenAgent(BaseAgent):
    """Qwen Code agent."""

    @property
    def agent_type(self) -> str:
        return "qwen"

    @property
    def supports_resume(self) -> bool:
        return True

    @property
    def history_base_path(self) -> Path:
        return QWEN_HISTORY_BASE

    @property
    def history_glob_pattern(self) -> str:
        return "**/*.jsonl"

    def available_commands(self, working_dir: str | None = None) -> list[dict[str, str]]:
        return [
            {"name": "compact", "command": "/compact", "description": "Compress conversation history"},
            {"name": "clear", "command": "/clear", "description": "Clear conversation and start fresh"},
            {"name": "help", "command": "/help", "description": "Show available commands"},
            {"name": "cost", "command": "/cost", "description": "Show token usage and cost"},
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
        parts = ["qwen"]

        # Qwen uses --yolo for auto-approve mode
        parts.append("--yolo")

        if resume_session_id:
            parts.append(f"--resume {resume_session_id}")
        else:
            parts.append(f"--session-id {session_id}")

        # Build system prompt from protocol + board instructions
        board_prompt = self._build_board_system_prompt(board_name, role, prompt, prompt_overrides=prompt_overrides)
        sys_parts = []
        if protocol_path and protocol_path.exists():
            sys_parts.append(protocol_path.read_text())
        if board_prompt:
            sys_parts.append(board_prompt)
        if sys_parts:
            combined = "\n\n".join(sys_parts)
            parts.append(f"--append-system-prompt \"{combined[:2000]}\"")

        if flags:
            parts.extend(flags)

        # Pass initial prompt
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
        if cli_prompt:
            prompt_file = Path(f"/tmp/coral_prompt_{session_id}.txt")
            prompt_file.write_text(cli_prompt)
            parts.append(f"\"$(cat '{prompt_file}')\"")

        return " ".join(parts)

    def load_history_sessions(self) -> list[dict[str, Any]]:
        if not QWEN_HISTORY_BASE.exists():
            return []

        sessions: dict[str, dict[str, Any]] = {}
        for history_file in QWEN_HISTORY_BASE.rglob("*.jsonl"):
            try:
                with open(history_file, "r", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        session_id = entry.get("sessionId")
                        if not session_id:
                            continue

                        if session_id not in sessions:
                            sessions[session_id] = {
                                "session_id": session_id,
                                "messages": [],
                                "first_timestamp": entry.get("timestamp"),
                                "last_timestamp": entry.get("timestamp"),
                                "source_file": str(history_file),
                                "source_type": "qwen",
                                "summary": None,
                            }

                        ts = entry.get("timestamp")
                        if ts:
                            s = sessions[session_id]
                            if not s["first_timestamp"] or ts < s["first_timestamp"]:
                                s["first_timestamp"] = ts
                            if not s["last_timestamp"] or ts > s["last_timestamp"]:
                                s["last_timestamp"] = ts

                        sessions[session_id]["messages"].append(entry)
            except OSError:
                continue

        result = []
        for sid, data in sessions.items():
            first_human = ""
            summary_marker = ""
            for msg in data["messages"]:
                if not summary_marker and msg.get("type") == "assistant":
                    text = _extract_qwen_text(msg)
                    m = SUMMARY_RE.search(text)
                    if m:
                        summary_marker = _clean_match(m.group(1))

                if not first_human and msg.get("type") in ("human", "user"):
                    text = _extract_qwen_text(msg)
                    if text:
                        first_human = text[:100]

            data["summary"] = summary_marker or first_human or "(no messages)"
            data["message_count"] = len(data["messages"])
            listing = {k: v for k, v in data.items() if k != "messages"}
            result.append(listing)

        return result

    def load_session_messages(self, session_id: str) -> list[dict[str, Any]]:
        if not QWEN_HISTORY_BASE.exists():
            return []

        messages = []
        for history_file in QWEN_HISTORY_BASE.rglob("*.jsonl"):
            try:
                with open(history_file, "r", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if entry.get("sessionId") == session_id:
                            messages.append(entry)
            except OSError:
                continue

        return messages

    def extract_sessions(self, path: Path) -> list[ExtractedSession]:
        """Parse a Qwen JSONL file and return extracted session data."""
        sessions: dict[str, dict[str, Any]] = {}
        try:
            with open(path, "r", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    sid = entry.get("sessionId")
                    if not sid:
                        continue

                    if sid not in sessions:
                        sessions[sid] = {
                            "texts": [],
                            "first_ts": entry.get("timestamp"),
                            "last_ts": entry.get("timestamp"),
                            "msg_count": 0,
                            "first_human": "",
                        }

                    s = sessions[sid]
                    s["msg_count"] += 1
                    ts = entry.get("timestamp")
                    if ts:
                        if not s["first_ts"] or ts < s["first_ts"]:
                            s["first_ts"] = ts
                        if not s["last_ts"] or ts > s["last_ts"]:
                            s["last_ts"] = ts

                    text = _extract_qwen_text(entry)
                    if text.strip():
                        s["texts"].append(text)

                    if not s["first_human"] and entry.get("type") in ("human", "user"):
                        s["first_human"] = text[:100]
        except OSError:
            return []

        results = []
        for sid, s in sessions.items():
            summary = s["first_human"] or "(no messages)"
            body = "\n".join(s["texts"])[:FTS_BODY_CAP]
            results.append(ExtractedSession(
                session_id=sid,
                source_type="qwen",
                first_timestamp=s["first_ts"],
                last_timestamp=s["last_ts"],
                message_count=s["msg_count"],
                display_summary=summary,
                fts_body=body,
            ))
        return results
