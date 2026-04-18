const API_BASE = "http://127.0.0.1:8000";

// --- Auth gate (SaaS mode only; no-op in OSS) --------------------------------
// cloudStatus and supabaseClient are populated during bootstrapAuth() before
// any other fetch is issued. In SaaS mode, window.fetch is patched to attach
// the Supabase access token to every API_BASE request, so the rest of the
// codebase doesn't need per-call auth awareness.
let cloudStatus = null;
let supabaseClient = null;

async function bootstrapAuth() {
    try {
        const res = await fetch(`${API_BASE}/cloud/status`);
        cloudStatus = await res.json();
    } catch (_) {
        cloudStatus = { mode: "oss", features: {} };
    }
    if (cloudStatus.mode !== "saas") return;

    const cfg = cloudStatus.client_config || {};
    if (!cfg.supabase_url || !cfg.supabase_anon_key) {
        // SaaS mode declared but the server is missing the public client
        // config — fail loudly so the user knows to fix their .env instead of
        // silently downgrading to an auth-free experience.
        document.body.innerHTML = `<div style="padding:48px;max-width:560px;margin:0 auto;font-family:-apple-system,sans-serif;">
            <h2>Server misconfigured</h2>
            <p>SAAS_MODE is on but SUPABASE_URL or SUPABASE_ANON_KEY is missing. Add them to the server environment and restart.</p>
        </div>`;
        throw new Error("Missing SaaS client config.");
    }

    const { createClient } = await import("https://esm.sh/@supabase/supabase-js@2?bundle");
    supabaseClient = createClient(cfg.supabase_url, cfg.supabase_anon_key, {
        auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true },
    });

    const { data: { session } } = await supabaseClient.auth.getSession();
    if (!session) {
        window.location.replace("/login");
        throw new Error("No session — redirecting to /login.");
    }

    // Intercept every fetch to our API and attach the current access token.
    // Supabase auto-refreshes the token before it expires, so reading it on
    // each call is cheap and always yields a live JWT.
    const origFetch = window.fetch.bind(window);
    window.fetch = async (input, init = {}) => {
        const url = typeof input === "string" ? input : input?.url || "";
        const isOurApi = url.startsWith(API_BASE) || url.startsWith("/");
        if (!isOurApi) return origFetch(input, init);
        const { data: { session: fresh } } = await supabaseClient.auth.getSession();
        const token = fresh?.access_token;
        if (!token) return origFetch(input, init);
        const headers = new Headers(init.headers || (typeof input === "object" ? input.headers : undefined));
        headers.set("Authorization", `Bearer ${token}`);
        return origFetch(input, { ...init, headers });
    };

    // Surface Sign out entry in the File menu and stamp the user's email.
    document.querySelectorAll(".menu-saas-only").forEach((el) => { el.style.display = ""; });
    const emailSlot = document.getElementById("menu-sign-out-email");
    if (emailSlot) emailSlot.textContent = session.user?.email || "";
}

async function signOut() {
    if (!supabaseClient) return;
    await supabaseClient.auth.signOut();
    window.location.replace("/login");
}
const COLUMN_COUNT = 40;
const ROW_COUNT = 150;
const DEFAULT_COL_WIDTH = 112;
const DEFAULT_ROW_HEIGHT = 24;
const UNDO_LIMIT = 50;

let workbook = { active_sheet: "Sheet1", sheets: [] };
let gridData = {};
let selectedRange = { start: "A1", end: "A1" };
let selectionAnchor = null;
let isSelecting = false;
let editingCell = null;
let scopeMode = "selection";
let previewState = null;
let chainMode = false;
let assistantOpen = true;
let dragFillState = null;
let resizeState = null;
let formulaPickState = null;
let colWidths = {};
let rowHeights = {};
let pendingHistory = [];
let undoStack = [];
let redoStack = [];
let clipboardMatrix = null;
let modelCatalog = { models: [], default_model_id: null, configured_providers: [] };
let selectedModelId = null;
const MODEL_PREF_KEY = "gridos.selectedModelId";

// Session chat log — mirrors kernel.chat_log so save/reload can restore the thread.
// Each entry: { id, kind: "user"|"agent"|"chain-step"|"chain-complete"|"system",
//               text?, payload?, outcome?, ts }. DOM nodes carry data-chat-entry-id so
// outcome updates can find their entry back.
let sessionChat = [];
let chatPersistTimer = null;
let chatPersistInFlight = false;

function genChatEntryId() {
    return `c_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

function schedulePersistChat() {
    if (chatPersistTimer) clearTimeout(chatPersistTimer);
    chatPersistTimer = setTimeout(persistSessionChat, 300);
}

async function persistSessionChat() {
    chatPersistTimer = null;
    if (chatPersistInFlight) {
        // Retry after the current POST settles.
        schedulePersistChat();
        return;
    }
    chatPersistInFlight = true;
    try {
        await fetch(`${API_BASE}/workbook/chat/replace`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ entries: sessionChat }),
        });
    } catch (_) {
        // Best-effort sync; next schedulePersistChat call will retry.
    } finally {
        chatPersistInFlight = false;
    }
}

function pushChatEntry(entry) {
    sessionChat.push(entry);
    schedulePersistChat();
    return entry;
}

function updateChatEntryOutcome(entryId, outcome) {
    if (!entryId) return;
    const entry = sessionChat.find((e) => e.id === entryId);
    if (!entry) return;
    entry.outcome = outcome;
    schedulePersistChat();
}

function rehydrateChatFromWorkbook(entries) {
    // Clear the DOM + re-render every entry from the server's chat_log. Called on
    // page load and after importing a .gridos file. We suspend auto-persist while
    // replaying so the replay doesn't immediately POST the log right back.
    if (chatPersistTimer) { clearTimeout(chatPersistTimer); chatPersistTimer = null; }
    sessionChat = [];
    const conversation = document.getElementById("chat-conversation");
    if (!conversation) return;
    conversation.innerHTML = "";
    previewMessageEl = null;

    for (const entry of entries) {
        if (!entry || !entry.kind) continue;
        sessionChat.push(entry);
        if (entry.kind === "user") {
            addLog("user", escapeHtml(entry.text || ""));
        } else if (entry.kind === "system") {
            addLog("system", escapeHtml(entry.text || ""));
        } else if (entry.kind === "agent") {
            const payload = entry.payload || {};
            const { html } = buildPreviewCardBody(payload, { includeActions: false });
            const msg = addLog("agent", html);
            if (msg) {
                msg.dataset.chatEntryId = entry.id;
                if (entry.outcome) {
                    const badge = document.createElement("div");
                    badge.className = `preview-outcome preview-outcome-${entry.outcome}`;
                    badge.textContent = entry.outcome === "applied" ? "Applied"
                        : entry.outcome === "dismissed" ? "Dismissed"
                        : entry.outcome === "replaced" ? "Superseded"
                        : entry.outcome;
                    msg.prepend(badge);
                }
                wireProposedMacroButtons(msg);
            }
        } else if (entry.kind === "chain-step" || entry.kind === "chain-complete") {
            const { html } = buildChainStepHtml(entry.payload || {}, entry.step_idx ?? 0);
            const msg = addLog(entry.kind, html);
            if (msg) wireProposedMacroButtons(msg);
        }
    }

    if (!sessionChat.length) {
        // Restore the empty-state panel so the quick-prompt chips reappear.
        clearChatConversationDom();
    }
}

function clearChatConversationDom() {
    // DOM-only reset (no sessionChat / backend mutation). Used by rehydrate when
    // the incoming chat_log is empty.
    const conversation = document.getElementById("chat-conversation");
    if (!conversation) return;
    conversation.innerHTML = `
        <div class="chat-empty" id="chat-empty">
            <div class="chat-empty-logo">GO</div>
            <h3>How can I help?</h3>
            <p>Describe what you want to build or analyze. I'll plan it, preview the cells, and only write them when you approve.</p>
            <div class="quick-prompts" id="quick-prompts-empty"></div>
        </div>
    `;
    seedQuickPromptChips();
}

function seedQuickPromptChips() {
    const empty = document.getElementById("quick-prompts-empty");
    if (!empty) return;
    const prompts = [
        { text: "Operating model", prompt: "Build a quarterly operating model starting at B2 with revenue growing 10% QoQ from 100, COGS at 40% of revenue, OpEx flat at 30, gross profit, and operating income. Plan the full model first, then fill section by section.", chain: true },
        { text: "Simple DCF", prompt: "Build a simple DCF starting at B2: 5 years of FCF growing 15% from 100, a 10% discount rate row, present value of each year using DIVIDE and POWER, and a total PV. Plan first, then fill.", chain: true },
        { text: "Hiring tracker", prompt: "Create a hiring tracker in the selected area with role, stage, owner, and notes columns.", chain: false },
        { text: "Summarize selection", prompt: "Summarize the selected range into a clean executive header row and totals.", chain: false },
    ];
    prompts.forEach((p) => {
        const btn = document.createElement("button");
        btn.className = "quick-prompt";
        btn.type = "button";
        btn.textContent = p.text;
        btn.dataset.prompt = p.prompt;
        if (p.chain) btn.dataset.chain = "true";
        btn.addEventListener("click", () => {
            document.getElementById("assistant-input").value = p.prompt;
            syncSendButtonState();
            autoGrowInput();
            if (p.chain) setChainMode(true);
            document.getElementById("assistant-input").focus();
        });
        empty.appendChild(btn);
    });
}

const cellEls = new Map();
const colEls = new Map();
const rowEls = new Map();
let populatedCells = new Set();
let paintedSelection = new Set();
let paintedPreview = new Set();
let activeCellId = null;

// Charts state
let sheetCharts = [];
const chartInstances = new Map();
const chartOverlayEls = new Map();
const minimizedChartIds = new Set();
let editingChartId = null;
const CHART_PALETTE = ["#4285f4", "#ea4335", "#fbbc04", "#34a853", "#a142f4", "#00acc1", "#ff7043", "#8d6e63"];

function colLabel(index) {
    let label = "";
    let value = index + 1;
    while (value > 0) {
        const remainder = (value - 1) % 26;
        label = String.fromCharCode(65 + remainder) + label;
        value = Math.floor((value - 1) / 26);
    }
    return label;
}

function a1ToCoords(a1) {
    const match = /^([A-Z]+)(\d+)$/.exec(a1.toUpperCase());
    if (!match) return { row: 0, col: 0 };
    let col = 0;
    for (const char of match[1]) col = col * 26 + (char.charCodeAt(0) - 64);
    return { row: Number(match[2]) - 1, col: col - 1 };
}

function coordsToA1(row, col) {
    return `${colLabel(col)}${row + 1}`;
}

function escapeHtml(value) {
    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

function getSelectedBounds() {
    const start = a1ToCoords(selectedRange.start);
    const end = a1ToCoords(selectedRange.end);
    return {
        top: Math.min(start.row, end.row),
        bottom: Math.max(start.row, end.row),
        left: Math.min(start.col, end.col),
        right: Math.max(start.col, end.col),
    };
}

function getSelectedCells() {
    const bounds = getSelectedBounds();
    const cells = [];
    for (let row = bounds.top; row <= bounds.bottom; row++) {
        for (let col = bounds.left; col <= bounds.right; col++) {
            cells.push(coordsToA1(row, col));
        }
    }
    return cells;
}

function selectionLabel() {
    return selectedRange.start === selectedRange.end ? selectedRange.start : `${selectedRange.start}:${selectedRange.end}`;
}

function getCellDisplay(state) {
    if (!state) return "";
    if (state.formula) return state.formula;
    if (state.value === null || state.value === undefined) return "";
    return String(state.value);
}

function renderTabs() {
    const strip = document.getElementById("tab-strip");
    const tabs = workbook.sheets.map((sheet) => `
        <button class="tab-btn ${sheet.active ? "active" : ""}" data-sheet="${escapeHtml(sheet.name)}">${escapeHtml(sheet.name)}</button>
    `).join("");
    strip.innerHTML = `${tabs}<button class="icon-btn" id="add-tab-btn">+</button><button class="icon-btn" id="rename-tab-btn">R</button>`;

    strip.querySelectorAll("[data-sheet]").forEach((button) => {
        button.addEventListener("click", async () => activateSheet(button.dataset.sheet));
    });
    document.getElementById("add-tab-btn").addEventListener("click", createSheet);
    document.getElementById("rename-tab-btn").addEventListener("click", renameActiveSheet);
}

async function fetchWorkbook({ rehydrateChat: shouldRehydrate = false } = {}) {
    const res = await fetch(`${API_BASE}/api/workbook`);
    workbook = await res.json();
    const activePill = document.getElementById("active-sheet-pill");
    if (activePill) activePill.textContent = workbook.active_sheet;
    syncWorkbookTitleInput();
    renderTabs();
    if (shouldRehydrate) {
        rehydrateChatFromWorkbook(workbook.chat_log || []);
    }
}

function syncWorkbookTitleInput() {
    const input = document.getElementById("workbook-title-input");
    const name = workbook.workbook_name || "Untitled workbook";
    document.title = `${name} — GridOS`;
    if (!input) return;
    if (document.activeElement === input) return;
    input.value = name;
}

async function commitWorkbookName(newName) {
    const cleaned = (newName || "").trim();
    const current = workbook.workbook_name || "Untitled workbook";
    if (!cleaned || cleaned === current) {
        syncWorkbookTitleInput();
        return;
    }
    try {
        const res = await fetch(`${API_BASE}/workbook/rename`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: cleaned }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Could not rename workbook.");
        workbook.workbook_name = data.workbook_name;
        syncWorkbookTitleInput();
        document.title = `${data.workbook_name} — GridOS`;
        addLog("system", `Workbook renamed to ${escapeHtml(data.workbook_name)}.`);
    } catch (error) {
        addLog("system", escapeHtml(`Rename failed: ${error.message}`));
        syncWorkbookTitleInput();
    }
}

async function activateSheet(name) {
    await fetch(`${API_BASE}/workbook/sheet/activate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
    });
    await fetchWorkbook();
    clearPreview();
    selectedRange = { start: "A1", end: "A1" };
    await fetchGrid();
}

async function createSheet() {
    const proposed = window.prompt("Name the new tab", `Sheet ${workbook.sheets.length + 1}`);
    if (proposed === null) return;
    const res = await fetch(`${API_BASE}/workbook/sheet`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: proposed }),
    });
    workbook = await res.json();
    renderTabs();
    const activePill = document.getElementById("active-sheet-pill");
    if (activePill) activePill.textContent = workbook.active_sheet;
    selectedRange = { start: "A1", end: "A1" };
    await fetchGrid();
}

async function renameActiveSheet() {
    const current = workbook.active_sheet;
    const proposed = window.prompt("Rename current tab", current);
    if (!proposed || proposed === current) return;
    const res = await fetch(`${API_BASE}/workbook/sheet/rename`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ old_name: current, new_name: proposed }),
    });
    workbook = await res.json();
    renderTabs();
    const activePill = document.getElementById("active-sheet-pill");
    if (activePill) activePill.textContent = workbook.active_sheet;
}

function applyDimensions() {
    // colEls now holds one <col> element per column; the browser applies the
    // width to every <td>/<th> in that column with a single style write.
    colEls.forEach((node, label) => {
        const width = colWidths[label] || DEFAULT_COL_WIDTH;
        node.style.width = `${width}px`;
    });
    // rowEls now holds one <tr> element per row; rows auto-size to the tallest
    // cell, but setting height on <tr> propagates cheaply.
    rowEls.forEach((node, row) => {
        const height = rowHeights[row] || DEFAULT_ROW_HEIGHT;
        node.style.height = `${height}px`;
    });
    if (sheetCharts && sheetCharts.length) {
        sheetCharts.forEach(spec => {
            const el = chartOverlayEls.get(spec.id);
            if (el) positionChartOverlay(el, spec);
        });
    }
}

function renderGridShell() {
    const table = document.getElementById("spreadsheet");

    // <colgroup> lets us resize a whole column with ONE DOM write (per <col>)
    // instead of touching every td's inline style.
    let html = `<colgroup><col class="rowhdr-col" /></colgroup><colgroup id="data-colgroup">`;
    for (let col = 0; col < COLUMN_COUNT; col++) {
        const label = colLabel(col);
        html += `<col data-colgroup-for="${label}" />`;
    }
    html += `</colgroup>`;

    html += `<tr><th class="corner"></th>`;
    for (let col = 0; col < COLUMN_COUNT; col++) {
        const label = colLabel(col);
        html += `<th class="col-header" data-col="${label}"><div class="header-inner">${label}<div class="resize-handle-col" data-resize-col="${label}"></div></div></th>`;
    }
    html += `</tr>`;

    for (let row = 1; row <= ROW_COUNT; row++) {
        html += `<tr data-row-tr="${row}"><th class="row-header" data-row="${row}"><div class="header-inner">${row}<div class="resize-handle-row" data-resize-row="${row}"></div></div></th>`;
        for (let col = 0; col < COLUMN_COUNT; col++) {
            const label = colLabel(col);
            const a1 = `${label}${row}`;
            html += `<td data-cell="${a1}"><div class="cell-content"></div></td>`;
        }
        html += `</tr>`;
    }
    table.innerHTML = html;

    colEls.clear();
    rowEls.clear();
    cellEls.clear();

    table.querySelectorAll("[data-colgroup-for]").forEach((node) => {
        colEls.set(node.dataset.colgroupFor, node);
    });
    table.querySelectorAll("tr[data-row-tr]").forEach((node) => {
        rowEls.set(node.dataset.rowTr, node);
    });
    table.querySelectorAll("td[data-cell]").forEach((node) => {
        cellEls.set(node.dataset.cell, node);
    });

    document.getElementById("grid-meta").textContent = `${COLUMN_COUNT} columns x ${ROW_COUNT} rows`;
    applyDimensions();
}

function updateCellDom(a1) {
    const td = cellEls.get(a1);
    if (!td) return;
    const state = gridData[a1];
    td.classList.toggle("locked", Boolean(state?.locked));
    const content = td.firstElementChild;
    const display = state && state.value !== null && state.value !== undefined ? String(state.value) : "";
    content.className = `cell-content${state?.formula ? " cell-formula" : ""}`;
    content.textContent = display;
}

function refreshPopulatedCells() {
    const nextPopulated = new Set();
    Object.entries(gridData).forEach(([a1, state]) => {
        if ((state.value !== null && state.value !== "") || state.formula || state.locked) {
            nextPopulated.add(a1);
        }
    });

    populatedCells.forEach((a1) => {
        if (!nextPopulated.has(a1)) {
            const td = cellEls.get(a1);
            if (td) {
                td.classList.remove("locked");
                td.firstElementChild.className = "cell-content";
                td.firstElementChild.textContent = "";
            }
        }
    });

    nextPopulated.forEach((a1) => updateCellDom(a1));
    populatedCells = nextPopulated;
    document.getElementById("metric-cells").textContent = Object.values(gridData).filter((state) => ((state.value !== null && state.value !== "") || state.formula)).length;
}

async function fetchGrid() {
    const res = await fetch(`${API_BASE}/debug/grid?sheet=${encodeURIComponent(workbook.active_sheet)}`);
    const payload = await res.json();
    gridData = payload.cells || {};
    sheetCharts = payload.charts || [];
    refreshPopulatedCells();
    syncSelectionUI();
    repaintSelection();
    repaintPreview();
    renderCharts();
    refreshChartsList();
}

function syncSelectionUI() {
    const anchor = selectedRange.end;
    document.getElementById("name-box").textContent = selectionLabel();
    document.getElementById("selection-pill").textContent = selectionLabel();
    const composerHint = document.getElementById("composer-hint");
    if (composerHint) {
        composerHint.textContent = `${scopeMode === "selection" ? `Selection: ${selectionLabel()}` : "Whole sheet"} · Preview-safe by default`;
    }
    const subtitleEl = document.getElementById("assistant-subtitle");
    if (subtitleEl) subtitleEl.textContent = scopeMode === "selection" ? "Focused on the selected cells." : "Focused on the active sheet.";
    const scopePill = document.getElementById("scope-pill");
    if (scopePill) scopePill.textContent = scopeMode === "selection" ? "Selected Cells" : "Entire Sheet";
    document.getElementById("formula-input").value = getCellDisplay(gridData[anchor]);
    updateSelectionStats();
    highlightHeadersForSelection();
}

function highlightHeadersForSelection() {
    document.querySelectorAll(".col-header.col-selected").forEach((el) => el.classList.remove("col-selected"));
    document.querySelectorAll(".row-header.row-selected").forEach((el) => el.classList.remove("row-selected"));
    const bounds = getSelectedBounds();
    for (let col = bounds.left; col <= bounds.right; col++) {
        const label = colLabel(col);
        document.querySelectorAll(`th.col-header[data-col="${label}"]`).forEach((el) => el.classList.add("col-selected"));
    }
    for (let row = bounds.top; row <= bounds.bottom; row++) {
        document.querySelectorAll(`th.row-header[data-row="${row + 1}"]`).forEach((el) => el.classList.add("row-selected"));
    }
}

function repaintSelection() {
    paintedSelection.forEach((a1) => cellEls.get(a1)?.classList.remove("selected"));
    paintedSelection.clear();
    if (activeCellId) {
        const oldActive = cellEls.get(activeCellId);
        if (oldActive) {
            oldActive.classList.remove("active");
            oldActive.querySelector(".fill-handle")?.remove();
        }
    }

    const bounds = getSelectedBounds();
    for (let row = bounds.top; row <= bounds.bottom; row++) {
        for (let col = bounds.left; col <= bounds.right; col++) {
            const a1 = coordsToA1(row, col);
            const td = cellEls.get(a1);
            if (td) {
                td.classList.add("selected");
                paintedSelection.add(a1);
            }
        }
    }

    activeCellId = selectedRange.end;
    const active = cellEls.get(activeCellId);
    if (active) {
        active.classList.add("active");
        if (!editingCell && !active.querySelector(".fill-handle")) {
            const handle = document.createElement("div");
            handle.className = "fill-handle";
            active.appendChild(handle);
        }
    }
}

function repaintPreview(extraCells = null) {
    paintedPreview.forEach((a1) => {
        const td = cellEls.get(a1);
        if (td) {
            td.classList.remove("preview", "preview-active");
            updateCellDom(a1);
        }
    });
    paintedPreview.clear();

    if (extraCells) {
        extraCells.forEach((a1, index) => {
            const td = cellEls.get(a1);
            if (!td) return;
            td.classList.add("preview");
            if (index === 0) td.classList.add("preview-active");
            paintedPreview.add(a1);
        });
        return;
    }

    const items = previewState?.preview_cells || [];
    items.forEach((item, index) => {
        const td = cellEls.get(item.cell);
        if (!td) return;
        td.classList.add("preview");
        if (index === 0) td.classList.add("preview-active");
        const content = td.firstElementChild;
        const display = item.value === null || item.value === undefined ? "" : String(item.value);
        content.textContent = display;
        content.classList.add("cell-preview-text");
        paintedPreview.add(item.cell);
    });
}

function setSelection(start, end) {
    // Skip the DOM work when nothing actually changed — common during a selection
    // drag where the mouse moves inside the same cell across many frames.
    if (selectedRange.start === start && selectedRange.end === end) return;
    selectedRange = { start, end };
    syncSelectionUI();
    repaintSelection();
}

function addLog(kind, html) {
    const conversation = document.getElementById("chat-conversation");
    const empty = document.getElementById("chat-empty");
    if (empty && empty.parentElement === conversation) empty.remove();
    const msg = document.createElement("div");
    msg.className = `msg ${kind}`;
    msg.innerHTML = html;
    conversation.appendChild(msg);
    conversation.scrollTop = conversation.scrollHeight;
    return msg;
}

function clearChatConversation() {
    sessionChat = [];
    previewMessageEl = null;
    if (chatPersistTimer) { clearTimeout(chatPersistTimer); chatPersistTimer = null; }
    fetch(`${API_BASE}/workbook/chat/clear`, { method: "POST" }).catch(() => {});
    clearChatConversationDom();
}

async function persistSingleCell(cell, value) {
    const res = await fetch(`${API_BASE}/grid/cell`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cell, value, sheet: workbook.active_sheet }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Could not save cell.");
    return data;
}

async function persistRange(targetCell, values) {
    const res = await fetch(`${API_BASE}/grid/range`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_cell: targetCell, values, sheet: workbook.active_sheet }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Could not save range.");
    return data;
}

function extractSelectionMatrix() {
    const bounds = getSelectedBounds();
    const rows = [];
    for (let row = bounds.top; row <= bounds.bottom; row++) {
        const values = [];
        for (let col = bounds.left; col <= bounds.right; col++) {
            values.push(getCellDisplay(gridData[coordsToA1(row, col)]));
        }
        rows.push(values);
    }
    return rows;
}

async function saveFormulaBar() {
    const before = snapshotGrid();
    try {
        setStatus("Saving");
        await persistSingleCell(selectedRange.end, document.getElementById("formula-input").value);
        await fetchGrid();
        recordAction(before);
        setStatus("Ready");
    } catch (error) {
        addLog("system", escapeHtml(`Save failed: ${error.message}`));
        setStatus("Recover");
    }
}

function getInlineEditor() {
    return document.getElementById("inline-editor");
}

function isEditingFormula() {
    if (!editingCell) return false;
    const input = getInlineEditor();
    return !!input && input.value.trimStart().startsWith("=");
}

function rangeRefText(anchorA1, endA1) {
    if (anchorA1 === endA1) return anchorA1;
    const a = a1ToCoords(anchorA1);
    const b = a1ToCoords(endA1);
    const top = Math.min(a.row, b.row);
    const bottom = Math.max(a.row, b.row);
    const left = Math.min(a.col, b.col);
    const right = Math.max(a.col, b.col);
    return `${coordsToA1(top, left)}:${coordsToA1(bottom, right)}`;
}

function startFormulaPick(cellA1) {
    const input = getInlineEditor();
    if (!input) return;
    const start = input.selectionStart ?? input.value.length;
    const end = input.selectionEnd ?? input.value.length;
    input.value = input.value.slice(0, start) + cellA1 + input.value.slice(end);
    const caret = start + cellA1.length;
    input.setSelectionRange(caret, caret);
    input.focus();
    formulaPickState = {
        insertStart: start,
        insertLen: cellA1.length,
        anchor: cellA1,
        end: cellA1,
    };
}

function extendFormulaPick(cellA1) {
    if (!formulaPickState || cellA1 === formulaPickState.end) return;
    const input = getInlineEditor();
    if (!input) return;
    const ref = rangeRefText(formulaPickState.anchor, cellA1);
    const { insertStart, insertLen } = formulaPickState;
    input.value = input.value.slice(0, insertStart) + ref + input.value.slice(insertStart + insertLen);
    const caret = insertStart + ref.length;
    input.setSelectionRange(caret, caret);
    input.focus();
    formulaPickState.insertLen = ref.length;
    formulaPickState.end = cellA1;
}

function finishFormulaPick() {
    if (!formulaPickState) return;
    formulaPickState = null;
    const input = getInlineEditor();
    if (input) input.focus();
}

function startInlineEdit(cell, seed) {
    const state = gridData[cell] || {};
    if (state.locked) {
        addLog("system", `${cell} is locked and cannot be edited.`);
        return;
    }

    editingCell = cell;
    const td = cellEls.get(cell);
    const initial = typeof seed === "string" ? seed : getCellDisplay(state);
    td.innerHTML = `<input class="editing-input" id="inline-editor" value="${escapeHtml(initial)}" />`;
    const input = document.getElementById("inline-editor");
    input.focus();
    if (typeof seed === "string") {
        const len = input.value.length;
        input.setSelectionRange(len, len);
    } else {
        input.select();
    }
    input.addEventListener("keydown", async (event) => {
        if (event.key === "Enter") {
            event.preventDefault();
            await commitInlineEdit(cell, input.value);
            moveSelection(1, 0, false);
        }
        if (event.key === "Tab") {
            event.preventDefault();
            await commitInlineEdit(cell, input.value);
            moveSelection(0, event.shiftKey ? -1 : 1, false);
        }
        if (event.key === "Escape") {
            editingCell = null;
            td.innerHTML = `<div class="cell-content"></div>`;
            updateCellDom(cell);
            repaintSelection();
        }
    });
    input.addEventListener("blur", async () => {
        if (formulaPickState) return;
        if (editingCell === cell) await commitInlineEdit(cell, input.value);
    });
}

async function commitInlineEdit(cell, value) {
    editingCell = null;
    const before = snapshotGrid();
    try {
        await persistSingleCell(cell, value);
        const td = cellEls.get(cell);
        td.innerHTML = `<div class="cell-content"></div>`;
        await fetchGrid();
        recordAction(before);
    } catch (error) {
        addLog("system", escapeHtml(`Inline edit failed: ${error.message}`));
        await fetchGrid();
    }
}

function parseClipboardMatrix(text) {
    return text.replace(/\r/g, "").split("\n").filter(Boolean).map((row) => row.split("\t"));
}

async function copySelection() {
    const text = extractSelectionMatrix().map((row) => row.join("\t")).join("\n");
    try {
        await navigator.clipboard.writeText(text);
        addLog("system", `Copied ${selectionLabel()} to clipboard.`);
    } catch {
        addLog("system", "Clipboard write was blocked by the browser.");
    }
}

async function pasteSelection(text) {
    const matrix = parseClipboardMatrix(text);
    if (!matrix.length) return;
    const before = snapshotGrid();
    try {
        await persistRange(selectedRange.end, matrix);
        await fetchGrid();
        recordAction(before);
        addLog("system", `Pasted ${matrix.length} row(s) into ${selectedRange.end}.`);
    } catch (error) {
        addLog("system", escapeHtml(`Paste failed: ${error.message}`));
    }
}

function buildFillMatrix(source, fillRows, fillCols) {
    const sourceRows = source.length;
    const sourceCols = source[0]?.length || 1;
    const output = [];
    for (let row = 0; row < fillRows; row++) {
        const current = [];
        for (let col = 0; col < fillCols; col++) {
            current.push(source[row % sourceRows][col % sourceCols]);
        }
        output.push(current);
    }
    return output;
}

async function applyDragFill(targetCell) {
    const origin = getSelectedBounds();
    const target = a1ToCoords(targetCell);
    const source = extractSelectionMatrix();
    const rowStart = Math.min(origin.top, target.row);
    const rowEnd = Math.max(origin.bottom, target.row);
    const colStart = Math.min(origin.left, target.col);
    const colEnd = Math.max(origin.right, target.col);
    const fillRows = rowEnd - rowStart + 1;
    const fillCols = colEnd - colStart + 1;
    const matrix = buildFillMatrix(source, fillRows, fillCols);

    const before = snapshotGrid();
    try {
        await persistRange(coordsToA1(rowStart, colStart), matrix);
        selectedRange = { start: coordsToA1(rowStart, colStart), end: coordsToA1(rowEnd, colEnd) };
        await fetchGrid();
        recordAction(before);
        addLog("system", `Filled ${selectionLabel()} from the current pattern.`);
    } catch (error) {
        addLog("system", escapeHtml(`Drag fill failed: ${error.message}`));
    }
}

function renderPlanBlock(plan) {
    if (!plan || !plan.sections || !plan.sections.length) return "";
    const title = plan.title ? escapeHtml(plan.title) : "Plan";
    const anchor = plan.anchor
        ? `<span style="color:var(--text-muted);">anchored at ${escapeHtml(plan.anchor)}</span>`
        : "";
    const items = plan.sections.map((s, i) => {
        const label = escapeHtml(s.label || `Section ${i + 1}`);
        const target = s.target ? `<code>${escapeHtml(s.target)}</code>` : "";
        const notes = s.notes
            ? `<div style="font-size:11px;color:var(--text-muted);margin-left:18px;">${escapeHtml(s.notes)}</div>`
            : "";
        return `<li style="margin-bottom:4px;"><strong>${label}</strong> ${target}${notes}</li>`;
    }).join("");
    return `
        <div class="model-plan" style="margin-top:10px;padding:10px 12px;border:1px solid var(--border);border-radius:var(--radius);background:var(--bg-soft);">
            <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.04em;color:var(--accent);">Plan &middot; ${title}</div>
            <div style="font-size:11px;margin-top:2px;">${anchor}</div>
            <ol style="margin:6px 0 0;padding-left:20px;font-size:12px;">${items}</ol>
        </div>
    `;
}

function renderProposedMacroBlock(spec, options = {}) {
    if (!spec) return "";
    const paramsText = (spec.params || []).join(", ");
    const replaceNote = spec.replaces_existing
        ? ` <em style="color:#b06000;">(replaces existing ${escapeHtml(spec.name)})</em>`
        : "";
    const idSuffix = options.idSuffix || "card";
    const descLine = spec.description
        ? `<div style="font-size:11px;color:var(--text-muted);margin-top:2px;">${escapeHtml(spec.description)}</div>`
        : "";
    return `
        <div class="proposed-macro" data-macro-spec='${escapeHtml(JSON.stringify(spec))}' style="margin-top:10px;padding:10px 12px;border:1px solid var(--border);border-radius:var(--radius);background:var(--bg-soft);">
            <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.04em;color:var(--accent);">Proposed macro</div>
            <div style="font-family:var(--font-mono);margin-top:4px;"><strong>${escapeHtml(spec.name)}(${escapeHtml(paramsText)})</strong>${replaceNote}</div>
            ${descLine}
            <div style="font-family:var(--font-mono);margin-top:6px;color:var(--text);"><code>${escapeHtml(spec.body)}</code></div>
            <div class="assistant-actions" style="margin-top:8px;">
                <button class="primary-btn" data-macro-save="${idSuffix}" type="button">Save macro</button>
                <button class="ghost-btn" data-macro-dismiss="${idSuffix}" type="button">Dismiss</button>
            </div>
        </div>
    `;
}

function wireProposedMacroButtons(container, onDismiss) {
    container.querySelectorAll("[data-macro-save]").forEach((btn) => {
        btn.addEventListener("click", async () => {
            const block = btn.closest(".proposed-macro");
            if (!block) return;
            const spec = JSON.parse(block.dataset.macroSpec || "null");
            if (!spec) return;
            await saveProposedMacro(spec, block);
        });
    });
    container.querySelectorAll("[data-macro-dismiss]").forEach((btn) => {
        btn.addEventListener("click", () => {
            const block = btn.closest(".proposed-macro");
            if (block) block.remove();
            if (onDismiss) onDismiss();
        });
    });
}

async function saveProposedMacro(spec, block) {
    try {
        const res = await fetch(`${API_BASE}/tools/save_macro`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                name: spec.name,
                description: spec.description || "",
                params: spec.params || [],
                body: spec.body,
            }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Save failed.");
        block.innerHTML = `<div style="color:var(--success);font-size:12px;">Saved macro <strong>${escapeHtml(spec.name)}</strong>. It's now callable from any cell.</div>`;
        addLog("system", `Saved user macro <code>${escapeHtml(spec.name)}</code>.`);
        if (typeof refreshToolsTab === "function") {
            try { await refreshToolsTab(); } catch (_) { /* library panel may not be open */ }
        }
    } catch (error) {
        addLog("system", escapeHtml(`Save macro failed: ${error.message}`));
    }
}

let previewMessageEl = null;

function freezePreviewCard(outcome) {
    // Turn the active preview card into a persistent chat history entry:
    // strip the action buttons, optionally tag it with an outcome badge,
    // and release the previewMessageEl handle so future renders don't touch it.
    if (!previewMessageEl) return;
    previewMessageEl.querySelectorAll(".msg-actions").forEach((el) => el.remove());
    if (outcome) {
        const badge = document.createElement("div");
        badge.className = `preview-outcome preview-outcome-${outcome}`;
        badge.textContent = outcome === "applied" ? "Applied"
            : outcome === "dismissed" ? "Dismissed"
            : outcome === "replaced" ? "Superseded"
            : outcome;
        previewMessageEl.prepend(badge);
    }
    updateChatEntryOutcome(previewMessageEl.dataset.chatEntryId, outcome || null);
    previewMessageEl = null;
}

function buildPreviewCardBody(payload, { includeActions = true, idSuffix = "card" } = {}) {
    const hasCells = payload.preview_cells && payload.preview_cells.length;
    const previewRange = hasCells
        ? `${payload.preview_cells[0].cell} → ${payload.preview_cells[payload.preview_cells.length - 1].cell}`
        : payload.target_cell;
    const hasValues = Array.isArray(payload.values) && payload.values.length > 0;
    const hasChart = Boolean(payload.chart_spec);
    const canApply = hasValues || hasChart;
    const applyLabel = hasValues ? "Apply" : "Add chart";

    const macroError = payload.macro_error
        ? `<div style="margin-top:8px;color:var(--danger);font-size:11px;">Macro proposal ignored: ${escapeHtml(payload.macro_error)}</div>`
        : "";
    const macroBlock = renderProposedMacroBlock(payload.proposed_macro, { idSuffix });
    const planBlock = renderPlanBlock(payload.plan);

    let actionsRow = "";
    if (includeActions) {
        actionsRow = canApply
            ? `<div class="msg-actions">
                <button class="primary-btn" id="apply-preview-btn">${escapeHtml(applyLabel)}</button>
                <button class="ghost-btn" id="dismiss-preview-btn">Dismiss</button>
            </div>`
            : `<div class="msg-actions">
                <button class="ghost-btn" id="dismiss-preview-btn">Dismiss</button>
            </div>`;
    }

    const agentLabel = escapeHtml((payload.category || "agent").toUpperCase());
    const html = `
        <div>${escapeHtml(payload.reasoning || "Preview ready.")}</div>
        <div class="msg-meta">
            <strong style="color:var(--accent);">${agentLabel}</strong>
            <span>·</span>
            <span>${payload.scope === "selection" ? "Selection" : "Whole sheet"}</span>
            <span>·</span>
            <span class="target-chip">${escapeHtml(previewRange || payload.target_cell || "")}</span>
        </div>
        ${planBlock}
        ${macroError}
        ${macroBlock}
        ${actionsRow}
    `;
    return { html, canApply };
}

function renderPreviewAsChatMessage() {
    // Freeze the prior preview (keep its reasoning visible as chat history)
    // before rendering the new one.
    if (previewMessageEl && previewMessageEl.parentElement) {
        freezePreviewCard("replaced");
    }
    previewMessageEl = null;

    if (!previewState) return;

    const { html, canApply } = buildPreviewCardBody(previewState, { includeActions: true });
    previewMessageEl = addLog("agent", html);

    const entry = pushChatEntry({
        id: genChatEntryId(),
        kind: "agent",
        payload: { ...previewState },
        outcome: null,
        ts: Date.now(),
    });
    previewMessageEl.dataset.chatEntryId = entry.id;

    if (canApply) {
        previewMessageEl.querySelector("#apply-preview-btn")?.addEventListener("click", applyPreview);
    }
    previewMessageEl.querySelector("#dismiss-preview-btn")?.addEventListener("click", () => {
        freezePreviewCard("dismissed");
        previewState = null;
        repaintPreview();
    });
    wireProposedMacroButtons(previewMessageEl);
}

// Back-compat shim — callers still call renderPreviewCard() to (re)render.
function renderPreviewCard() {
    renderPreviewAsChatMessage();
}

function clearPreview() {
    previewState = null;
    if (previewMessageEl && previewMessageEl.parentElement) previewMessageEl.remove();
    previewMessageEl = null;
    repaintPreview();
}

async function requestPreview() {
    const prompt = document.getElementById("assistant-input").value.trim();
    if (!prompt) return;
    pushChatEntry({ id: genChatEntryId(), kind: "user", text: prompt, ts: Date.now() });
    addLog("user", escapeHtml(prompt));
    // Clear the composer after submit.
    const input = document.getElementById("assistant-input");
    input.value = "";
    autoGrowInput();
    syncSendButtonState();

    const payload = {
        prompt,
        history: pendingHistory.slice(-6),
        scope: scopeMode,
        selected_cells: scopeMode === "selection" ? getSelectedCells() : [],
        sheet: workbook.active_sheet,
        model_id: selectedModelId || null,
    };

    if (chainMode) {
        await runChain(prompt, payload);
        return;
    }

    setStatus("Thinking");
    const thinking = addLog("thinking", `<span class="dot"></span><span class="dot"></span><span class="dot"></span>`);
    try {
        const res = await fetch(`${API_BASE}/agent/chat`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        thinking.remove();
        if (!res.ok) throw new Error(data.detail || "Preview failed.");
        previewState = data;
        pendingHistory.push({ role: "user", content: prompt });
        pendingHistory.push({ role: "assistant", content: `${data.category}: ${data.reasoning}` });
        renderPreviewAsChatMessage();
        repaintPreview();
        setStatus("Awaiting approval");
    } catch (error) {
        if (thinking.parentElement) thinking.remove();
        addLog("system", escapeHtml(`Preview failed: ${error.message}`));
        setStatus("Recover");
    }
}

async function runChain(prompt, payload) {
    clearPreview();
    setStatus("Chaining…");
    addLog("system", "Chain mode engaged — each step auto-applies and is observed.");
    const thinking = addLog("thinking", `<span class="dot"></span><span class="dot"></span><span class="dot"></span>`);
    const before = snapshotGrid();

    try {
        const res = await fetch(`${API_BASE}/agent/chat/chain`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        thinking.remove();
        if (!res.ok) throw new Error(data.detail || "Chain failed.");

        pendingHistory.push({ role: "user", content: prompt });
        renderChainSteps(data);

        const last = data.steps?.[data.steps.length - 1];
        if (last) {
            pendingHistory.push({
                role: "assistant",
                content: `chain (${data.iterations_used} steps): ${last.reasoning || ""}`,
            });
        }

        await fetchGrid();
        recordAction(before);
        setStatus(`Chain finished (${data.iterations_used} step${data.iterations_used === 1 ? "" : "s"})`);
    } catch (error) {
        if (thinking.parentElement) thinking.remove();
        addLog("system", escapeHtml(`Chain failed: ${error.message}`));
        setStatus("Recover");
    }
}

function buildChainStepHtml(step, idx) {
    const macroBlock = renderProposedMacroBlock(step.proposed_macro, { idSuffix: `chain-${idx}` });
    const macroError = step.macro_error
        ? `<div style="margin-top:6px;color:var(--danger);font-size:11px;">Macro proposal ignored: ${escapeHtml(step.macro_error)}</div>`
        : "";
    const planBlock = renderPlanBlock(step.plan);

    if (step.completion_signal) {
        return {
            kind: "chain-complete",
            html: `
                <strong>Step ${step.iteration + 1} &middot; complete</strong>
                <div>${escapeHtml(step.reasoning || "Agent signaled the task is finished.")}</div>
                ${planBlock}
                ${macroError}
                ${macroBlock}
            `,
        };
    }

    const valuesJson = JSON.stringify(step.values);
    const obsItems = (step.observations || []).map((obs) => {
        const formula = obs.formula ? ` <em>(formula: ${escapeHtml(obs.formula)})</em>` : "";
        const warn = obs.warning
            ? `<div style="color:var(--danger);font-size:11px;margin-left:18px;">⚠ ${escapeHtml(obs.warning)}</div>`
            : "";
        return `<li>${escapeHtml(obs.cell)} = ${escapeHtml(String(obs.value))}${formula}${warn}</li>`;
    }).join("");

    return {
        kind: "chain-step",
        html: `
            <strong>Step ${step.iteration + 1} &middot; ${escapeHtml(step.agent_id)}</strong>
            <div>${escapeHtml(step.reasoning || "")}</div>
            <div style="margin-top:6px;">Target: <strong>${escapeHtml(step.target)}</strong> &middot; Wrote: <code>${escapeHtml(valuesJson)}</code></div>
            ${obsItems ? `<ul>${obsItems}</ul>` : ""}
            ${planBlock}
            ${macroError}
            ${macroBlock}
        `,
    };
}

function renderChainSteps(data) {
    const steps = data.steps || [];
    if (!steps.length) {
        const text = "Chain returned no steps.";
        addLog("system", escapeHtml(text));
        pushChatEntry({ id: genChatEntryId(), kind: "system", text, ts: Date.now() });
        return;
    }

    steps.forEach((step, idx) => {
        const { kind, html } = buildChainStepHtml(step, idx);
        const msg = addLog(kind, html);
        if (msg) wireProposedMacroButtons(msg);
        pushChatEntry({
            id: genChatEntryId(),
            kind,
            payload: step,
            step_idx: idx,
            ts: Date.now(),
        });
    });

    if (data.terminated_early) {
        const text = `Chain terminated early after ${data.iterations_used} iteration(s).`;
        addLog("system", escapeHtml(text));
        pushChatEntry({ id: genChatEntryId(), kind: "system", text, ts: Date.now() });
    }
}

async function applyPreview() {
    if (!previewState) return;
    const before = snapshotGrid();
    const values = Array.isArray(previewState.values) ? previewState.values : [];
    const targetCell = previewState.original_request || previewState.target_cell || "A1";
    const hasChart = Boolean(previewState.chart_spec);
    try {
        setStatus("Applying");
        const res = await fetch(`${API_BASE}/agent/apply`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                sheet: workbook.active_sheet,
                agent_id: previewState.agent_id,
                target_cell: targetCell,
                values: values,
                shift_direction: "right",
                chart_spec: previewState.chart_spec || null,
            }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || "Apply failed.");
        if (data.chart_error) {
            addLog("system", escapeHtml(data.chart_error));
        }
        if (previewState.target_cell) selectedRange = { start: previewState.target_cell, end: previewState.target_cell };
        freezePreviewCard("applied");
        previewState = null;
        repaintPreview();
        await fetchGrid();
        recordAction(before);
        setStatus("Ready");
    } catch (error) {
        addLog("system", escapeHtml(`Apply failed: ${error.message}`));
        setStatus("Recover");
    }
}

function setStatus(text) {
    document.getElementById("status-pill").textContent = text;
}

function setScope(mode) {
    scopeMode = mode;
    document.querySelectorAll(".chip[data-scope]").forEach((button) => {
        button.classList.toggle("active", button.dataset.scope === mode);
    });
    syncSelectionUI();
}

function setChainMode(enabled) {
    chainMode = Boolean(enabled);
    const toggle = document.getElementById("chain-mode-toggle");
    const chip = toggle?.closest(".chip");
    if (toggle) toggle.checked = chainMode;
    if (chip) chip.classList.toggle("active", chainMode);
    if (chainMode && previewState) clearPreview();
}

function autoGrowInput() {
    const input = document.getElementById("assistant-input");
    if (!input) return;
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 160) + "px";
}

function syncSendButtonState() {
    const btn = document.getElementById("send-btn");
    const input = document.getElementById("assistant-input");
    if (btn && input) btn.disabled = input.value.trim().length === 0;
}

function toggleAssistant(force) {
    assistantOpen = typeof force === "boolean" ? force : !assistantOpen;
    document.getElementById("assistant-panel").classList.toggle("hidden", !assistantOpen);
}

function beginResize(kind, key, startPos) {
    resizeState = {
        kind,
        key,
        startPos,
        startSize: kind === "col" ? (colWidths[key] || DEFAULT_COL_WIDTH) : (rowHeights[key] || DEFAULT_ROW_HEIGHT),
    };
}

function handleDocumentMouseMove(event) {
    if (formulaPickState) {
        // Range refs (A1:B2) aren't supported by the kernel parser yet, so
        // hold the pick at the single anchor cell until dragging is wired up.
        return;
    }
    if (resizeState) {
        const delta = resizeState.kind === "col" ? event.clientX - resizeState.startPos : event.clientY - resizeState.startPos;
        const next = Math.max(resizeState.kind === "col" ? 72 : 24, resizeState.startSize + delta);
        if (resizeState.kind === "col") colWidths[resizeState.key] = next;
        else rowHeights[resizeState.key] = next;
        applyDimensions();
        return;
    }

    if (dragFillState) {
        const td = event.target.closest("td[data-cell]");
        if (!td) return;
        const start = getSelectedBounds();
        const end = a1ToCoords(td.dataset.cell);
        const tempCells = [];
        for (let row = Math.min(start.top, end.row); row <= Math.max(start.bottom, end.row); row++) {
            for (let col = Math.min(start.left, end.col); col <= Math.max(start.right, end.col); col++) {
                tempCells.push(coordsToA1(row, col));
            }
        }
        repaintPreview(tempCells);
        return;
    }

    if (!isSelecting || !selectionAnchor) return;
    const td = event.target.closest("td[data-cell]");
    if (td) setSelection(selectionAnchor, td.dataset.cell);
}

async function handleDocumentMouseUp(event) {
    if (formulaPickState) {
        finishFormulaPick();
        return;
    }
    if (resizeState) {
        resizeState = null;
        return;
    }
    if (dragFillState) {
        const td = event.target.closest("td[data-cell]");
        if (previewState) repaintPreview();
        else repaintPreview([]);
        if (td) await applyDragFill(td.dataset.cell);
        dragFillState = null;
        return;
    }
    isSelecting = false;
    selectionAnchor = null;
}

function attachGridEvents() {
    const table = document.getElementById("spreadsheet");
    table.addEventListener("mousedown", (event) => {
        const fillHandle = event.target.closest(".fill-handle");
        if (fillHandle) {
            dragFillState = { origin: selectionLabel() };
            event.preventDefault();
            return;
        }

        const colHandle = event.target.closest("[data-resize-col]");
        if (colHandle) {
            beginResize("col", colHandle.dataset.resizeCol, event.clientX);
            event.preventDefault();
            return;
        }

        const rowHandle = event.target.closest("[data-resize-row]");
        if (rowHandle) {
            beginResize("row", rowHandle.dataset.resizeRow, event.clientY);
            event.preventDefault();
            return;
        }

        const td = event.target.closest("td[data-cell]");
        if (td && isEditingFormula() && td.dataset.cell !== editingCell) {
            event.preventDefault();
            startFormulaPick(td.dataset.cell);
            return;
        }
        if (!td || editingCell) return;
        selectionAnchor = td.dataset.cell;
        isSelecting = true;
        setSelection(td.dataset.cell, td.dataset.cell);
    });

    table.addEventListener("dblclick", (event) => {
        const td = event.target.closest("td[data-cell]");
        if (td) startInlineEdit(td.dataset.cell);
    });

    table.addEventListener("contextmenu", (event) => {
        const td = event.target.closest("td[data-cell]");
        if (!td) return;
        event.preventDefault();
        const a1 = td.dataset.cell;
        if (!paintedSelection.has(a1)) setSelection(a1, a1);
        positionCtxMenu(event);
    });
}

function attachGlobalEvents() {
    document.addEventListener("mousemove", handleDocumentMouseMove);
    document.addEventListener("mouseup", handleDocumentMouseUp);

    document.addEventListener("keydown", async (event) => {
        const targetTag = document.activeElement?.tagName;
        const editingText = targetTag === "TEXTAREA" || targetTag === "INPUT";
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "c" && !editingText) {
            event.preventDefault();
            await copySelection();
            return;
        }
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "v" && !editingText) {
            const text = await navigator.clipboard.readText().catch(() => "");
            if (text) {
                event.preventDefault();
                await pasteSelection(text);
            }
            return;
        }
        if (!editingText) {
            await handleGridKeydown(event);
        }
    });

    document.addEventListener("paste", async (event) => {
        const editingText = ["TEXTAREA", "INPUT"].includes(document.activeElement?.tagName);
        if (editingText) return;
        const text = event.clipboardData?.getData("text/plain");
        if (text) {
            event.preventDefault();
            await pasteSelection(text);
        }
    });

    document.addEventListener("click", (event) => {
        const menu = document.getElementById("ctx-menu");
        if (menu && menu.style.display === "block" && !menu.contains(event.target)) {
            hideCtxMenu();
        }
        if (!event.target.closest(".menubar-menu-group")) {
            closeAllMenus();
        }
    });

    document.addEventListener("scroll", () => {
        hideCtxMenu();
        closeAllMenus();
    }, true);

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            hideCtxMenu();
            closeAllMenus();
        }
    });
}

async function clearActiveSheet() {
    const approved = window.confirm("Clear every unlocked cell in this tab?");
    if (!approved) return;
    const before = snapshotGrid();
    await fetch(`${API_BASE}/system/clear?sheet=${encodeURIComponent(workbook.active_sheet)}`, { method: "POST" });
    clearPreview();
    await fetchGrid();
    recordAction(before);
}

async function unlockAll() {
    const approved = window.confirm("Force-unlock every cell across every sheet? This is irreversible via the lock state itself.");
    if (!approved) return;
    const before = snapshotGrid();
    try {
        const res = await fetch(`${API_BASE}/system/unlock-all`, { method: "POST" });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Unlock failed.");
        addLog("system", `Unlocked ${data.unlocked} cell${data.unlocked === 1 ? "" : "s"}, dropped ${data.dropped} empty placeholder${data.dropped === 1 ? "" : "s"}.`);
        await fetchGrid();
        recordAction(before);
    } catch (error) {
        addLog("system", escapeHtml(`Unlock failed: ${error.message}`));
    }
}

// ======== Undo / Redo ========

function snapshotGrid() {
    const snapshot = {};
    Object.entries(gridData).forEach(([a1, state]) => {
        snapshot[a1] = getCellDisplay(state);
    });
    return { sheet: workbook.active_sheet, cells: snapshot };
}

function recordAction(beforeSnapshot) {
    if (!beforeSnapshot) return;
    undoStack.push(beforeSnapshot);
    if (undoStack.length > UNDO_LIMIT) undoStack.shift();
    redoStack = [];
    refreshUndoRedoButtons();
}

function refreshUndoRedoButtons() {
    const undoBtn = document.getElementById("undo-btn");
    const redoBtn = document.getElementById("redo-btn");
    if (undoBtn) undoBtn.disabled = undoStack.length === 0;
    if (redoBtn) redoBtn.disabled = redoStack.length === 0;
}

async function restoreSnapshot(target) {
    if (target.sheet && target.sheet !== workbook.active_sheet) {
        try {
            await fetch(`${API_BASE}/workbook/sheet/activate`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name: target.sheet }),
            });
            await fetchWorkbook();
        } catch {
            // best-effort; if the sheet no longer exists we'll fall through
        }
    }

    const currentCells = {};
    Object.entries(gridData).forEach(([a1, state]) => {
        currentCells[a1] = getCellDisplay(state);
    });

    const allCells = new Set([...Object.keys(target.cells), ...Object.keys(currentCells)]);
    for (const a1 of allCells) {
        const desired = target.cells[a1] || "";
        const current = currentCells[a1] || "";
        if (desired === current) continue;
        try {
            await persistSingleCell(a1, desired);
        } catch {
            // skip locked cells or other rejections silently
        }
    }
    await fetchGrid();
}

async function undo() {
    if (!undoStack.length) return;
    const target = undoStack.pop();
    const redoSnap = snapshotGrid();
    redoStack.push(redoSnap);
    setStatus("Undo");
    await restoreSnapshot(target);
    setStatus("Ready");
    refreshUndoRedoButtons();
}

async function redo() {
    if (!redoStack.length) return;
    const target = redoStack.pop();
    const undoSnap = snapshotGrid();
    undoStack.push(undoSnap);
    setStatus("Redo");
    await restoreSnapshot(target);
    setStatus("Ready");
    refreshUndoRedoButtons();
}

// ======== Selection stats ========

function updateSelectionStats() {
    const sumEl = document.getElementById("stats-sum");
    const avgEl = document.getElementById("stats-avg");
    const countEl = document.getElementById("stats-count");
    if (!sumEl || !avgEl || !countEl) return;

    let sum = 0;
    let numericCount = 0;
    let nonEmptyCount = 0;
    getSelectedCells().forEach((a1) => {
        const state = gridData[a1];
        if (!state) return;
        if (state.value !== null && state.value !== undefined && state.value !== "") nonEmptyCount++;
        const num = Number(state.value);
        if (state.value !== null && state.value !== undefined && state.value !== "" && !Number.isNaN(num)) {
            sum += num;
            numericCount++;
        }
    });

    const fmt = (n) => {
        if (!Number.isFinite(n)) return "0";
        return Math.abs(n) >= 10000 || Number.isInteger(n) ? n.toLocaleString() : n.toFixed(2).replace(/\.?0+$/, "");
    };

    sumEl.textContent = fmt(sum);
    avgEl.textContent = numericCount ? fmt(sum / numericCount) : "0";
    countEl.textContent = String(nonEmptyCount);
}

// ======== Context menu ========

function positionCtxMenu(event) {
    const menu = document.getElementById("ctx-menu");
    if (!menu) return;
    menu.style.display = "block";
    const rect = menu.getBoundingClientRect();
    const maxX = window.innerWidth - rect.width - 4;
    const maxY = window.innerHeight - rect.height - 4;
    menu.style.left = `${Math.min(event.clientX, maxX)}px`;
    menu.style.top = `${Math.min(event.clientY, maxY)}px`;
}

function hideCtxMenu() {
    const menu = document.getElementById("ctx-menu");
    if (menu) menu.style.display = "none";
}

async function handleCtxAction(action) {
    hideCtxMenu();
    if (action === "copy") {
        await copySelection();
    } else if (action === "cut") {
        await copySelection();
        await clearSelection();
    } else if (action === "paste") {
        const text = await navigator.clipboard.readText().catch(() => "");
        if (text) await pasteSelection(text);
    } else if (action === "clear") {
        await clearSelection();
    }
}

async function clearSelection() {
    const cells = getSelectedCells();
    if (!cells.length) return;
    const before = snapshotGrid();
    let changed = false;
    for (const a1 of cells) {
        try {
            await persistSingleCell(a1, "");
            changed = true;
        } catch {
            // ignore locked or failed cells
        }
    }
    if (changed) {
        await fetchGrid();
        recordAction(before);
    }
}

// ======== Keyboard navigation ========

function moveSelection(rowDelta, colDelta, extend) {
    const anchor = a1ToCoords(extend ? selectedRange.start : selectedRange.end);
    const end = a1ToCoords(selectedRange.end);
    const nextRow = Math.max(0, Math.min(ROW_COUNT - 1, end.row + rowDelta));
    const nextCol = Math.max(0, Math.min(COLUMN_COUNT - 1, end.col + colDelta));
    const nextCell = coordsToA1(nextRow, nextCol);
    if (extend) {
        setSelection(coordsToA1(anchor.row, anchor.col), nextCell);
    } else {
        setSelection(nextCell, nextCell);
    }
    scrollCellIntoView(nextCell);
}

function scrollCellIntoView(a1) {
    const td = cellEls.get(a1);
    const wrap = document.getElementById("sheet-wrap");
    if (!td || !wrap) return;
    const tdRect = td.getBoundingClientRect();
    const wrapRect = wrap.getBoundingClientRect();
    if (tdRect.top < wrapRect.top + 34) wrap.scrollTop -= wrapRect.top + 34 - tdRect.top;
    if (tdRect.bottom > wrapRect.bottom) wrap.scrollTop += tdRect.bottom - wrapRect.bottom;
    if (tdRect.left < wrapRect.left + 50) wrap.scrollLeft -= wrapRect.left + 50 - tdRect.left;
    if (tdRect.right > wrapRect.right) wrap.scrollLeft += tdRect.right - wrapRect.right;
}

async function handleGridKeydown(event) {
    const activeTag = document.activeElement?.tagName;
    const isEditing = activeTag === "TEXTAREA" || activeTag === "INPUT";
    if (isEditing) return;

    const key = event.key;
    const meta = event.ctrlKey || event.metaKey;

    if (meta && key.toLowerCase() === "z" && !event.shiftKey) {
        event.preventDefault();
        await undo();
        return;
    }
    if (meta && (key.toLowerCase() === "y" || (key.toLowerCase() === "z" && event.shiftKey))) {
        event.preventDefault();
        await redo();
        return;
    }
    if (meta && key.toLowerCase() === "s") {
        event.preventDefault();
        await saveWorkbook();
        return;
    }

    if (key === "ArrowUp") { event.preventDefault(); moveSelection(-1, 0, event.shiftKey); return; }
    if (key === "ArrowDown") { event.preventDefault(); moveSelection(1, 0, event.shiftKey); return; }
    if (key === "ArrowLeft") { event.preventDefault(); moveSelection(0, -1, event.shiftKey); return; }
    if (key === "ArrowRight") { event.preventDefault(); moveSelection(0, 1, event.shiftKey); return; }
    if (key === "Tab") { event.preventDefault(); moveSelection(0, event.shiftKey ? -1 : 1, false); return; }
    if (key === "Enter") { event.preventDefault(); startInlineEdit(selectedRange.end); return; }
    if (key === "F2") { event.preventDefault(); startInlineEdit(selectedRange.end); return; }
    if (key === "Delete" || key === "Backspace") { event.preventDefault(); await clearSelection(); return; }

    if (key.length === 1 && !meta && !event.altKey) {
        event.preventDefault();
        startInlineEdit(selectedRange.end, key);
    }
}

// ======== Workbook save/load ========

async function saveWorkbook() {
    try {
        setStatus("Saving");
        const res = await fetch(`${API_BASE}/system/export`, { method: "GET" });
        if (!res.ok) throw new Error(`Export failed (${res.status})`);
        const blob = await res.blob();
        const disposition = res.headers.get("Content-Disposition") || "";
        const match = disposition.match(/filename="([^"]+)"/);
        const filename = match ? match[1] : "workbook.gridos";
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = filename;
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        URL.revokeObjectURL(url);
        setStatus("Saved");
        addLog("system", `Workbook downloaded as ${escapeHtml(filename)}.`);
    } catch (error) {
        setStatus("Recover");
        addLog("system", escapeHtml(`Save failed: ${error.message}`));
    }
}

function pickWorkbookFile() {
    return new Promise((resolve) => {
        const input = document.createElement("input");
        input.type = "file";
        input.accept = ".gridos,.json,application/json";
        input.addEventListener("change", () => {
            resolve(input.files && input.files[0] ? input.files[0] : null);
        });
        input.addEventListener("cancel", () => resolve(null));
        input.click();
    });
}

async function loadWorkbook() {
    const file = await pickWorkbookFile();
    if (!file) return;
    try {
        setStatus("Loading");
        const text = await file.text();
        let payload;
        try {
            payload = JSON.parse(text);
        } catch (e) {
            throw new Error("Selected file is not valid JSON.");
        }
        const before = snapshotGrid();
        const res = await fetch(`${API_BASE}/system/import`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Import failed.");
        await fetchWorkbook({ rehydrateChat: true });
        await fetchGrid();
        recordAction(before);
        setStatus("Ready");
        addLog("system", `Workbook loaded from ${escapeHtml(file.name)}.`);
    } catch (error) {
        setStatus("Recover");
        addLog("system", escapeHtml(`Load failed: ${error.message}`));
    }
}

// ======== Menubar dropdowns ========

function closeAllMenus() {
    document.querySelectorAll(".menu-dropdown.open").forEach((el) => el.classList.remove("open"));
    document.querySelectorAll(".menubar-menu-btn.open").forEach((el) => el.classList.remove("open"));
}

function toggleMenu(name, anchorBtn) {
    const dropdown = document.getElementById(`menu-${name}`);
    if (!dropdown) return;
    const wasOpen = dropdown.classList.contains("open");
    closeAllMenus();
    if (!wasOpen) {
        dropdown.classList.add("open");
        anchorBtn?.classList.add("open");
    }
}

async function handleMenuAction(action) {
    closeAllMenus();
    switch (action) {
        case "new-sheet":
            await createSheet();
            break;
        case "save":
            await saveWorkbook();
            break;
        case "load":
            await loadWorkbook();
            break;
        case "clear-sheet":
            await clearActiveSheet();
            break;
        case "unlock-all":
            await unlockAll();
            break;
        case "sign-out":
            await signOut();
            break;
        case "undo":
            await undo();
            break;
        case "redo":
            await redo();
            break;
        case "cut":
            await copySelection();
            await clearSelection();
            break;
        case "copy":
            await copySelection();
            break;
        case "paste": {
            const text = await navigator.clipboard.readText().catch(() => "");
            if (text) await pasteSelection(text);
            break;
        }
        case "clear":
            await clearSelection();
            break;
        case "toggle-assistant":
            toggleAssistant();
            break;
        case "toggle-charts":
            toggleChartsPanel();
            break;
        case "insert-chart":
            openChartModal();
            break;
        case "reset-column-widths":
            colWidths = {};
            rowHeights = {};
            applyDimensions();
            addLog("system", "Column and row sizes reset.");
            break;
        case "open-library-templates":
            openLibraryModal("templates");
            break;
        case "open-library-tools":
            openLibraryModal("tools");
            break;
    }
}

// ======== LLM providers: model picker + settings modal ========

async function refreshModelCatalog() {
    try {
        const res = await fetch(`${API_BASE}/models/available`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        modelCatalog = await res.json();
    } catch (e) {
        modelCatalog = { models: [], default_model_id: null, configured_providers: [] };
    }
    const saved = localStorage.getItem(MODEL_PREF_KEY);
    const savedAvailable = modelCatalog.models.some((m) => m.id === saved && m.available);
    if (savedAvailable) {
        selectedModelId = saved;
    } else {
        selectedModelId = modelCatalog.default_model_id;
    }
    renderModelSelect();
}

function renderModelSelect() {
    const select = document.getElementById("model-select");
    if (!select) return;
    select.innerHTML = "";
    const available = modelCatalog.models.filter((m) => m.available);
    if (available.length === 0) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "No key configured";
        opt.disabled = true;
        opt.selected = true;
        select.appendChild(opt);
        select.classList.add("unset");
        select.disabled = true;
        return;
    }
    select.classList.remove("unset");
    select.disabled = false;
    available.forEach((m) => {
        const opt = document.createElement("option");
        opt.value = m.id;
        opt.textContent = m.display_name;
        if (m.id === selectedModelId) opt.selected = true;
        select.appendChild(opt);
    });
    if (!selectedModelId || !available.some((m) => m.id === selectedModelId)) {
        selectedModelId = available[0].id;
        select.value = selectedModelId;
    }
}

function onModelSelectChange(event) {
    const id = event.target.value;
    if (!id) return;
    selectedModelId = id;
    localStorage.setItem(MODEL_PREF_KEY, id);
}

async function openSettingsModal() {
    const backdrop = document.getElementById("settings-modal-backdrop");
    if (!backdrop) return;
    backdrop.removeAttribute("hidden");
    await renderSettingsProviders();
}

function closeSettingsModal() {
    const backdrop = document.getElementById("settings-modal-backdrop");
    if (backdrop) backdrop.setAttribute("hidden", "");
}

async function renderSettingsProviders() {
    const container = document.getElementById("settings-providers-list");
    if (!container) return;
    container.innerHTML = `<div class="hint">Loading…</div>`;
    try {
        const res = await fetch(`${API_BASE}/settings/providers`);
        const data = await res.json();
        const providers = data.providers || [];
        container.innerHTML = "";
        providers.forEach((p) => container.appendChild(renderProviderRow(p)));
    } catch (e) {
        container.innerHTML = `<div class="settings-provider-error">Could not load providers: ${escapeHtml(e.message)}</div>`;
    }
}

function renderProviderRow(provider) {
    const wrap = document.createElement("div");
    wrap.className = "settings-provider";
    const statusClass = provider.configured ? "on" : "off";
    const statusText = provider.configured ? "Configured" : "Not configured";

    wrap.innerHTML = `
        <div class="settings-provider-head">
            <h4>${escapeHtml(provider.display_name)}</h4>
            <span class="settings-provider-status ${statusClass}">${statusText}</span>
        </div>
        ${provider.configured
            ? `<div class="settings-provider-row">
                   <div class="settings-provider-masked">${escapeHtml(provider.masked_key || "•••••••")}</div>
                   <button type="button" class="ghost-btn" data-action="replace">Replace</button>
                   <button type="button" class="ghost-btn" data-action="delete">Remove</button>
               </div>`
            : `<div class="settings-provider-row">
                   <input type="password" placeholder="Paste API key" autocomplete="off" spellcheck="false" />
                   <button type="button" class="primary-btn" data-action="save">Save</button>
               </div>`
        }
        <div class="settings-provider-error" data-role="error"></div>
    `;

    const errEl = wrap.querySelector('[data-role="error"]');
    const setErr = (msg) => { errEl.textContent = msg || ""; };

    wrap.querySelector('[data-action="save"]')?.addEventListener("click", async () => {
        const input = wrap.querySelector('input[type="password"]');
        const key = (input?.value || "").trim();
        if (!key) { setErr("Paste an API key first."); return; }
        setErr("Saving…");
        try {
            const res = await fetch(`${API_BASE}/settings/keys/save`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ provider: provider.id, api_key: key }),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || "Save failed.");
            setErr("");
            await renderSettingsProviders();
            await refreshModelCatalog();
        } catch (e) {
            setErr(e.message);
        }
    });

    wrap.querySelector('[data-action="replace"]')?.addEventListener("click", () => {
        const head = wrap.querySelector(".settings-provider-row");
        head.innerHTML = `
            <input type="password" placeholder="Paste new API key" autocomplete="off" spellcheck="false" />
            <button type="button" class="primary-btn" data-action="save">Save</button>
            <button type="button" class="ghost-btn" data-action="cancel">Cancel</button>
        `;
        head.querySelector('[data-action="save"]').addEventListener("click", async () => {
            const input = head.querySelector('input[type="password"]');
            const key = (input?.value || "").trim();
            if (!key) { setErr("Paste an API key first."); return; }
            setErr("Saving…");
            try {
                const res = await fetch(`${API_BASE}/settings/keys/save`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ provider: provider.id, api_key: key }),
                });
                const data = await res.json();
                if (!res.ok) throw new Error(data.detail || "Save failed.");
                setErr("");
                await renderSettingsProviders();
                await refreshModelCatalog();
            } catch (e) {
                setErr(e.message);
            }
        });
        head.querySelector('[data-action="cancel"]').addEventListener("click", renderSettingsProviders);
    });

    wrap.querySelector('[data-action="delete"]')?.addEventListener("click", async () => {
        if (!confirm(`Remove the ${provider.display_name} API key?`)) return;
        setErr("Removing…");
        try {
            const res = await fetch(`${API_BASE}/settings/keys/${encodeURIComponent(provider.id)}`, { method: "DELETE" });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || "Delete failed.");
            setErr("");
            await renderSettingsProviders();
            await refreshModelCatalog();
        } catch (e) {
            setErr(e.message);
        }
    });

    return wrap;
}

function attachSettingsEvents() {
    document.getElementById("settings-btn")?.addEventListener("click", openSettingsModal);
    document.getElementById("settings-modal-close")?.addEventListener("click", closeSettingsModal);
    document.getElementById("settings-modal-backdrop")?.addEventListener("click", (e) => {
        if (e.target === e.currentTarget) closeSettingsModal();
    });
    document.getElementById("composer-settings-link")?.addEventListener("click", openSettingsModal);
    document.getElementById("model-select")?.addEventListener("change", onModelSelectChange);
}

// ======== Library ========

let libraryToolsCache = null;

async function openLibraryModal(tab = "templates") {
    const backdrop = document.getElementById("library-modal-backdrop");
    if (!backdrop) return;
    backdrop.removeAttribute("hidden");
    switchLibraryTab(tab);
    clearLibraryForms();
    await Promise.all([refreshTemplateList(), refreshToolsTab()]);
}

function closeLibraryModal() {
    const backdrop = document.getElementById("library-modal-backdrop");
    if (backdrop) backdrop.setAttribute("hidden", "");
}

function switchLibraryTab(tab) {
    document.querySelectorAll("[data-library-tab]").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.libraryTab === tab);
    });
    document.querySelectorAll("[data-library-panel]").forEach((panel) => {
        panel.classList.toggle("active", panel.dataset.libraryPanel === tab);
    });
}

function clearLibraryForms() {
    ["template-save-name", "template-save-desc", "macro-form-name", "macro-form-params", "macro-form-desc", "macro-form-body"].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.value = "";
    });
    setLibraryError("template-save-error", "");
    setLibraryError("macro-form-error", "");
}

function setLibraryError(elId, message) {
    const el = document.getElementById(elId);
    if (el) el.textContent = message || "";
}

async function refreshTemplateList() {
    const list = document.getElementById("template-list");
    const empty = document.getElementById("template-empty");
    if (!list) return;
    list.innerHTML = "";
    try {
        const res = await fetch(`${API_BASE}/templates/list`);
        const data = await res.json();
        const items = data.templates || [];
        if (!items.length) {
            empty.removeAttribute("hidden");
            return;
        }
        empty.setAttribute("hidden", "");
        items.forEach((tpl) => list.appendChild(renderTemplateItem(tpl)));
    } catch (e) {
        empty.removeAttribute("hidden");
        empty.textContent = `Could not load templates: ${e.message}`;
    }
}

function renderTemplateItem(tpl) {
    const li = document.createElement("li");
    li.className = "library-list-item";
    const author = tpl.author || "You";
    const isPreset = author !== "You";
    if (isPreset) li.classList.add("is-preset");
    const name = document.createElement("div");
    const badgeClass = isPreset ? "author-badge preset" : "author-badge user";
    name.innerHTML = `<strong>${escapeHtml(tpl.name || tpl.id)}</strong> <span class="${badgeClass}">${escapeHtml(author)}</span>`;
    const meta = document.createElement("div");
    meta.className = "meta";
    const when = tpl.created_at ? new Date(tpl.created_at).toLocaleString() : "unknown";
    const desc = tpl.description ? ` · ${escapeHtml(tpl.description)}` : "";
    meta.innerHTML = `${when} · ${tpl.sheet_count || 0} sheet${tpl.sheet_count === 1 ? "" : "s"} · ${tpl.cell_count || 0} cells${desc}`;
    const actions = document.createElement("div");
    actions.className = "actions";

    const applyBtn = document.createElement("button");
    applyBtn.type = "button";
    applyBtn.className = "primary";
    applyBtn.textContent = "Load";
    applyBtn.addEventListener("click", () => confirmAndApplyTemplate(tpl, li));

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "danger";
    delBtn.textContent = "Delete";
    delBtn.addEventListener("click", () => deleteTemplate(tpl.id));

    actions.appendChild(applyBtn);
    actions.appendChild(delBtn);
    li.appendChild(name);
    li.appendChild(meta);
    li.appendChild(actions);
    return li;
}

function confirmAndApplyTemplate(tpl, parentLi) {
    const existing = parentLi.querySelector(".library-confirm");
    if (existing) { existing.remove(); return; }
    const warn = document.createElement("div");
    warn.className = "library-confirm";
    warn.innerHTML = `<span>Loading <strong>${escapeHtml(tpl.name || tpl.id)}</strong> will clear all unlocked cells on this workbook. Locked cells are preserved.</span>`;
    const ok = document.createElement("button");
    ok.type = "button";
    ok.className = "primary";
    ok.textContent = "Apply";
    ok.addEventListener("click", async () => {
        ok.disabled = true;
        await applyTemplate(tpl.id);
        warn.remove();
    });
    warn.appendChild(ok);
    parentLi.appendChild(warn);
}

async function applyTemplate(id) {
    const before = snapshotGrid();
    try {
        const res = await fetch(`${API_BASE}/templates/apply/${encodeURIComponent(id)}`, { method: "POST" });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Could not apply template.");
        addLog("system", `Template applied: ${data.applied} cell${data.applied === 1 ? "" : "s"}, ${data.skipped_locked} skipped (locked).`);
        closeLibraryModal();
        await fetchGrid();
        recordAction(before);
    } catch (e) {
        addLog("system", escapeHtml(`Template apply failed: ${e.message}`));
    }
}

async function deleteTemplate(id) {
    try {
        const res = await fetch(`${API_BASE}/templates/${encodeURIComponent(id)}`, { method: "DELETE" });
        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            throw new Error(data.detail || "Could not delete template.");
        }
        await refreshTemplateList();
        addLog("system", "Template deleted.");
    } catch (e) {
        addLog("system", escapeHtml(`Template delete failed: ${e.message}`));
    }
}

async function saveCurrentAsTemplate() {
    const name = document.getElementById("template-save-name").value.trim();
    const desc = document.getElementById("template-save-desc").value.trim();
    setLibraryError("template-save-error", "");
    if (!name) {
        setLibraryError("template-save-error", "Name is required.");
        return;
    }
    try {
        const res = await fetch(`${API_BASE}/templates/save`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name, description: desc }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Could not save template.");
        document.getElementById("template-save-name").value = "";
        document.getElementById("template-save-desc").value = "";
        addLog("system", `Template saved: <strong>${escapeHtml(name)}</strong>.`);
        await refreshTemplateList();
    } catch (e) {
        setLibraryError("template-save-error", e.message);
    }
}

async function refreshToolsTab() {
    try {
        const res = await fetch(`${API_BASE}/tools/list`);
        const data = await res.json();
        libraryToolsCache = data;
        renderPrimitives(data.primitives || []);
        renderMacros(data.macros || []);
        renderHeroTools(data.hero_tools || []);
    } catch (e) {
        addLog("system", escapeHtml(`Could not load tools: ${e.message}`));
    }
}

function renderPrimitives(primitives) {
    const container = document.getElementById("primitives-chips");
    if (!container) return;
    container.innerHTML = "";
    primitives.forEach((p) => {
        const chip = document.createElement("span");
        chip.className = "library-chip";
        chip.textContent = p.name;
        container.appendChild(chip);
    });
}

function renderMacros(macros) {
    const list = document.getElementById("macro-list");
    const empty = document.getElementById("macro-empty");
    if (!list) return;
    list.innerHTML = "";
    if (!macros.length) {
        empty.removeAttribute("hidden");
        return;
    }
    empty.setAttribute("hidden", "");
    macros.forEach((macro) => {
        const li = document.createElement("li");
        li.className = "library-list-item";
        const name = document.createElement("div");
        name.innerHTML = `<strong>${escapeHtml(macro.name)}(${(macro.params || []).map(escapeHtml).join(", ")})</strong>`;
        const meta = document.createElement("div");
        meta.className = "meta";
        const descPrefix = macro.description ? `${escapeHtml(macro.description)} · ` : "";
        meta.innerHTML = `${descPrefix}<code>${escapeHtml(macro.body)}</code>`;
        const actions = document.createElement("div");
        actions.className = "actions";
        const editBtn = document.createElement("button");
        editBtn.type = "button";
        editBtn.textContent = "Edit";
        editBtn.addEventListener("click", () => loadMacroIntoForm(macro));
        const delBtn = document.createElement("button");
        delBtn.type = "button";
        delBtn.className = "danger";
        delBtn.textContent = "Delete";
        delBtn.addEventListener("click", () => deleteMacro(macro.name));
        actions.appendChild(editBtn);
        actions.appendChild(delBtn);
        li.appendChild(name);
        li.appendChild(meta);
        li.appendChild(actions);
        list.appendChild(li);
    });
}

function loadMacroIntoForm(macro) {
    document.getElementById("macro-form-name").value = macro.name || "";
    document.getElementById("macro-form-params").value = (macro.params || []).join(", ");
    document.getElementById("macro-form-desc").value = macro.description || "";
    document.getElementById("macro-form-body").value = macro.body || "";
    setLibraryError("macro-form-error", "");
    document.getElementById("macro-form-name").focus();
}

async function saveMacro() {
    const name = document.getElementById("macro-form-name").value.trim();
    const paramsRaw = document.getElementById("macro-form-params").value;
    const desc = document.getElementById("macro-form-desc").value.trim();
    const body = document.getElementById("macro-form-body").value.trim();
    setLibraryError("macro-form-error", "");
    if (!name) { setLibraryError("macro-form-error", "Name is required."); return; }
    if (!body) { setLibraryError("macro-form-error", "Body is required."); return; }
    const params = paramsRaw.split(",").map((s) => s.trim()).filter(Boolean);
    try {
        const res = await fetch(`${API_BASE}/tools/save_macro`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name, description: desc, params, body }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Could not save macro.");
        addLog("system", `Macro saved: <strong>${escapeHtml(name.toUpperCase())}</strong>.`);
        clearLibraryForms();
        await refreshToolsTab();
    } catch (e) {
        setLibraryError("macro-form-error", e.message);
    }
}

async function deleteMacro(name) {
    try {
        const res = await fetch(`${API_BASE}/tools/macros/${encodeURIComponent(name)}`, { method: "DELETE" });
        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            throw new Error(data.detail || "Could not delete macro.");
        }
        addLog("system", `Macro deleted: ${escapeHtml(name)}.`);
        await refreshToolsTab();
    } catch (e) {
        addLog("system", escapeHtml(`Macro delete failed: ${e.message}`));
    }
}

function renderHeroTools(tools) {
    const container = document.getElementById("hero-tools-list");
    if (!container) return;
    container.innerHTML = "";
    tools.forEach((tool) => {
        const row = document.createElement("div");
        row.className = "library-toggle-row";
        const info = document.createElement("div");
        info.className = "info";
        info.innerHTML = `<strong>${escapeHtml(tool.display_name)}</strong><p>${escapeHtml(tool.description)}</p>`;
        const label = document.createElement("label");
        label.className = "library-toggle";
        const input = document.createElement("input");
        input.type = "checkbox";
        input.checked = !!tool.enabled;
        input.addEventListener("change", () => toggleHeroTool(tool.id, input.checked));
        const slider = document.createElement("span");
        label.appendChild(input);
        label.appendChild(slider);
        row.appendChild(info);
        row.appendChild(label);
        container.appendChild(row);
    });
}

async function toggleHeroTool(id, enabled) {
    try {
        const res = await fetch(`${API_BASE}/tools/hero/toggle`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ tool_id: id, enabled }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Could not toggle hero tool.");
        addLog("system", `${escapeHtml(id)}: ${data.enabled ? "enabled" : "disabled"}.`);
    } catch (e) {
        addLog("system", escapeHtml(`Hero toggle failed: ${e.message}`));
        await refreshToolsTab();
    }
}

// ======== Charts ========

function parseA1Range(rangeStr) {
    if (!rangeStr) return null;
    const clean = rangeStr.trim().toUpperCase();
    const [startRaw, endRaw] = clean.includes(":") ? clean.split(":") : [clean, clean];
    const startMatch = /^([A-Z]+)(\d+)$/.exec(startRaw);
    const endMatch = /^([A-Z]+)(\d+)$/.exec(endRaw);
    if (!startMatch || !endMatch) return null;
    const start = a1ToCoords(startRaw);
    const end = a1ToCoords(endRaw);
    return {
        top: Math.min(start.row, end.row),
        bottom: Math.max(start.row, end.row),
        left: Math.min(start.col, end.col),
        right: Math.max(start.col, end.col),
    };
}

function cellNumeric(a1) {
    const state = gridData[a1];
    if (!state) return 0;
    const v = state.value;
    if (typeof v === "number") return v;
    if (v === null || v === undefined || v === "") return 0;
    const n = Number(v);
    return Number.isFinite(n) ? n : 0;
}

function cellText(a1) {
    const state = gridData[a1];
    if (!state || state.value === null || state.value === undefined) return "";
    return String(state.value);
}

function buildChartData(spec) {
    const range = parseA1Range(spec.data_range);
    if (!range) return null;
    const byColumns = (spec.orientation || "columns") === "columns";

    if (byColumns) {
        // First column: labels. Remaining columns: series. Each row = one label.
        const labels = [];
        for (let r = range.top; r <= range.bottom; r++) {
            labels.push(cellText(coordsToA1(r, range.left)));
        }
        const datasets = [];
        for (let c = range.left + 1; c <= range.right; c++) {
            const seriesName = cellText(coordsToA1(range.top, c)) || coordsToA1(range.top, c);
            // If the first row looks like a header (non-numeric), skip it.
            const firstCellVal = gridData[coordsToA1(range.top, c)]?.value;
            const headerIsText = typeof firstCellVal === "string" && isNaN(Number(firstCellVal));
            const startRow = headerIsText ? range.top + 1 : range.top;
            const data = [];
            for (let r = startRow; r <= range.bottom; r++) {
                data.push(cellNumeric(coordsToA1(r, c)));
            }
            if (headerIsText) {
                // drop the header label from labels for this dataset only — but Chart.js shares labels,
                // so instead slice labels once (outside the loop) when any series has a header.
            }
            datasets.push({
                label: seriesName,
                data,
                backgroundColor: CHART_PALETTE[(datasets.length) % CHART_PALETTE.length],
                borderColor: CHART_PALETTE[(datasets.length) % CHART_PALETTE.length],
            });
        }
        // If the first label cell itself looks like a header for the label column, drop the first label.
        const firstLabelState = gridData[coordsToA1(range.top, range.left)];
        const firstLabelIsText = firstLabelState && typeof firstLabelState.value === "string" && isNaN(Number(firstLabelState.value));
        if (firstLabelIsText && datasets.some(ds => ds.data.length === labels.length - 1)) {
            labels.shift();
        }
        // Normalize: if some datasets skipped header and others didn't, trim all to min length.
        const minLen = Math.min(labels.length, ...datasets.map(ds => ds.data.length));
        datasets.forEach(ds => { ds.data = ds.data.slice(0, minLen); });
        return { labels: labels.slice(0, minLen), datasets };
    } else {
        // orientation = rows. First row: labels. Remaining rows: series.
        const labels = [];
        for (let c = range.left; c <= range.right; c++) {
            labels.push(cellText(coordsToA1(range.top, c)));
        }
        const datasets = [];
        for (let r = range.top + 1; r <= range.bottom; r++) {
            const seriesName = cellText(coordsToA1(r, range.left)) || coordsToA1(r, range.left);
            const firstCellVal = gridData[coordsToA1(r, range.left)]?.value;
            const headerIsText = typeof firstCellVal === "string" && isNaN(Number(firstCellVal));
            const startCol = headerIsText ? range.left + 1 : range.left;
            const data = [];
            for (let c = startCol; c <= range.right; c++) {
                data.push(cellNumeric(coordsToA1(r, c)));
            }
            datasets.push({
                label: seriesName,
                data,
                backgroundColor: CHART_PALETTE[(datasets.length) % CHART_PALETTE.length],
                borderColor: CHART_PALETTE[(datasets.length) % CHART_PALETTE.length],
            });
        }
        const firstLabelState = gridData[coordsToA1(range.top, range.left)];
        const firstLabelIsText = firstLabelState && typeof firstLabelState.value === "string" && isNaN(Number(firstLabelState.value));
        if (firstLabelIsText && datasets.some(ds => ds.data.length === labels.length - 1)) {
            labels.shift();
        }
        const minLen = Math.min(labels.length, ...datasets.map(ds => ds.data.length));
        datasets.forEach(ds => { ds.data = ds.data.slice(0, minLen); });
        return { labels: labels.slice(0, minLen), datasets };
    }
}

function positionChartOverlay(el, spec) {
    const anchor = cellEls.get(spec.anchor_cell);
    if (!anchor) {
        el.style.display = "none";
        return;
    }
    el.style.display = "flex";
    el.style.left = `${anchor.offsetLeft}px`;
    el.style.top = `${anchor.offsetTop}px`;
    if (minimizedChartIds.has(spec.id)) {
        el.style.width = "";
        el.style.height = "";
    } else {
        el.style.width = `${spec.width || 400}px`;
        el.style.height = `${spec.height || 280}px`;
    }
}

function setChartMinimized(id, minimized) {
    const overlay = chartOverlayEls.get(id);
    if (!overlay) return;
    if (minimized) minimizedChartIds.add(id);
    else minimizedChartIds.delete(id);
    overlay.classList.toggle("minimized", minimized);
    const btn = overlay.querySelector(".chart-overlay-btn.minimize");
    if (btn) {
        btn.textContent = minimized ? "▢" : "–";
        btn.title = minimized ? "Restore chart" : "Minimize chart";
    }
    const spec = sheetCharts.find(c => c.id === id);
    if (spec) positionChartOverlay(overlay, spec);
    const inst = chartInstances.get(id);
    if (inst && !minimized) {
        requestAnimationFrame(() => inst.resize());
    }
}

function destroyChartInstance(id) {
    const inst = chartInstances.get(id);
    if (inst) {
        inst.destroy();
        chartInstances.delete(id);
    }
    const el = chartOverlayEls.get(id);
    if (el && el.parentNode) el.parentNode.removeChild(el);
    chartOverlayEls.delete(id);
}

function pruneMinimizedIds() {
    const keep = new Set(sheetCharts.map(c => c.id));
    Array.from(minimizedChartIds).forEach(id => { if (!keep.has(id)) minimizedChartIds.delete(id); });
}

function renderSingleChart(spec) {
    const layer = document.getElementById("chart-layer");
    if (!layer || typeof Chart === "undefined") return;

    destroyChartInstance(spec.id);

    const overlay = document.createElement("div");
    overlay.className = "chart-overlay";
    overlay.dataset.chartId = spec.id;
    if (minimizedChartIds.has(spec.id)) overlay.classList.add("minimized");

    const header = document.createElement("div");
    header.className = "chart-overlay-header";
    const titleEl = document.createElement("div");
    titleEl.className = "chart-overlay-title";
    titleEl.textContent = spec.title || "(untitled chart)";
    titleEl.title = `${spec.data_range} · ${spec.chart_type}`;
    const actions = document.createElement("div");
    actions.className = "chart-overlay-actions";
    const minBtn = document.createElement("button");
    minBtn.className = "chart-overlay-btn minimize";
    minBtn.type = "button";
    const startsMinimized = minimizedChartIds.has(spec.id);
    minBtn.textContent = startsMinimized ? "▢" : "–";
    minBtn.title = startsMinimized ? "Restore chart" : "Minimize chart";
    minBtn.addEventListener("click", () => setChartMinimized(spec.id, !minimizedChartIds.has(spec.id)));
    const editBtn = document.createElement("button");
    editBtn.className = "chart-overlay-btn";
    editBtn.type = "button";
    editBtn.textContent = "Edit";
    editBtn.addEventListener("click", () => openChartModal(spec.id));
    const closeBtn = document.createElement("button");
    closeBtn.className = "chart-overlay-btn";
    closeBtn.type = "button";
    closeBtn.textContent = "×";
    closeBtn.title = "Delete chart";
    closeBtn.addEventListener("click", () => deleteChartById(spec.id));
    actions.appendChild(minBtn);
    actions.appendChild(editBtn);
    actions.appendChild(closeBtn);
    header.appendChild(titleEl);
    header.appendChild(actions);

    const canvasWrap = document.createElement("div");
    canvasWrap.className = "chart-overlay-canvas-wrap";
    const canvas = document.createElement("canvas");
    canvasWrap.appendChild(canvas);

    overlay.appendChild(header);
    overlay.appendChild(canvasWrap);
    layer.appendChild(overlay);

    positionChartOverlay(overlay, spec);
    chartOverlayEls.set(spec.id, overlay);

    const data = buildChartData(spec);
    if (!data) {
        titleEl.textContent = `${spec.title || "(untitled)"} — invalid range`;
        return;
    }

    const isPie = spec.chart_type === "pie";
    const chartConfig = {
        type: spec.chart_type,
        data: isPie
            ? {
                  labels: data.labels,
                  datasets: data.datasets.length
                      ? [{
                            label: data.datasets[0].label,
                            data: data.datasets[0].data,
                            backgroundColor: data.labels.map((_, i) => CHART_PALETTE[i % CHART_PALETTE.length]),
                        }]
                      : [],
              }
            : data,
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: !isPie ? data.datasets.length > 1 : true, position: isPie ? "right" : "top" },
                title: { display: false },
            },
            scales: isPie ? {} : {
                y: { beginAtZero: true },
            },
        },
    };

    try {
        const inst = new Chart(canvas.getContext("2d"), chartConfig);
        chartInstances.set(spec.id, inst);
    } catch (e) {
        titleEl.textContent = `${spec.title || "(untitled)"} — render error`;
        console.error("Chart render error", e);
    }
}

function renderCharts() {
    const layer = document.getElementById("chart-layer");
    if (!layer) return;
    const keepIds = new Set(sheetCharts.map(c => c.id));
    Array.from(chartInstances.keys()).forEach(id => {
        if (!keepIds.has(id)) destroyChartInstance(id);
    });
    pruneMinimizedIds();
    sheetCharts.forEach(spec => renderSingleChart(spec));
}

function revealChart(spec) {
    if (!spec) return;
    if (minimizedChartIds.has(spec.id)) setChartMinimized(spec.id, false);
    const anchor = cellEls.get(spec.anchor_cell);
    const wrap = document.getElementById("sheet-wrap");
    if (anchor && wrap) {
        const wrapRect = wrap.getBoundingClientRect();
        const anchorRect = anchor.getBoundingClientRect();
        const targetLeft = wrap.scrollLeft + (anchorRect.left - wrapRect.left) - 40;
        const targetTop = wrap.scrollTop + (anchorRect.top - wrapRect.top) - 40;
        wrap.scrollTo({ left: Math.max(0, targetLeft), top: Math.max(0, targetTop), behavior: "smooth" });
        setSelection(spec.anchor_cell, spec.anchor_cell);
    }
    const overlay = chartOverlayEls.get(spec.id);
    if (overlay) {
        overlay.classList.remove("flash-highlight");
        void overlay.offsetWidth;
        overlay.classList.add("flash-highlight");
        setTimeout(() => overlay.classList.remove("flash-highlight"), 1200);
    }
}

function refreshChartsList() {
    const list = document.getElementById("charts-list");
    const subtitle = document.getElementById("charts-panel-subtitle");
    if (!list) return;
    list.innerHTML = "";
    if (!sheetCharts.length) {
        if (subtitle) subtitle.textContent = "No charts on this sheet yet.";
        return;
    }
    if (subtitle) subtitle.textContent = `${sheetCharts.length} chart${sheetCharts.length === 1 ? "" : "s"} on this sheet.`;
    sheetCharts.forEach(spec => {
        const li = document.createElement("li");
        li.className = "charts-list-item";
        const name = document.createElement("div");
        name.innerHTML = `<strong>${escapeHtml(spec.title || "(untitled)")}</strong>`;
        name.style.cursor = "pointer";
        name.title = "Jump to chart";
        name.addEventListener("click", () => revealChart(spec));
        const meta = document.createElement("div");
        meta.className = "meta";
        meta.textContent = `${spec.chart_type} · ${spec.data_range} · anchor ${spec.anchor_cell}`;
        const actions = document.createElement("div");
        actions.className = "actions";
        const jumpBtn = document.createElement("button");
        jumpBtn.type = "button";
        jumpBtn.textContent = "Jump";
        jumpBtn.addEventListener("click", () => revealChart(spec));
        const editBtn = document.createElement("button");
        editBtn.type = "button";
        editBtn.textContent = "Edit";
        editBtn.addEventListener("click", () => openChartModal(spec.id));
        const delBtn = document.createElement("button");
        delBtn.type = "button";
        delBtn.textContent = "Delete";
        delBtn.className = "danger";
        delBtn.addEventListener("click", () => deleteChartById(spec.id));
        actions.appendChild(jumpBtn);
        actions.appendChild(editBtn);
        actions.appendChild(delBtn);
        li.appendChild(name);
        li.appendChild(meta);
        li.appendChild(actions);
        list.appendChild(li);
    });
}

function toggleChartsPanel(force) {
    const panel = document.getElementById("charts-panel");
    if (!panel) return;
    const want = force !== undefined ? force : panel.hasAttribute("hidden");
    if (want) panel.removeAttribute("hidden");
    else panel.setAttribute("hidden", "");
}

function openChartModal(existingId) {
    editingChartId = existingId || null;
    const backdrop = document.getElementById("chart-modal-backdrop");
    const titleHeader = document.getElementById("chart-modal-title");
    const submitBtn = document.getElementById("chart-form-submit");
    const existing = existingId ? sheetCharts.find(c => c.id === existingId) : null;
    const defaults = existing || {
        title: "",
        data_range: selectionLabel().includes(":") ? selectionLabel() : `${selectedRange.start}:${selectedRange.end}`,
        chart_type: "bar",
        orientation: "columns",
        anchor_cell: "F2",
        width: 400,
        height: 280,
    };
    document.getElementById("chart-form-title").value = defaults.title || "";
    document.getElementById("chart-form-range").value = defaults.data_range || "";
    document.getElementById("chart-form-type").value = defaults.chart_type || "bar";
    document.getElementById("chart-form-orientation").value = defaults.orientation || "columns";
    document.getElementById("chart-form-anchor").value = defaults.anchor_cell || "F2";
    document.getElementById("chart-form-width").value = defaults.width || 400;
    document.getElementById("chart-form-height").value = defaults.height || 280;
    if (titleHeader) titleHeader.textContent = existingId ? "Edit chart" : "New chart";
    if (submitBtn) submitBtn.textContent = existingId ? "Save changes" : "Create chart";
    backdrop.removeAttribute("hidden");
    document.getElementById("chart-form-range").focus();
}

function closeChartModal() {
    editingChartId = null;
    document.getElementById("chart-modal-backdrop").setAttribute("hidden", "");
}

async function submitChartForm(event) {
    event.preventDefault();
    const payload = {
        title: document.getElementById("chart-form-title").value.trim(),
        data_range: document.getElementById("chart-form-range").value.trim().toUpperCase(),
        chart_type: document.getElementById("chart-form-type").value,
        orientation: document.getElementById("chart-form-orientation").value,
        anchor_cell: document.getElementById("chart-form-anchor").value.trim().toUpperCase() || "F2",
        width: Number(document.getElementById("chart-form-width").value) || 400,
        height: Number(document.getElementById("chart-form-height").value) || 280,
        sheet: workbook.active_sheet,
    };
    if (!parseA1Range(payload.data_range)) {
        addLog("system", escapeHtml(`Invalid data range: ${payload.data_range}`));
        return;
    }
    try {
        if (editingChartId) {
            const res = await fetch(`${API_BASE}/system/charts/${editingChartId}`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            if (!res.ok) throw new Error((await res.json()).detail || "Update failed");
            addLog("system", `Chart updated: <strong>${escapeHtml(payload.title || payload.data_range)}</strong>.`);
        } else {
            const res = await fetch(`${API_BASE}/system/charts`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            if (!res.ok) throw new Error((await res.json()).detail || "Create failed");
            addLog("system", `Chart created: <strong>${escapeHtml(payload.title || payload.data_range)}</strong>.`);
        }
        closeChartModal();
        await fetchGrid();
        toggleChartsPanel(true);
    } catch (error) {
        addLog("system", escapeHtml(`Chart save failed: ${error.message}`));
    }
}

async function deleteChartById(id) {
    try {
        const res = await fetch(`${API_BASE}/system/charts/${id}?sheet=${encodeURIComponent(workbook.active_sheet)}`, {
            method: "DELETE",
        });
        if (!res.ok) throw new Error((await res.json()).detail || "Delete failed");
        addLog("system", "Chart deleted.");
        destroyChartInstance(id);
        await fetchGrid();
    } catch (error) {
        addLog("system", escapeHtml(`Chart delete failed: ${error.message}`));
    }
}

// ======== Bootstrap ========

async function bootstrap() {
    // Auth gate must run first so every subsequent fetch carries the Bearer
    // token. No-op in OSS mode.
    await bootstrapAuth();
    renderGridShell();
    attachGridEvents();
    attachGlobalEvents();
    await fetchWorkbook({ rehydrateChat: true });
    await fetchGrid();
    setScope("selection");
    toggleAssistant(true);
    refreshUndoRedoButtons();
    await refreshModelCatalog();
    attachSettingsEvents();

    document.querySelectorAll("[data-prompt]").forEach((button) => {
        button.addEventListener("click", () => {
            const input = document.getElementById("assistant-input");
            input.value = button.dataset.prompt;
            autoGrowInput();
            syncSendButtonState();
            toggleAssistant(true);
            if (button.dataset.chain === "true") setChainMode(true);
            input.focus();
        });
    });

    // Hero prompt handoff from landing page: pick up a prompt stashed in
    // sessionStorage and (optionally) auto-submit it so the user lands on a
    // workbook that's already being built.
    const initialPrompt = sessionStorage.getItem("gridos.initialPrompt");
    const initialAutosubmit = sessionStorage.getItem("gridos.initialAutosubmit") === "1";
    sessionStorage.removeItem("gridos.initialPrompt");
    sessionStorage.removeItem("gridos.initialAutosubmit");
    if (initialPrompt) {
        const input = document.getElementById("assistant-input");
        input.value = initialPrompt;
        autoGrowInput();
        syncSendButtonState();
        toggleAssistant(true);
        setScope("sheet");
        if (initialAutosubmit) {
            requestPreview();
        } else {
            input.focus();
        }
    }

    document.getElementById("formula-input").addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            event.preventDefault();
            saveFormulaBar();
        }
    });
    document.getElementById("clear-sheet-btn").addEventListener("click", clearActiveSheet);
    document.getElementById("assistant-toggle").addEventListener("click", () => toggleAssistant());
    document.getElementById("assistant-close").addEventListener("click", () => toggleAssistant(false));
    document.getElementById("chat-clear")?.addEventListener("click", () => {
        clearPreview();
        clearChatConversation();
    });
    const sendBtn = document.getElementById("send-btn");
    if (sendBtn) sendBtn.addEventListener("click", requestPreview);
    const composerInput = document.getElementById("assistant-input");
    composerInput.addEventListener("input", () => {
        autoGrowInput();
        syncSendButtonState();
    });
    composerInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            if (!event.repeat) requestPreview();
        }
    });
    document.getElementById("save-btn").addEventListener("click", saveWorkbook);
    document.getElementById("load-btn").addEventListener("click", loadWorkbook);
    document.getElementById("undo-btn").addEventListener("click", undo);
    document.getElementById("redo-btn").addEventListener("click", redo);

    const titleInput = document.getElementById("workbook-title-input");
    if (titleInput) {
        titleInput.addEventListener("blur", () => commitWorkbookName(titleInput.value));
        titleInput.addEventListener("keydown", (event) => {
            if (event.key === "Enter") {
                event.preventDefault();
                titleInput.blur();
            } else if (event.key === "Escape") {
                event.preventDefault();
                syncWorkbookTitleInput();
                titleInput.blur();
            }
        });
    }

    document.querySelectorAll(".chip[data-scope]").forEach((button) => {
        button.addEventListener("click", () => setScope(button.dataset.scope));
    });
    const chainToggle = document.getElementById("chain-mode-toggle");
    if (chainToggle) {
        chainToggle.addEventListener("change", (event) => setChainMode(event.target.checked));
    }
    setChainMode(false);
    autoGrowInput();
    syncSendButtonState();

    document.querySelectorAll("#ctx-menu li[data-action]").forEach((item) => {
        item.addEventListener("click", () => handleCtxAction(item.dataset.action));
    });

    document.querySelectorAll(".menubar-menu-btn[data-menu]").forEach((btn) => {
        btn.addEventListener("click", (event) => {
            event.stopPropagation();
            toggleMenu(btn.dataset.menu, btn);
        });
        btn.addEventListener("mouseenter", () => {
            const anyOpen = document.querySelector(".menu-dropdown.open");
            if (anyOpen) toggleMenu(btn.dataset.menu, btn);
        });
    });

    document.querySelectorAll(".menu-dropdown li[data-menu-action]").forEach((item) => {
        item.addEventListener("click", (event) => {
            event.stopPropagation();
            handleMenuAction(item.dataset.menuAction);
        });
    });

    // Charts panel + modal wiring
    document.getElementById("charts-panel-close")?.addEventListener("click", () => toggleChartsPanel(false));
    document.getElementById("charts-panel-add")?.addEventListener("click", () => openChartModal());
    document.getElementById("chart-modal-close")?.addEventListener("click", closeChartModal);
    document.getElementById("chart-form-cancel")?.addEventListener("click", closeChartModal);
    document.getElementById("chart-form")?.addEventListener("submit", submitChartForm);
    document.getElementById("chart-modal-backdrop")?.addEventListener("click", (event) => {
        if (event.target.id === "chart-modal-backdrop") closeChartModal();
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && !document.getElementById("chart-modal-backdrop").hasAttribute("hidden")) {
            closeChartModal();
        }
    });

    // Library modal wiring
    document.getElementById("library-modal-close")?.addEventListener("click", closeLibraryModal);
    document.getElementById("library-modal-backdrop")?.addEventListener("click", (event) => {
        if (event.target.id === "library-modal-backdrop") closeLibraryModal();
    });
    document.querySelectorAll("[data-library-tab]").forEach((btn) => {
        btn.addEventListener("click", () => switchLibraryTab(btn.dataset.libraryTab));
    });
    document.getElementById("template-save-btn")?.addEventListener("click", saveCurrentAsTemplate);
    document.getElementById("macro-form-submit")?.addEventListener("click", saveMacro);
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && !document.getElementById("library-modal-backdrop")?.hasAttribute("hidden")) {
            closeLibraryModal();
        }
    });

    // Reposition chart overlays whenever column/row sizes change.
    window.addEventListener("resize", () => sheetCharts.forEach(spec => {
        const el = chartOverlayEls.get(spec.id);
        if (el) positionChartOverlay(el, spec);
    }));
}

bootstrap();
