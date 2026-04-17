/* Coral Mobile — phone UI for LAN access.
 *
 * Single-file SPA (no imports from the desktop SPA).
 * Hash-based router: #/ home, #/s/<session_id> detail, #/b/<project> board.
 * Polling transport with `fetchJson` as the single seam; upgrading to WS
 * later is a replacement of fetchJson callers, not the views.
 */

const TOKEN_KEY = "coral.mobile.token";
const POLL_HOME_MS = 8000;
const POLL_DETAIL_MS = 3000;
const POLL_BOARD_MS = 5000;
const CHAT_PAGE_SIZE = 50;

const root = document.getElementById("mobile-root");

// ── Token + fetch ─────────────────────────────────────────────────────

function getToken() { return localStorage.getItem(TOKEN_KEY) || ""; }
function setToken(tok) { localStorage.setItem(TOKEN_KEY, tok); }
function clearToken() { localStorage.removeItem(TOKEN_KEY); }

async function fetchJson(path, { method = "GET", body = null } = {}) {
    const headers = { "Authorization": "Bearer " + getToken() };
    if (body != null) headers["Content-Type"] = "application/json";
    const resp = await fetch(path, {
        method, headers,
        body: body == null ? null : JSON.stringify(body),
    });
    if (resp.status === 401) {
        clearToken();
        location.hash = "";
        renderLogin();
        throw new Error("Unauthorized");
    }
    if (!resp.ok) {
        const text = await resp.text().catch(() => "");
        throw new Error(`${resp.status}: ${text.slice(0, 200)}`);
    }
    return resp.json();
}

// ── Router ────────────────────────────────────────────────────────────

let _stopCurrentView = null;

function parseRoute() {
    const h = location.hash.replace(/^#\/?/, "");
    if (!h) return { name: "home" };
    if (h.startsWith("s/")) return { name: "detail", sessionId: decodeURIComponent(h.slice(2)) };
    if (h.startsWith("b/")) return { name: "board", project: decodeURIComponent(h.slice(2)) };
    return { name: "home" };
}

function navigate() {
    if (_stopCurrentView) { _stopCurrentView(); _stopCurrentView = null; }
    if (!getToken()) { renderLogin(); return; }
    const route = parseRoute();
    if (route.name === "home") renderHome();
    else if (route.name === "detail") renderDetail(route.sessionId);
    else if (route.name === "board") renderBoard(route.project);
}

window.addEventListener("hashchange", navigate);

// ── Helpers ───────────────────────────────────────────────────────────

function el(tag, attrs = {}, ...children) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
        if (k === "class") e.className = v;
        else if (k === "on") for (const [ev, fn] of Object.entries(v)) e.addEventListener(ev, fn);
        else if (v === true) e.setAttribute(k, "");
        else if (v !== false && v != null) e.setAttribute(k, v);
    }
    for (const c of children) {
        if (c == null) continue;
        e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return e;
}

function clear() { root.replaceChildren(); }

function statusDotClass(s) {
    if (s.sleeping) return "m-dot sleeping";
    if (s.waiting_for_input) return "m-dot waiting";
    if (s.working) return "m-dot working";
    if (s.done) return "m-dot done";
    return "m-dot";
}

function displayName(s) {
    return s.display_name || s.name || s.session_id?.slice(0, 8) || "(agent)";
}

function fmtWhen(ts) {
    if (ts == null || ts === "") return "";
    let d;
    if (typeof ts === "number") {
        d = new Date(ts < 1e12 ? ts * 1000 : ts);
    } else {
        d = new Date(ts);
    }
    if (isNaN(d.getTime())) return "";
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    return sameDay
        ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
        : d.toLocaleString([], { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

let _pollPaused = false;
document.addEventListener("visibilitychange", () => { _pollPaused = document.hidden; });

function startPoll(intervalMs, fn) {
    let stopped = false;
    let failStreak = 0;
    async function tick() {
        if (stopped) return;
        if (_pollPaused) { setTimeout(tick, 1000); return; }
        try {
            await fn();
            failStreak = 0;
        } catch (e) {
            failStreak += 1;
            console.warn("[mobile] poll failed", e);
        }
        const backoff = Math.min(intervalMs * Math.pow(1.5, failStreak), 30000);
        setTimeout(tick, failStreak ? backoff : intervalMs);
    }
    tick();
    return () => { stopped = true; };
}

// ── Login ─────────────────────────────────────────────────────────────

function renderLogin() {
    clear();
    const err = el("div", { class: "err" });
    const input = el("input", { type: "password", placeholder: "Paste mobile token", autocomplete: "off" });
    const btn = el("button", {}, "Unlock");
    btn.addEventListener("click", async () => {
        const tok = input.value.trim();
        if (!tok) { err.textContent = "Token required"; return; }
        setToken(tok);
        try {
            await fetchJson("/api/mobile/sessions");
            location.hash = "";
            navigate();
        } catch (e) {
            clearToken();
            err.textContent = "Invalid token";
        }
    });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") btn.click(); });
    const wrap = el("div", { class: "m-login" },
        el("h2", {}, "Coral Mobile"),
        el("p", {}, "Paste the mobile token printed in the Coral server console."),
        input, btn, err,
    );
    root.appendChild(wrap);
    setTimeout(() => input.focus(), 50);
}

// ── Home ──────────────────────────────────────────────────────────────

async function renderHome() {
    clear();
    const header = el("div", { class: "m-header" },
        el("h1", {}, "Coral"),
        el("button", {
            class: "action",
            on: { click: () => { if (confirm("Log out?")) { clearToken(); renderLogin(); } } },
        }, "Logout"),
    );
    const list = el("ul", { class: "m-list" });
    const spinner = el("div", { class: "m-spinner" }, "Loading…");
    root.append(header, list, spinner);

    function renderSessionLi(s) {
        const dot = el("span", { class: statusDotClass(s) });
        const isOrch = (s.board_job_title || "").toLowerCase().includes("orchestr");
        const title = el("div", { class: "m-item-title" },
            displayName(s),
            s.board_job_title ? el("span", { class: "m-role" }, s.board_job_title) : null,
        );
        const sub = el("div", { class: "m-item-sub" }, s.summary || s.status || "(idle)");
        const meta = el("div", { class: "m-item-meta" });
        meta.appendChild(el("span", { class: "m-badge" }, s.agent_type || "?"));
        if (isOrch) meta.appendChild(el("span", { class: "m-badge orch" }, "orch"));
        if (s.heartbeat_on) meta.appendChild(el("span", { class: "m-badge heartbeat" }, "heartbeat"));
        if (s.waiting_for_input) meta.appendChild(el("span", { class: "m-badge input" }, "input"));
        const body = el("div", { class: "m-item-body" }, title, sub, meta);
        return el("li", {
            on: { click: () => { location.hash = "#/s/" + encodeURIComponent(s.session_id); } },
        }, dot, body);
    }

    async function refresh() {
        const data = await fetchJson("/api/mobile/sessions");
        const sessions = data.sessions || [];
        list.replaceChildren();
        if (!sessions.length) {
            list.appendChild(el("div", { class: "m-empty" }, "No live agents."));
            spinner.remove();
            return;
        }

        // Group by board_project. Orchestrator first within a team; solo at the bottom.
        const teams = new Map();
        const solo = [];
        for (const s of sessions) {
            if (s.board_project) {
                if (!teams.has(s.board_project)) teams.set(s.board_project, []);
                teams.get(s.board_project).push(s);
            } else {
                solo.push(s);
            }
        }
        for (const [proj, members] of teams) {
            members.sort((a, b) => {
                const aO = (a.board_job_title || "").toLowerCase().includes("orchestr") ? 0 : 1;
                const bO = (b.board_job_title || "").toLowerCase().includes("orchestr") ? 0 : 1;
                if (aO !== bO) return aO - bO;
                return displayName(a).localeCompare(displayName(b));
            });
            const header = el("li", { class: "m-team-header" },
                el("span", { class: "m-team-icon" }, "▸"),
                el("span", {}, proj),
                el("span", { class: "m-team-count" }, String(members.length)),
                el("button", {
                    class: "m-team-board-btn",
                    on: { click: (e) => {
                        e.stopPropagation();
                        location.hash = "#/b/" + encodeURIComponent(proj);
                    } },
                }, "Board"),
            );
            list.appendChild(header);
            for (const s of members) list.appendChild(renderSessionLi(s));
        }
        if (solo.length) {
            if (teams.size) list.appendChild(el("li", { class: "m-team-header solo" },
                el("span", {}, "Solo"),
            ));
            for (const s of solo) list.appendChild(renderSessionLi(s));
        }
        spinner.remove();
    }

    try { await refresh(); } catch (e) {
        spinner.remove();
        root.appendChild(el("div", { class: "m-error" }, String(e.message || e)));
        return;
    }
    _stopCurrentView = startPoll(POLL_HOME_MS, refresh);
}

// ── Detail ────────────────────────────────────────────────────────────

async function renderDetail(sessionId) {
    clear();
    const back = el("button", { class: "back", on: { click: () => { location.hash = ""; } } }, "←");
    const title = el("h1", {}, "Agent");
    const boardBtn = el("button", { class: "action" }, "Board");
    boardBtn.style.display = "none";
    const header = el("div", { class: "m-header" }, back, title, boardBtn);

    const detailHeader = el("div", { class: "m-detail-header" });
    const teamRow = el("div", { class: "m-team-row" });
    teamRow.style.display = "none";
    const quick = el("div", { class: "m-quick" });
    const pane = el("pre", { class: "m-pane" });
    const feedToggle = el("button", { class: "m-feed-toggle" }, "Hide event log");
    const feed = el("div", { class: "m-events" });
    feedToggle.addEventListener("click", () => {
        const shown = feed.style.display !== "none";
        feed.style.display = shown ? "none" : "";
        feedToggle.textContent = shown ? "Show event log" : "Hide event log";
    });

    const ta = el("textarea", { placeholder: "Send to agent…", rows: "1" });
    const sendBtn = el("button", {}, "Send");
    const inputBar = el("div", { class: "m-input-bar" }, ta, sendBtn);
    const wrap = el("div", { class: "m-detail" }, header, detailHeader, teamRow, quick, pane, feedToggle, feed, inputBar);
    root.appendChild(wrap);

    let current = null;

    async function doSend() {
        const command = ta.value.trim();
        if (!command) return;
        sendBtn.disabled = true;
        try {
            await fetchJson(`/api/mobile/sessions/${encodeURIComponent(sessionId)}/send`, {
                method: "POST",
                body: { command, agent_type: current?.agent_type, session_id: sessionId },
            });
            ta.value = "";
        } catch (e) {
            alert("Send failed: " + e.message);
        } finally {
            sendBtn.disabled = false;
        }
    }
    sendBtn.addEventListener("click", doSend);
    ta.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); doSend(); }
    });

    function buildQuick(detail) {
        quick.replaceChildren();
        const makeBtn = (label, fn, extraClass = "") => {
            const b = el("button", { class: extraClass, on: { click: fn } }, label);
            return b;
        };
        const cmdSend = async (cmd) => {
            try {
                await fetchJson(`/api/mobile/sessions/${encodeURIComponent(sessionId)}/send`, {
                    method: "POST",
                    body: { command: cmd, agent_type: detail.agent_type, session_id: sessionId },
                });
            } catch (e) { alert("Send failed: " + e.message); }
        };
        const keySend = async (keys) => {
            try {
                await fetchJson(`/api/mobile/sessions/${encodeURIComponent(sessionId)}/keys`, {
                    method: "POST",
                    body: { keys, agent_type: detail.agent_type },
                });
            } catch (e) { alert("Key send failed: " + e.message); }
        };
        quick.appendChild(makeBtn("Enter", () => keySend(["Enter"])));
        quick.appendChild(makeBtn("y", () => keySend(["y", "Enter"])));
        quick.appendChild(makeBtn("1", () => keySend(["1", "Enter"])));
        quick.appendChild(makeBtn("2", () => keySend(["2", "Enter"])));
        quick.appendChild(makeBtn("ESC", () => keySend(["Escape"])));
        quick.appendChild(makeBtn("Mode", () => cmdSend("/mode")));
        quick.appendChild(makeBtn("Bash", () => cmdSend("!")));
        quick.appendChild(makeBtn("Rewind", () => cmdSend("/rewind")));
        const hb = makeBtn(
            detail.heartbeat_on ? "♡ Heartbeat On" : "♡ Heartbeat",
            async () => {
                try {
                    const r = await fetchJson(
                        `/api/mobile/sessions/${encodeURIComponent(sessionId)}/heartbeat-toggle`,
                        { method: "POST", body: { enabled: !detail.heartbeat_on } },
                    );
                    detail.heartbeat_on = !!r.enabled;
                    buildQuick(detail);
                } catch (e) { alert("Heartbeat toggle failed: " + e.message); }
            },
            detail.heartbeat_on ? "is-on" : "",
        );
        quick.appendChild(hb);
    }

    function renderDetailPayload(d) {
        current = d;
        title.textContent = displayName(d);
        const stale = d.staleness_seconds;
        const staleText = stale != null ? ` · idle ${Math.round(stale)}s` : "";
        detailHeader.replaceChildren(
            el("div", { class: "m-detail-title" }, (d.status || "(no status)") + staleText),
            el("div", { class: "m-detail-sub" }, d.summary || "(no pulse summary)"),
        );
        buildQuick(d);

        // Pane capture — what the agent terminal currently shows. This is
        // where the actual question/prompt appears when "waiting for input".
        const paneText = d.pane_capture || "(no pane capture — agent may be sleeping)";
        const wasAtBottom = pane.scrollTop + pane.clientHeight > pane.scrollHeight - 20;
        pane.textContent = paneText;
        if (wasAtBottom) pane.scrollTop = pane.scrollHeight;

        feed.replaceChildren();
        const events = (d.events || []).slice().reverse();
        if (!events.length) {
            feed.appendChild(el("div", { class: "m-empty" }, "No recent events."));
        } else {
            for (const ev of events) {
                const kind = ev.event_type || ev.type || "event";
                const body = ev.summary || ev.event_text || ev.text || ev.status || "";
                const node = el("div", { class: "m-event kind-" + kind },
                    el("div", { class: "t" }, `${kind} · ${fmtWhen(ev.created_at || ev.timestamp)}`),
                    el("div", {}, body),
                );
                feed.appendChild(node);
            }
        }
        if (d.board_project) {
            boardBtn.textContent = "Board · " + d.board_project;
            boardBtn.style.display = "";
            boardBtn.onclick = () => { location.hash = "#/b/" + encodeURIComponent(d.board_project); };
        }

        // Team members row — horizontally scrollable, tap to jump.
        // Sort: me → alive orchestrators → alive others → stale. Stale chips
        // are greyed out but still visible for context.
        teamRow.replaceChildren();
        const members = (d.team_members || []).slice();
        if (members.length) {
            members.sort((a, b) => {
                if (a.is_me !== b.is_me) return a.is_me ? -1 : 1;
                if (a.alive !== b.alive) return a.alive ? -1 : 1;
                const aO = (a.job_title || "").toLowerCase().includes("orchestr") ? 0 : 1;
                const bO = (b.job_title || "").toLowerCase().includes("orchestr") ? 0 : 1;
                return aO - bO;
            });
            const aliveCount = members.filter(m => m.alive).length;
            teamRow.appendChild(el("div", { class: "m-team-label" },
                "Team · " + (d.board_project || ""),
                el("span", { class: "m-role" }, `${aliveCount} of ${members.length} alive`),
                d.my_job_title ? el("span", { class: "m-role" }, "you: " + d.my_job_title) : null,
            ));
            const strip = el("div", { class: "m-team-strip" });
            for (const m of members) {
                const dotCls = !m.alive ? "m-dot sleeping"
                    : m.waiting_for_input ? "m-dot waiting"
                    : m.working ? "m-dot working"
                    : m.done ? "m-dot done" : "m-dot";
                const isOrch = (m.job_title || "").toLowerCase().includes("orchestr");
                const chip = el("button", {
                    class: "m-team-chip" + (m.is_me ? " me" : "") + (isOrch ? " orch" : ""),
                    disabled: !m.alive || !m.session_id,
                    on: {
                        click: () => {
                            if (m.session_id && !m.is_me) {
                                location.hash = "#/s/" + encodeURIComponent(m.session_id);
                            }
                        },
                    },
                },
                    el("span", { class: dotCls }),
                    el("span", { class: "m-team-chip-name" }, m.display_name || m.name || m.job_title || "?"),
                    el("span", { class: "m-team-chip-role" }, m.job_title || ""),
                );
                strip.appendChild(chip);
            }
            teamRow.appendChild(strip);
            teamRow.style.display = "";
        } else {
            teamRow.style.display = "none";
        }
    }

    async function refresh() {
        const d = await fetchJson(`/api/mobile/sessions/${encodeURIComponent(sessionId)}`);
        renderDetailPayload(d);
    }

    try { await refresh(); } catch (e) {
        root.appendChild(el("div", { class: "m-error" }, String(e.message || e)));
        return;
    }
    _stopCurrentView = startPoll(POLL_DETAIL_MS, refresh);
}

// ── Board ─────────────────────────────────────────────────────────────

async function renderBoard(project) {
    clear();
    const back = el("button", { class: "back", on: { click: () => history.back() } }, "←");
    const title = el("h1", {}, "Board · " + project);
    const header = el("div", { class: "m-header" }, back, title);
    const msgs = el("div", { class: "m-board-messages" });
    const loadMore = el("button", { class: "m-load-more" }, "Load older");
    loadMore.style.display = "none";

    const ta = el("textarea", { placeholder: "Post to board…", rows: "1" });
    const sendBtn = el("button", {}, "Post");
    const inputBar = el("div", { class: "m-input-bar" }, ta, sendBtn);
    const wrap = el("div", { class: "m-board" }, header, msgs, inputBar);
    root.appendChild(wrap);

    let seen = new Map();
    let oldestId = null;

    function fmtMsg(m) {
        return el("div", { class: "m-message" },
            el("div", { class: "who" }, m.job_title || m.session_id?.slice(0, 8) || "unknown"),
            el("div", { class: "when" }, new Date(m.created_at).toLocaleString()),
            el("div", { class: "body" }, m.content || ""),
        );
    }

    async function refresh() {
        const data = await fetchJson(`/api/mobile/board/${encodeURIComponent(project)}/messages?limit=${CHAT_PAGE_SIZE}`);
        const list = data.messages || [];
        list.sort((a, b) => a.id - b.id);
        const atBottom = msgs.scrollTop + msgs.clientHeight > msgs.scrollHeight - 40;
        for (const m of list) {
            if (seen.has(m.id)) continue;
            seen.set(m.id, true);
            msgs.appendChild(fmtMsg(m));
            if (oldestId == null || m.id < oldestId) oldestId = m.id;
        }
        loadMore.style.display = list.length >= CHAT_PAGE_SIZE ? "" : "none";
        if (atBottom) msgs.scrollTop = msgs.scrollHeight;
    }

    loadMore.addEventListener("click", async () => {
        if (oldestId == null) return;
        try {
            const data = await fetchJson(
                `/api/mobile/board/${encodeURIComponent(project)}/messages?limit=${CHAT_PAGE_SIZE}&before_id=${oldestId}`,
            );
            const older = (data.messages || []).slice().sort((a, b) => a.id - b.id);
            if (!older.length) { loadMore.style.display = "none"; return; }
            const frag = document.createDocumentFragment();
            for (const m of older) {
                if (seen.has(m.id)) continue;
                seen.set(m.id, true);
                frag.appendChild(fmtMsg(m));
                if (m.id < oldestId) oldestId = m.id;
            }
            msgs.insertBefore(frag, msgs.firstChild);
            if (older.length < CHAT_PAGE_SIZE) loadMore.style.display = "none";
        } catch (e) { alert("Load older failed: " + e.message); }
    });

    async function doPost() {
        const content = ta.value.trim();
        if (!content) return;
        sendBtn.disabled = true;
        try {
            // Post as a "mobile" pseudo-session identity
            const sid = "mobile-" + (getToken().slice(0, 8) || "user");
            await fetchJson(`/api/mobile/board/${encodeURIComponent(project)}/messages`, {
                method: "POST",
                body: { session_id: sid, content },
            });
            ta.value = "";
            await refresh();
        } catch (e) {
            alert("Post failed: " + e.message);
        } finally {
            sendBtn.disabled = false;
        }
    }
    sendBtn.addEventListener("click", doPost);
    ta.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); doPost(); }
    });

    // Prepend load-more above messages list
    msgs.parentNode.insertBefore(loadMore, msgs);

    try { await refresh(); } catch (e) {
        root.appendChild(el("div", { class: "m-error" }, String(e.message || e)));
        return;
    }
    _stopCurrentView = startPoll(POLL_BOARD_MS, refresh);
}

// ── Boot ──────────────────────────────────────────────────────────────
navigate();
