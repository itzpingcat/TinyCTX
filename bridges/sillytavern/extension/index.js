/**
 * TinyCTX — SillyTavern Extension
 * ================================
 * Thin client for a TinyCTX gateway. One fixed session ID (configurable),
 * whose versions are the "chat list". ST actions map transparently:
 *
 *   Send            → POST   /v1/sessions/{id}/message  (stream)
 *   Regenerate/Swipe→ DELETE /history/{last_assistant_eid}
 *                     + PUT  /generation  (stream)
 *   Continue        → PUT    /generation  (no history mutation)
 *   Abort           → DELETE /generation
 *   Edit message    → PATCH  /history/{eid}
 *   Delete message  → DELETE /history/{eid}
 *   Start new chat  → POST   /sessions/{id}/next  (bump version)
 *   Rename chat     → PATCH  /sessions/{id}/rename
 *   Manage chats    → version list from GET /sessions/{id}/versions
 *   Impersonate     → ST native (interceptor returns undefined)
 *
 * Card fields sync to gateway workspace:
 *   description → AGENTS.md
 *   personality → SOUL.md
 *   scenario    → MEMORY.md
 */

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const MODULE_NAME = "tinyctx";
const CHAR_NAME   = "TinyCTX Agent";

const FIELD_MAP = {
    description: "AGENTS.md",
    personality:  "SOUL.md",
    scenario:     "MEMORY.md",
};

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

const DEFAULT_SETTINGS = Object.freeze({
    endpoint:         "http://127.0.0.1:8080",
    api_key:          "",
    session_id:       "sillytavern",
    show_tool_events: true,
    sync_card_fields: true,
});

function getSettings() {
    const { extensionSettings } = SillyTavern.getContext();
    if (!extensionSettings[MODULE_NAME]) {
        extensionSettings[MODULE_NAME] = structuredClone(DEFAULT_SETTINGS);
    }
    for (const key of Object.keys(DEFAULT_SETTINGS)) {
        if (!Object.hasOwn(extensionSettings[MODULE_NAME], key)) {
            extensionSettings[MODULE_NAME][key] = DEFAULT_SETTINGS[key];
        }
    }
    return extensionSettings[MODULE_NAME];
}

function saveSettings() {
    SillyTavern.getContext().saveSettingsDebounced();
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let isTinyCTXActive = false;
let healthPollTimer = null;

// ---------------------------------------------------------------------------
// Gateway helpers
// ---------------------------------------------------------------------------

function sid() { return getSettings().session_id || "sillytavern"; }

function gw(path) {
    return getSettings().endpoint.replace(/\/$/, "") + path;
}

function gwHeaders() {
    const h = { "Content-Type": "application/json" };
    const k = getSettings().api_key;
    if (k) h["Authorization"] = `Bearer ${k}`;
    return h;
}

async function gwFetch(path, opts = {}) {
    return fetch(gw(path), { headers: gwHeaders(), ...opts });
}

async function gwHealth() {
    try {
        const r = await fetch(gw("/v1/health"), { signal: AbortSignal.timeout(3000) });
        return r.ok ? await r.json() : null;
    } catch { return null; }
}

// ---------------------------------------------------------------------------
// Status bar
// ---------------------------------------------------------------------------

function setStatus(text, cls = "idle") {
    document.querySelector(".tinyctx-dot")?.setAttribute("data-state", cls);
    const el = document.getElementById("tinyctx-status-text");
    if (el) el.textContent = text;
}

// ---------------------------------------------------------------------------
// Workspace sync
// ---------------------------------------------------------------------------

async function syncField(field, value) {
    if (!getSettings().sync_card_fields || value == null) return;
    const file = FIELD_MAP[field];
    if (!file) return;
    try {
        await gwFetch(`/v1/workspace/files/${file}`, {
            method: "PUT",
            body: JSON.stringify({ content: value }),
        });
    } catch (e) {
        console.warn(`[TinyCTX] sync ${field}→${file} failed:`, e);
    }
}

async function syncAllFields() {
    const { characters, characterId } = SillyTavern.getContext();
    const char = characters?.[characterId];
    if (!char) return;
    for (const field of Object.keys(FIELD_MAP)) {
        await syncField(field, char[field] ?? char.data?.[field] ?? "");
    }
}

/**
 * Pull workspace files back into the character card fields.
 * Called once on activate so gateway edits to SOUL/AGENTS/MEMORY.md
 * (e.g. by the agent itself) are reflected in the ST character card.
 */
async function pullFieldsFromGateway() {
    if (!getSettings().sync_card_fields) return;
    const { characters, characterId, writeExtensionField } = SillyTavern.getContext();
    const char = characters?.[characterId];
    if (!char) return;

    for (const [field, file] of Object.entries(FIELD_MAP)) {
        try {
            const r = await gwFetch(`/v1/workspace/files/${file}`);
            if (!r.ok) continue;
            const { content } = await r.json();
            if (typeof content !== "string") continue;
            // Update the in-memory character object so ST re-renders the card
            char[field] = content;
            if (char.data) char.data[field] = content;
        } catch (e) {
            console.warn(`[TinyCTX] pull ${file}→${field} failed:`, e);
        }
    }
}

// ---------------------------------------------------------------------------
// SSE reader
// ---------------------------------------------------------------------------

async function* readSSE(response) {
    const reader  = response.body.getReader();
    const decoder = new TextDecoder();
    let   buf     = "";
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split("\n");
        buf = lines.pop();
        for (const line of lines) {
            if (!line.startsWith("data:")) continue;
            const raw = line.slice(5).trim();
            if (!raw || raw === "[DONE]") continue;
            try { yield JSON.parse(raw); } catch { /* skip malformed */ }
        }
    }
}

// ---------------------------------------------------------------------------
// Stream a generation from an already-started fetch response.
// Collects tool_call/tool_result events and injects them as ST-native
// tool messages. Returns the final assistant text.
// ---------------------------------------------------------------------------

async function streamGeneration(response) {
    if (response.status === 429) {
        setStatus("Busy — retrying…", "connecting");
        await new Promise(r => setTimeout(r, 1500));
        return null; // caller should retry
    }
    if (!response.ok) {
        setStatus(`Error ${response.status}`, "err");
        throw new Error(`Gateway ${response.status}`);
    }

    const ctx         = SillyTavern.getContext();
    const showTools   = getSettings().show_tool_events;
    let   accumulated = "";
    // Collect tool events for one "call + result" pair, then inject into ST chat
    let   pendingCall  = null;

    for await (const ev of readSSE(response)) {
        switch (ev.type) {
            case "text_chunk":
                accumulated += ev.text;
                break;

            case "text_final":
                if (ev.text) accumulated = ev.text;
                break;

            case "tool_call":
                if (showTools) {
                    // Inject a tool_call message into ST's chat natively
                    pendingCall = ev;
                    await ctx.addOneMessage({
                        is_user:   false,
                        name:      CHAR_NAME,
                        mes:       "",
                        send_date: Date.now(),
                        extra: {
                            type: "tool_calls",
                            tool_calls: [{
                                id:       ev.call_id,
                                type:     "function",
                                function: { name: ev.name, arguments: JSON.stringify(ev.args ?? {}) },
                            }],
                        },
                    }, { scroll: false, render: true });
                }
                break;

            case "tool_result":
                if (showTools && pendingCall) {
                    // Inject a tool result message
                    await ctx.addOneMessage({
                        is_user:   false,
                        name:      CHAR_NAME,
                        mes:       "",
                        send_date: Date.now(),
                        extra: {
                            type: "tool_result",
                            tool_call_id: ev.call_id,
                            tool_name:    ev.name,
                            result:       ev.output,
                            is_error:     ev.is_error,
                        },
                    }, { scroll: false, render: true });
                    pendingCall = null;
                }
                break;

            case "error":
                setStatus(`Agent error: ${ev.message}`, "err");
                break;

            case "done":
                setStatus(`Session: ${sid()}`, "ok");
                break;
        }
    }

    return accumulated;
}

// ---------------------------------------------------------------------------
// Get last assistant entry id from gateway history
// ---------------------------------------------------------------------------

async function getLastAssistantEntryId() {
    try {
        const r = await gwFetch(`/v1/sessions/${encodeURIComponent(sid())}/history`);
        if (!r.ok) return null;
        const entries = await r.json();
        for (let i = entries.length - 1; i >= 0; i--) {
            if (entries[i].role === "assistant" && typeof entries[i].content === "string") {
                return entries[i].id;
            }
        }
    } catch {}
    return null;
}

// ---------------------------------------------------------------------------
// Load a version's history into ST's chat array
// ---------------------------------------------------------------------------

async function loadVersionIntoST(version) {
    const ctx = SillyTavern.getContext();
    if (!ctx?.chat) return;

    const url = version != null
        ? `/v1/sessions/${encodeURIComponent(sid())}/history?version=${version}`
        : `/v1/sessions/${encodeURIComponent(sid())}/history`;

    try {
        const r = await gwFetch(url);
        if (!r.ok) return;
        const entries = await r.json();

        ctx.chat.length = 0;
        for (const e of entries) {
            if (e.role === "user" && typeof e.content === "string") {
                ctx.chat.push({
                    is_user:   true,
                    name:      ctx.name1 ?? "You",
                    mes:       e.content,
                    send_date: Date.now(),
                    extra:     { tinyctx_entry_id: e.id },
                });
            } else if (e.role === "assistant" && typeof e.content === "string" && e.content) {
                ctx.chat.push({
                    is_user:   false,
                    name:      CHAR_NAME,
                    mes:       e.content,
                    send_date: Date.now(),
                    extra:     { tinyctx_entry_id: e.id },
                });
            }
            // tool_call / tool_result / system entries skipped — they're ephemeral UI
        }

        if (typeof ctx.reloadCurrentChat === "function") {
            await ctx.reloadCurrentChat();
        }
    } catch (e) {
        console.warn("[TinyCTX] loadVersionIntoST failed:", e);
    }
}

// ---------------------------------------------------------------------------
// Version panel (replaces ST's "manage chat files" for TinyCTX char)
// ---------------------------------------------------------------------------

const VERSION_PANEL_HTML = `
<div id="tinyctx-version-panel" style="display:none;">
    <div class="tinyctx-session-header">
        <span>Chat history</span>
        <button id="tinyctx-new-chat-btn" title="Start new chat">＋ New</button>
    </div>
    <div id="tinyctx-version-list"></div>
</div>
`;

function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

async function renderVersionPanel() {
    const list = document.getElementById("tinyctx-version-list");
    if (!list) return;

    let versions = [];
    let activeV  = null;
    try {
        const r = await gwFetch(`/v1/sessions/${encodeURIComponent(sid())}/versions`);
        if (r.ok) {
            const data = await r.json();
            versions   = data.versions ?? [];
            activeV    = data.active_version;
        }
    } catch {}

    list.innerHTML = "";

    if (!versions.length) {
        list.innerHTML = `<div class="tinyctx-session-empty">No history yet</div>`;
        return;
    }

    // Show newest first
    for (const v of [...versions].reverse()) {
        const item = document.createElement("div");
        item.className = "tinyctx-session-item" + (v === activeV ? " active" : "");
        item.innerHTML = `
            <span class="tinyctx-session-icon">💬</span>
            <span class="tinyctx-session-name">Chat ${v}</span>
            ${v === activeV ? '<span class="tinyctx-session-turns">current</span>' : ''}
        `;
        item.addEventListener("click", () => switchVersion(v));
        list.appendChild(item);
    }
}

async function switchVersion(version) {
    setStatus(`Loading chat ${version}…`, "connecting");
    await loadVersionIntoST(version);
    await renderVersionPanel();
    setStatus(`Session: ${sid()} v${version}`, "ok");
}

function showVersionPanel() {
    document.getElementById("tinyctx-version-panel")?.style.setProperty("display", "");
}

function hideVersionPanel() {
    document.getElementById("tinyctx-version-panel")?.style.setProperty("display", "none");
}

// ---------------------------------------------------------------------------
// Generate interceptor  (manifest: generate_interceptor: "tinyCTXIntercept")
//
// ST calls tinyCTXIntercept(chat, type) before generation.
// Returning a string → ST uses it as the assistant reply.
// Returning undefined → ST proceeds normally.
//
// Types we handle: "normal", "regenerate", "continue"
// Types we pass through: "impersonate", "quiet"
// ---------------------------------------------------------------------------

window.tinyCTXIntercept = async function(chat, type) {
    if (!isTinyCTXActive) return;
    if (type === "impersonate" || type === "quiet") return;

    const sessionId = sid();
    setStatus(`Generating…`, "connecting");

    try {
        if (type === "regenerate") {
            // 1. Delete the last assistant entry only (no cascading back through tool turns)
            const eid = await getLastAssistantEntryId();
            if (eid) {
                await gwFetch(
                    `/v1/sessions/${encodeURIComponent(sessionId)}/history/${eid}`,
                    { method: "DELETE" }
                );
            }
            // 2. Queue a new generation against existing context
            const r = await fetch(gw(`/v1/sessions/${encodeURIComponent(sessionId)}/generation`), {
                method:  "PUT",
                headers: gwHeaders(),
                body:    JSON.stringify({ stream: true }),
            });
            return await streamGeneration(r) ?? "[TinyCTX: no response]";
        }

        if (type === "continue") {
            // Send ST's configured continue nudge prompt as a normal user message.
            // This is the string the user set in ST's Advanced Formatting settings.
            const nudge = document.getElementById("continue_nudge_prompt_textarea")?.value?.trim()
                ?? "[Continue your last message without repeating its original content.]";
            const r = await fetch(gw(`/v1/sessions/${encodeURIComponent(sessionId)}/message`), {
                method:  "POST",
                headers: gwHeaders(),
                body:    JSON.stringify({ text: nudge, stream: true, session_type: "dm" }),
            });
            return await streamGeneration(r) ?? "[TinyCTX: no response]";
        }

        // Normal send
        const lastUser = [...chat].reverse().find(m => m.is_user);
        if (!lastUser) return;
        const text = lastUser.mes?.trim();
        if (!text) return;

        const r = await fetch(gw(`/v1/sessions/${encodeURIComponent(sessionId)}/message`), {
            method:  "POST",
            headers: gwHeaders(),
            body:    JSON.stringify({ text, stream: true, session_type: "dm" }),
        });
        return await streamGeneration(r) ?? "[TinyCTX: no response]";

    } catch (e) {
        console.error("[TinyCTX] intercept error:", e);
        setStatus("Error", "err");
        return `[TinyCTX error: ${e.message}]`;
    }
};

// ---------------------------------------------------------------------------
// ST event hooks
// ---------------------------------------------------------------------------

async function onChatChanged() {
    const { characters, characterId } = SillyTavern.getContext();
    const char = characters?.[characterId];

    if (char?.name !== CHAR_NAME) {
        if (isTinyCTXActive) deactivate();
        return;
    }

    if (!isTinyCTXActive) await activate();

    // Load current version's history into ST's chat array
    await loadVersionIntoST(null);
    await renderVersionPanel();
}


async function onMessageEdited(messageId) {
    if (!isTinyCTXActive) return;
    const { chat } = SillyTavern.getContext();
    const msg      = chat?.[messageId];
    if (!msg) return;
    const entryId  = msg.extra?.tinyctx_entry_id;
    if (!entryId) return;
    await gwFetch(
        `/v1/sessions/${encodeURIComponent(sid())}/history/${entryId}`,
        { method: "PATCH", body: JSON.stringify({ content: msg.mes }) }
    );
}

async function onMessageDeleted(messageId) {
    if (!isTinyCTXActive) return;
    // ST fires this after removal — we need the entry id we cached before
    // We store it on the message object in extra.tinyctx_entry_id during load.
    // By the time this fires the message is gone from ctx.chat, so we track it
    // in a pre-delete hook instead (see bindDeleteHook).
}

// Wire up pre-delete so we can grab the entry id before ST removes the message
function bindDeleteHook() {
    // ST's delete button fires a click on .mes_block .del_mes_but
    // We intercept via event delegation on #chat
    const chatEl = document.getElementById("chat");
    if (!chatEl || chatEl.dataset.tinyCTXDelete) return;
    chatEl.dataset.tinyCTXDelete = "1";

    chatEl.addEventListener("click", async (e) => {
        if (!isTinyCTXActive) return;
        const btn = e.target.closest(".mes_delete, [title='Delete message']");
        if (!btn) return;
        const mesEl  = btn.closest(".mes");
        if (!mesEl) return;
        const mesId  = parseInt(mesEl.dataset.mesid, 10);
        if (isNaN(mesId)) return;
        const { chat } = SillyTavern.getContext();
        const msg       = chat?.[mesId];
        const entryId   = msg?.extra?.tinyctx_entry_id;
        if (!entryId) return;
        // Fire-and-forget — ST will remove the message from its side
        gwFetch(
            `/v1/sessions/${encodeURIComponent(sid())}/history/${entryId}`,
            { method: "DELETE" }
        ).catch(e => console.warn("[TinyCTX] delete failed:", e));
    }, true); // capture phase so we run before ST's own handler
}

// Wire up abort to ST's stop generation button
function bindAbortHook() {
    // ST's stop button: #stop_generation or .stop_generation_button
    const stopBtn = document.getElementById("stop_generation")
        ?? document.querySelector(".stop_generation_button");
    if (!stopBtn || stopBtn.dataset.tinyCTXAbort) return;
    stopBtn.dataset.tinyCTXAbort = "1";

    stopBtn.addEventListener("click", () => {
        if (!isTinyCTXActive) return;
        gwFetch(`/v1/sessions/${encodeURIComponent(sid())}/generation`, { method: "DELETE" })
            .catch(e => console.warn("[TinyCTX] abort failed:", e));
    });
}

// Wire up rename chat
function bindRenameHook() {
    const btn = document.getElementById("chat_rename_confirm_button")
        ?? document.querySelector(".rename_chat_confirm");
    if (!btn || btn.dataset.tinyCTXRename) return;
    btn.dataset.tinyCTXRename = "1";

    btn.addEventListener("click", async () => {
        if (!isTinyCTXActive) return;
        const input   = document.getElementById("chat_rename_input")
            ?? document.querySelector(".rename_chat_input");
        const newName = input?.value?.trim();
        if (!newName) return;
        const oldId   = sid();
        await gwFetch(`/v1/sessions/${encodeURIComponent(oldId)}/rename`, {
            method: "PATCH",
            body:   JSON.stringify({ new_id: newName }),
        });
        getSettings().session_id = newName;
        saveSettings();
        await renderVersionPanel();
    });
}

// ---------------------------------------------------------------------------
// Activate / deactivate
// ---------------------------------------------------------------------------

async function activate() {
    isTinyCTXActive = true;
    setStatus("Connecting…", "connecting");
    showVersionPanel();
    // Pull first (gateway is authoritative for agent-written edits),
    // then push any local card changes on top.
    await pullFieldsFromGateway();
    await syncAllFields();
    startPolling();
}

function deactivate() {
    isTinyCTXActive = false;
    stopPolling();
    hideVersionPanel();
    setStatus("Inactive — select TinyCTX Agent", "idle");
}

function startPolling() {
    if (healthPollTimer) return;
    pollOnce();
    healthPollTimer = setInterval(pollOnce, 15_000);
}

function stopPolling() {
    clearInterval(healthPollTimer);
    healthPollTimer = null;
}

async function pollOnce() {
    const h = await gwHealth();
    if (h) setStatus(`Connected — uptime ${Math.round(h.uptime_s)}s`, "ok");
    else   setStatus("Gateway unreachable", "err");
}

// ---------------------------------------------------------------------------
// Managed character bootstrap
// ---------------------------------------------------------------------------

async function ensureCharacter() {
    const { characters } = SillyTavern.getContext();
    if ((characters ?? []).some(c => c.name === CHAR_NAME)) return;
    try {
        await fetch("/api/characters/create", {
            method:  "POST",
            headers: { "Content-Type": "application/json" },
            body:    JSON.stringify({
                name:          CHAR_NAME,
                description:   "<!-- Edit to update AGENTS.md on the TinyCTX gateway -->",
                personality:   "<!-- Edit to update SOUL.md on the TinyCTX gateway -->",
                scenario:      "<!-- Edit to update MEMORY.md on the TinyCTX gateway -->",
                first_mes:     "TinyCTX connected.",
                mes_example:   "",
                creator_notes: "Managed by TinyCTX extension — do not rename.",
                tags:          ["tinyctx"],
            }),
        });
        console.log("[TinyCTX] Created managed character.");
    } catch (e) {
        console.error("[TinyCTX] Failed to create character:", e);
    }
}

// ---------------------------------------------------------------------------
// Settings panel
// ---------------------------------------------------------------------------

const SETTINGS_HTML = `
<div id="tinyctx-settings">
    <div id="tinyctx-status-bar">
        <span class="tinyctx-dot" data-state="idle"></span>
        <span id="tinyctx-status-text">Inactive — select TinyCTX Agent to connect</span>
    </div>

    <div class="tinyctx-field">
        <label for="tinyctx-endpoint">Gateway endpoint</label>
        <input type="text" id="tinyctx-endpoint" placeholder="http://127.0.0.1:8080">
    </div>

    <div class="tinyctx-field">
        <label for="tinyctx-apikey">API key</label>
        <input type="password" id="tinyctx-apikey" placeholder="blank = no auth">
    </div>

    <div class="tinyctx-field">
        <label for="tinyctx-session-id">Session ID</label>
        <input type="text" id="tinyctx-session-id" placeholder="sillytavern">
    </div>

    <div class="tinyctx-row">
        <input type="checkbox" id="tinyctx-tool-events">
        <label for="tinyctx-tool-events">Show tool events in chat</label>
    </div>

    <div class="tinyctx-row">
        <input type="checkbox" id="tinyctx-sync-fields">
        <label for="tinyctx-sync-fields">Sync card fields → workspace on save</label>
    </div>

    <div class="tinyctx-btn-row">
        <button id="tinyctx-btn-ping">Ping</button>
        <button id="tinyctx-btn-sync">Sync fields</button>
        <button id="tinyctx-btn-recreate">Recreate character</button>
    </div>
</div>
`;

function bindSettings() {
    const s   = getSettings();
    const ep  = document.getElementById("tinyctx-endpoint");
    const key = document.getElementById("tinyctx-apikey");
    const sid_el = document.getElementById("tinyctx-session-id");
    const te  = document.getElementById("tinyctx-tool-events");
    const sf  = document.getElementById("tinyctx-sync-fields");
    if (!ep) return;

    ep.value     = s.endpoint;
    key.value    = s.api_key;
    sid_el.value = s.session_id;
    te.checked   = s.show_tool_events;
    sf.checked   = s.sync_card_fields;

    const persist = () => {
        s.endpoint         = ep.value.trim().replace(/\/$/, "") || "http://127.0.0.1:8080";
        s.api_key          = key.value.trim();
        s.session_id       = sid_el.value.trim() || "sillytavern";
        s.show_tool_events = te.checked;
        s.sync_card_fields = sf.checked;
        saveSettings();
    };
    [ep, key, sid_el, te, sf].forEach(el => {
        el.addEventListener("change", persist);
        el.addEventListener("input",  persist);
    });

    document.getElementById("tinyctx-btn-ping")?.addEventListener("click", async () => {
        setStatus("Pinging…", "connecting");
        const h = await gwHealth();
        h ? setStatus(`OK — uptime ${Math.round(h.uptime_s)}s`, "ok")
          : setStatus("Unreachable", "err");
    });

    document.getElementById("tinyctx-btn-sync")?.addEventListener("click", async () => {
        await syncAllFields();
        setStatus("Synced", "ok");
    });

    document.getElementById("tinyctx-btn-recreate")?.addEventListener("click", ensureCharacter);
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

(async () => {
    const { eventSource, event_types } = SillyTavern.getContext();

    // Settings panel
    $("#extensions_settings").append(`
        <div class="inline-drawer">
            <div class="inline-drawer-toggle inline-drawer-header">
                <b>TinyCTX</b>
                <div class="inline-drawer-icon fa-solid fa-circle-chevron-down down"></div>
            </div>
            <div class="inline-drawer-content">${SETTINGS_HTML}</div>
        </div>
    `);
    bindSettings();

    // Version panel — inject above the character list
    const anchor = document.getElementById("rm_print_characters_block")
        ?? document.getElementById("right-nav-panel")
        ?? document.body;
    anchor.insertAdjacentHTML("afterbegin", VERSION_PANEL_HTML);

    // "New chat" button → POST /next to bump version
    document.getElementById("tinyctx-new-chat-btn")?.addEventListener("click", async () => {
        if (!isTinyCTXActive) return;
        await gwFetch(`/v1/sessions/${encodeURIComponent(sid())}/next`, { method: "POST" });
        await loadVersionIntoST(null);
        await renderVersionPanel();
        setStatus(`New chat started`, "ok");
    });

    // Events — note: it's event_types (snake_case), not eventTypes
    eventSource.on(event_types.CHAT_CHANGED,     onChatChanged);
    eventSource.on(event_types.CHARACTER_EDITED, async () => {
        if (isTinyCTXActive) await syncAllFields();
    });
    eventSource.on(event_types.MESSAGE_EDITED,   onMessageEdited);
    eventSource.on(event_types.APP_READY, async () => {
        await ensureCharacter();
        await onChatChanged();
        bindRenameHook();
        bindAbortHook();
        bindDeleteHook();
    });

    console.log("[TinyCTX] Extension loaded.");
})();
