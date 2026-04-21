"""Mobile API — slim endpoints for the /mobile LAN phone UI.

Authentication: Bearer token in ``Authorization`` header, constant-time
compared against the token stored at ``<data_dir>/mobile_token``. The token
is auto-generated on first launch (0600 perms).

All endpoints here return payloads sized for phone UIs — no command lists,
no per-file git data. Heavy dashboard endpoints live in ``live_sessions``.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import socket
import time
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel

from coral.config import get_data_dir
from coral.tools.session_manager import discover_coral_agents, get_agent_log_path
from coral.tools.log_streamer import get_log_snapshot
from coral.tools.tmux_manager import send_to_tmux, capture_pane, send_raw_keys

if TYPE_CHECKING:
    from coral.store import CoralStore
    from coral.messageboard.store import MessageBoardStore

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mobile")

# Module-level dependencies, set by web_server.py
store: "CoralStore" = None  # type: ignore[assignment]
board_store: "MessageBoardStore" = None  # type: ignore[assignment]

# Night-heartbeat: server-side timers keyed by session_id. Survives mobile
# screen-lock — unlike the client-side desktop version in controls.js.
NIGHT_HEARTBEAT_INTERVAL_S = 30 * 60
NIGHT_HEARTBEAT_MSG = (
    "This is a night heartbeat. If u dont know what to do, multi model test end to end. "
    "If multi model dont exist, test end to end and make sure the night are worth your time. "
    "If u see this again after 30min, skip if needed"
)
_heartbeat_state: dict[str, dict] = {}


def _token_path() -> Path:
    return get_data_dir() / "mobile_token"


def get_or_create_token(rotate: bool = False) -> str:
    """Read the mobile token, generating it (with 0600 perms) if missing or rotating."""
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rotate and path.exists():
        try:
            tok = path.read_text().strip()
            if tok:
                return tok
        except OSError:
            pass
    tok = secrets.token_urlsafe(32)
    path.write_text(tok)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return tok


async def require_mobile_token(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency — rejects requests without a valid Bearer token."""
    expected = get_or_create_token()
    provided = ""
    if authorization and authorization.startswith("Bearer "):
        provided = authorization[len("Bearer "):].strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing mobile token")


def enumerate_lan_ips() -> list[str]:
    """Return all non-loopback IPv4 addresses on the host."""
    ips: list[str] = []
    try:
        import psutil
        for _iface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET and not addr.address.startswith("127."):
                    ips.append(addr.address)
    except ImportError:
        # Fallback: single best-guess via UDP trick
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
            s.close()
        except OSError:
            pass
    # Dedup, preserve order
    seen = set()
    unique = []
    for ip in ips:
        if ip not in seen:
            seen.add(ip)
            unique.append(ip)
    return unique


# ── Request models ───────────────────────────────────────────────────────

class SendRequest(BaseModel):
    command: str
    agent_type: str | None = None
    session_id: str | None = None


class HeartbeatToggleRequest(BaseModel):
    enabled: bool
    agent_type: str | None = None
    session_id: str | None = None


class KeysRequest(BaseModel):
    keys: list[str]
    agent_type: str | None = None


class PostBoardRequest(BaseModel):
    session_id: str
    content: str


# ── Slim session endpoints ──────────────────────────────────────────────

async def _build_slim_sessions() -> list[dict]:
    """Build phone-sized session list — no commands, no file lists, no git diff."""
    agents = await discover_coral_agents()
    session_ids = [a["session_id"] for a in agents if a.get("session_id")]
    display_names = await store.get_display_names(session_ids)
    icons = await store.get_icons(session_ids)
    latest_events = await store.get_latest_event_types(session_ids)
    latest_goals = await store.get_latest_goals(session_ids)

    # Board subscriptions keyed by tmux session name
    try:
        board_subs = await board_store.get_all_subscriptions()
    except Exception:
        board_subs = {}

    # Fallback for race condition — board_name stored in live_sessions DB
    live_board_names: dict[str, tuple[str, str]] = {}
    try:
        conn = await store._get_conn()
        rows = await (await conn.execute(
            "SELECT session_id, board_name, display_name FROM live_sessions WHERE board_name IS NOT NULL"
        )).fetchall()
        for row in rows:
            live_board_names[row["session_id"]] = (
                row["board_name"], row["display_name"] or ""
            )
    except Exception:
        pass

    results = []
    for agent in agents:
        log_path = agent["log_path"]
        from coral.tools.session_manager import get_log_status
        log_info = get_log_status(log_path)
        sid = agent.get("session_id")
        ev_tuple = latest_events.get(sid) if sid else None
        latest_ev = ev_tuple[0] if ev_tuple else None
        ev_summary = ev_tuple[1] if ev_tuple else None
        needs_input = latest_ev == "notification"
        done = latest_ev == "stop"
        working = latest_ev in ("tool_use", "prompt_submit")
        if working and ev_summary and ev_summary.startswith("Ran: sleep"):
            working = False
        if working and (log_info["staleness_seconds"] or 999) > 420:
            working = False

        summary = log_info["summary"]
        if not summary and sid:
            summary = latest_goals.get(sid)

        tmux_name = agent.get("tmux_session") or ""
        board_sub = board_subs.get(tmux_name)
        board_project = board_sub["project"] if board_sub else None
        board_job_title = board_sub["job_title"] if board_sub else None
        if not board_project and sid and sid in live_board_names:
            board_project, board_job_title = live_board_names[sid]

        results.append({
            "session_id": sid,
            "name": agent["agent_name"],
            "agent_type": agent["agent_type"],
            "display_name": display_names.get(sid) if sid else None,
            "icon": icons.get(sid) if sid else None,
            "status": log_info["status"],
            "summary": summary,
            "staleness_seconds": log_info["staleness_seconds"],
            "waiting_for_input": needs_input,
            "done": done,
            "working": working,
            "heartbeat_on": sid in _heartbeat_state if sid else False,
            "board_project": board_project,
            "board_job_title": board_job_title,
        })

    # Append sleeping sessions
    try:
        live_sids = {r["session_id"] for r in results if r.get("session_id")}
        all_live = await store.get_all_live_sessions()
        for sess in all_live:
            if not sess.get("is_sleeping"):
                continue
            sid = sess["session_id"]
            if sid in live_sids:
                continue
            results.append({
                "session_id": sid,
                "name": sess.get("agent_name", ""),
                "agent_type": sess.get("agent_type", "claude"),
                "display_name": sess.get("display_name"),
                "icon": sess.get("icon"),
                "status": "Sleeping",
                "summary": None,
                "staleness_seconds": None,
                "waiting_for_input": False,
                "done": False,
                "working": False,
                "heartbeat_on": False,
                "sleeping": True,
            })
    except Exception:
        log.debug("Mobile: failed to append sleeping sessions")

    return results


@router.get("/sessions", dependencies=[Depends(require_mobile_token)])
async def list_sessions():
    """Slim list of agents for the mobile home view."""
    return {"sessions": await _build_slim_sessions()}


@router.get("/sessions/{session_id}", dependencies=[Depends(require_mobile_token)])
async def session_detail(session_id: str, events_limit: int = Query(30, ge=1, le=100)):
    """Detail for one agent — pulse feed + recent events + snapshot."""
    agents = await discover_coral_agents()
    match = next((a for a in agents if a.get("session_id") == session_id), None)
    if not match:
        # Might be sleeping
        live = await store.get_all_live_sessions()
        sleeping = next((s for s in live if s.get("session_id") == session_id), None)
        if not sleeping:
            raise HTTPException(status_code=404, detail="Session not found")
        return {
            "session_id": session_id,
            "name": sleeping.get("agent_name"),
            "agent_type": sleeping.get("agent_type"),
            "display_name": sleeping.get("display_name"),
            "icon": sleeping.get("icon"),
            "sleeping": True,
            "status": "Sleeping",
            "summary": None,
            "events": [],
            "board_project": sleeping.get("board_name"),
        }

    log_path = match["log_path"]
    snapshot = get_log_snapshot(log_path)
    name = match["agent_name"]
    events = await store.list_agent_events(name, events_limit, session_id=session_id)
    pane_text = await capture_pane(name, agent_type=match["agent_type"], session_id=session_id)

    # Board + team members: resolve the board this agent is on, then
    # cross-join subscribers with live agents to get each teammate's status.
    board_project = None
    my_job_title = None
    tmux_name = match.get("tmux_session") or ""
    try:
        sub = await board_store.get_subscription(tmux_name)
        if sub:
            board_project = sub.get("project")
            my_job_title = sub.get("job_title")
    except Exception:
        pass
    if not board_project:
        try:
            prompt_info = await store.get_live_session_prompt_info(session_id)
            if prompt_info:
                board_project = prompt_info.get("board_name")
        except Exception:
            pass

    team_members: list[dict] = []
    if board_project:
        try:
            subs = await board_store.list_subscribers(board_project)
            # Map tmux_name -> live agent for quick lookup
            agent_by_tmux = {a.get("tmux_session") or "": a for a in agents}
            # Pull slim session info for live ones
            all_sids = [
                agent_by_tmux[s["session_id"]].get("session_id")
                for s in subs if s["session_id"] in agent_by_tmux
                and agent_by_tmux[s["session_id"]].get("session_id")
            ]
            all_latest = await store.get_latest_event_types(all_sids) if all_sids else {}
            all_display = await store.get_display_names(all_sids) if all_sids else {}
            for sub in subs:
                tx = sub["session_id"]
                a = agent_by_tmux.get(tx)
                if a:
                    a_sid = a.get("session_id")
                    ev = all_latest.get(a_sid) if a_sid else None
                    latest_ev = ev[0] if ev else None
                    team_members.append({
                        "session_id": a_sid,
                        "tmux_name": tx,
                        "job_title": sub.get("job_title"),
                        "receive_mode": sub.get("receive_mode"),
                        "name": a["agent_name"],
                        "agent_type": a.get("agent_type"),
                        "display_name": all_display.get(a_sid) if a_sid else None,
                        "waiting_for_input": latest_ev == "notification",
                        "working": latest_ev in ("tool_use", "prompt_submit"),
                        "done": latest_ev == "stop",
                        "alive": True,
                        "is_me": a_sid == session_id,
                    })
                else:
                    team_members.append({
                        "session_id": None,
                        "tmux_name": tx,
                        "job_title": sub.get("job_title"),
                        "receive_mode": sub.get("receive_mode"),
                        "name": tx,
                        "agent_type": None,
                        "display_name": None,
                        "waiting_for_input": False,
                        "working": False,
                        "done": False,
                        "alive": False,
                        "is_me": False,
                    })
        except Exception:
            log.exception("Failed to build team members for %s", board_project)

    return {
        "session_id": session_id,
        "name": name,
        "agent_type": match["agent_type"],
        "working_directory": match.get("working_directory", ""),
        "status": snapshot["status"],
        "summary": snapshot["summary"],
        "recent_lines": snapshot["recent_lines"],
        "staleness_seconds": snapshot["staleness_seconds"],
        "pane_capture": pane_text,
        "events": events,
        "heartbeat_on": session_id in _heartbeat_state,
        "board_project": board_project,
        "my_job_title": my_job_title,
        "team_members": team_members,
    }


@router.post("/sessions/{session_id}/keys", dependencies=[Depends(require_mobile_token)])
async def send_keys_to_session(session_id: str, body: KeysRequest):
    """Send raw tmux key names (Enter, y, 1, Escape, etc.)."""
    if not body.keys:
        raise HTTPException(status_code=400, detail="keys required")
    agents = await discover_coral_agents()
    match = next((a for a in agents if a.get("session_id") == session_id), None)
    if not match:
        raise HTTPException(status_code=404, detail="Session not found")
    err = await send_raw_keys(
        match["agent_name"], body.keys,
        agent_type=body.agent_type or match["agent_type"],
        session_id=session_id,
    )
    if err:
        raise HTTPException(status_code=500, detail=err)
    return {"ok": True}


@router.post("/sessions/{session_id}/send", dependencies=[Depends(require_mobile_token)])
async def send_to_session(session_id: str, body: SendRequest):
    command = (body.command or "").strip()
    if not command:
        raise HTTPException(status_code=400, detail="command required")

    agents = await discover_coral_agents()
    match = next((a for a in agents if a.get("session_id") == session_id), None)
    if not match:
        raise HTTPException(status_code=404, detail="Session not found")

    err = await send_to_tmux(
        match["agent_name"], command,
        agent_type=body.agent_type or match["agent_type"],
        session_id=session_id,
    )
    if err:
        raise HTTPException(status_code=500, detail=err)
    return {"ok": True}


# ── Server-side night-heartbeat ──────────────────────────────────────────

async def _resolve_heartbeat_config() -> tuple[int, str]:
    """Read night-heartbeat interval + prompt from user settings, with defensive fallback.

    Empty / whitespace-only / out-of-range values fall back to module defaults so a bad
    stored value can never turn the loop into a tight CPU burner or send an empty prompt.
    """
    interval_s = NIGHT_HEARTBEAT_INTERVAL_S
    msg = NIGHT_HEARTBEAT_MSG
    if store is None:
        return interval_s, msg
    try:
        settings = await store.get_settings()
    except Exception:
        return interval_s, msg
    raw_min = (settings.get("night_heartbeat_minutes") or "").strip()
    if raw_min:
        try:
            minutes = int(raw_min)
            if 1 <= minutes <= 1440:
                interval_s = minutes * 60
        except ValueError:
            pass
    raw_msg = (settings.get("night_heartbeat_prompt") or "").strip()
    if raw_msg:
        msg = raw_msg
    return interval_s, msg


async def _heartbeat_loop(session_id: str) -> None:
    """Send the night-heartbeat message on an interval until cancelled.

    Interval + prompt text are re-read from user settings at the top of each cycle, so
    edits made through the Settings modal take effect after the current sleep completes
    (no restart needed).
    """
    import asyncio
    while True:
        try:
            interval_s, msg = await _resolve_heartbeat_config()
            await asyncio.sleep(interval_s)
            agents = await discover_coral_agents()
            match = next((a for a in agents if a.get("session_id") == session_id), None)
            if not match:
                log.info("Night heartbeat: session %s gone, stopping", session_id[:8])
                break
            err = await send_to_tmux(
                match["agent_name"], msg,
                agent_type=match["agent_type"], session_id=session_id,
            )
            if err:
                log.warning("Night heartbeat send failed for %s: %s", session_id[:8], err)
            else:
                state = _heartbeat_state.get(session_id)
                if state:
                    state["last_sent_ts"] = int(time.time())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Night heartbeat loop error for %s", session_id[:8])


@router.post("/sessions/{session_id}/heartbeat-toggle", dependencies=[Depends(require_mobile_token)])
async def toggle_heartbeat(session_id: str, body: HeartbeatToggleRequest):
    import asyncio
    state = _heartbeat_state.get(session_id)
    if body.enabled:
        if state and not state["task"].done():
            return {"ok": True, "enabled": True, "already": True}
        task = asyncio.create_task(_heartbeat_loop(session_id))
        _heartbeat_state[session_id] = {
            "task": task,
            "started_ts": int(time.time()),
            "last_sent_ts": None,
        }
        return {"ok": True, "enabled": True}
    else:
        if state:
            state["task"].cancel()
            _heartbeat_state.pop(session_id, None)
        return {"ok": True, "enabled": False}


# ── Board (paginated read + post) ────────────────────────────────────────

@router.get("/board/{project}/messages", dependencies=[Depends(require_mobile_token)])
async def list_board_messages(
    project: str, limit: int = Query(50, ge=1, le=200),
    before_id: int | None = None,
):
    messages = await board_store.list_messages(project, limit, 0, before_id=before_id)
    return {"messages": messages}


@router.post("/board/{project}/messages", dependencies=[Depends(require_mobile_token)])
async def post_board_message(project: str, body: PostBoardRequest):
    if not (body.content or "").strip():
        raise HTTPException(status_code=400, detail="content required")
    message = await board_store.post_message(project, body.session_id, body.content)
    return message


# ── Token management ─────────────────────────────────────────────────────


@router.get("/info")
async def mobile_info(request: Request):
    """Return token + LAN URLs for the dashboard's Mobile Access modal.

    Localhost-only: a LAN device must already have the token to reach any
    other /api/mobile endpoint, so this one is the only path that can leak
    the token — restrict it to the same host the dashboard runs on.
    """
    from coral.mobile_gate import is_loopback_client
    client_host = request.client.host if request.client else None
    if not is_loopback_client(client_host):
        raise HTTPException(status_code=403, detail="Mobile info is localhost-only")
    port = request.url.port or (443 if request.url.scheme == "https" else 80)
    # Whether the server is actually bound to a non-loopback interface.
    # Set in web_server.main() after host resolution; fall back to env-sniff
    # if app.state wasn't populated (e.g. when the app is embedded in tests).
    lan_enabled = getattr(request.app.state, "lan_enabled", None)
    if lan_enabled is None:
        coral_host = os.environ.get("CORAL_HOST", "")
        mobile_env = os.environ.get("CORAL_MOBILE", "")
        lan_enabled = (
            mobile_env not in ("", "0", "false", "False")
            or (coral_host and coral_host not in ("127.0.0.1", "localhost", "::1"))
        )
    return {
        "token": get_or_create_token(),
        "lan_ips": enumerate_lan_ips(),
        "port": port,
        "lan_enabled": bool(lan_enabled),
    }


@router.post("/token/rotate", dependencies=[Depends(require_mobile_token)])
async def rotate_token():
    """Regenerate the mobile token. Caller must re-authenticate with the new token."""
    new_token = get_or_create_token(rotate=True)
    return {"ok": True, "token": new_token}
