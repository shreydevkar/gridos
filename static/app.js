const API_BASE = "http://127.0.0.1:8000";
const COLUMN_COUNT = 60;
const ROW_COUNT = 300;
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
let colWidths = {};
let rowHeights = {};
let pendingHistory = [];
let undoStack = [];
let redoStack = [];
let clipboardMatrix = null;

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

async function fetchWorkbook() {
    const res = await fetch(`${API_BASE}/workbook`);
    workbook = await res.json();
    const activePill = document.getElementById("active-sheet-pill");
    if (activePill) activePill.textContent = workbook.active_sheet;
    renderTabs();
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
    colEls.forEach((nodes, label) => {
        const width = colWidths[label] || DEFAULT_COL_WIDTH;
        nodes.forEach((node) => {
            node.style.width = `${width}px`;
            node.style.minWidth = `${width}px`;
        });
    });
    rowEls.forEach((nodes, row) => {
        const height = rowHeights[row] || DEFAULT_ROW_HEIGHT;
        nodes.forEach((node) => {
            node.style.height = `${height}px`;
            node.style.minHeight = `${height}px`;
        });
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
    let html = `<tr><th class="corner"></th>`;
    for (let col = 0; col < COLUMN_COUNT; col++) {
        const label = colLabel(col);
        html += `<th class="col-header" data-col="${label}"><div class="header-inner">${label}<div class="resize-handle-col" data-resize-col="${label}"></div></div></th>`;
    }
    html += `</tr>`;

    for (let row = 1; row <= ROW_COUNT; row++) {
        html += `<tr><th class="row-header" data-row="${row}"><div class="header-inner">${row}<div class="resize-handle-row" data-resize-row="${row}"></div></div></th>`;
        for (let col = 0; col < COLUMN_COUNT; col++) {
            const label = colLabel(col);
            const a1 = `${label}${row}`;
            html += `<td data-cell="${a1}" data-col="${label}" data-row="${row}"><div class="cell-content"></div></td>`;
        }
        html += `</tr>`;
    }
    table.innerHTML = html;

    table.querySelectorAll("[data-col]").forEach((node) => {
        const label = node.dataset.col;
        if (!colEls.has(label)) colEls.set(label, []);
        colEls.get(label).push(node);
    });
    table.querySelectorAll("[data-row]").forEach((node) => {
        const row = node.dataset.row;
        if (!rowEls.has(row)) rowEls.set(row, []);
        rowEls.get(row).push(node);
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
    document.getElementById("selection-summary").textContent = `Selection: ${selectionLabel()}${scopeMode === "selection" ? " · Assistant will use the selected cells." : " · Assistant will use the entire sheet."}`;
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
    selectedRange = { start, end };
    syncSelectionUI();
    repaintSelection();
}

function addLog(kind, html) {
    const log = document.getElementById("assistant-log");
    log.innerHTML += `<div class="msg ${kind}">${html}</div>`;
    log.scrollTop = log.scrollHeight;
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

function renderPreviewCard() {
    const card = document.getElementById("preview-card");
    if (!previewState) {
        card.style.display = "none";
        card.innerHTML = "";
        return;
    }
    const previewRange = previewState.preview_cells.length
        ? `${previewState.preview_cells[0].cell} -> ${previewState.preview_cells[previewState.preview_cells.length - 1].cell}`
        : previewState.target_cell;
    card.style.display = "block";
    card.innerHTML = `
        <h4>${escapeHtml((previewState.category || "agent").toUpperCase())} preview</h4>
        <p>${escapeHtml(previewState.reasoning || "Preview ready.")}</p>
        <p>Scope: <strong>${previewState.scope === "selection" ? "Selected cells" : "Entire sheet"}</strong> | Target: <strong>${escapeHtml(previewRange)}</strong></p>
        <div class="assistant-actions" style="margin-top:10px;">
            <button class="primary-btn" id="apply-preview-btn">Apply Preview</button>
            <button class="ghost-btn" id="dismiss-preview-btn">Dismiss</button>
        </div>
    `;
    document.getElementById("apply-preview-btn").addEventListener("click", applyPreview);
    document.getElementById("dismiss-preview-btn").addEventListener("click", clearPreview);
}

function clearPreview() {
    previewState = null;
    renderPreviewCard();
    repaintPreview();
}

async function requestPreview() {
    const prompt = document.getElementById("assistant-input").value.trim();
    if (!prompt) return;
    addLog("user", escapeHtml(prompt));

    const payload = {
        prompt,
        history: pendingHistory.slice(-6),
        scope: scopeMode,
        selected_cells: scopeMode === "selection" ? getSelectedCells() : [],
        sheet: workbook.active_sheet,
    };

    if (chainMode) {
        await runChain(prompt, payload);
        return;
    }

    setStatus("Previewing");
    try {
        const res = await fetch(`${API_BASE}/agent/chat`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Preview failed.");
        previewState = data;
        pendingHistory.push({ role: "user", content: prompt });
        pendingHistory.push({ role: "assistant", content: `${data.category}: ${data.reasoning}` });
        renderPreviewCard();
        repaintPreview();
        addLog("agent", `Preview ready for <strong>${escapeHtml(data.target_cell || "target")}</strong>. Nothing has been written yet.`);
        setStatus("Awaiting approval");
    } catch (error) {
        addLog("system", escapeHtml(`Preview failed: ${error.message}`));
        setStatus("Recover");
    }
}

async function runChain(prompt, payload) {
    clearPreview();
    setStatus("Chaining (this can take several seconds)");
    addLog("system", "Chain mode engaged. Each step auto-applies and is observed.");
    const before = snapshotGrid();

    try {
        const res = await fetch(`${API_BASE}/agent/chat/chain`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
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
        addLog("system", escapeHtml(`Chain failed: ${error.message}`));
        setStatus("Recover");
    }
}

function renderChainSteps(data) {
    const steps = data.steps || [];
    if (!steps.length) {
        addLog("system", "Chain returned no steps.");
        return;
    }

    steps.forEach((step) => {
        if (step.completion_signal) {
            addLog("chain-complete", `
                <strong>Step ${step.iteration + 1} &middot; complete</strong>
                <div>${escapeHtml(step.reasoning || "Agent signaled the task is finished.")}</div>
            `);
            return;
        }

        const valuesJson = JSON.stringify(step.values);
        const obsItems = (step.observations || []).map((obs) => {
            const formula = obs.formula ? ` <em>(formula: ${escapeHtml(obs.formula)})</em>` : "";
            return `<li>${escapeHtml(obs.cell)} = ${escapeHtml(String(obs.value))}${formula}</li>`;
        }).join("");

        addLog("chain-step", `
            <strong>Step ${step.iteration + 1} &middot; ${escapeHtml(step.agent_id)}</strong>
            <div>${escapeHtml(step.reasoning || "")}</div>
            <div style="margin-top:6px;">Target: <strong>${escapeHtml(step.target)}</strong> &middot; Wrote: <code>${escapeHtml(valuesJson)}</code></div>
            ${obsItems ? `<ul>${obsItems}</ul>` : ""}
        `);
    });

    if (data.terminated_early) {
        addLog("system", `Chain terminated early after ${data.iterations_used} iteration(s).`);
    }
}

async function applyPreview() {
    if (!previewState) return;
    const before = snapshotGrid();
    try {
        setStatus("Applying");
        await fetch(`${API_BASE}/agent/apply`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                sheet: workbook.active_sheet,
                agent_id: previewState.agent_id,
                target_cell: previewState.original_request || previewState.target_cell,
                values: previewState.values,
                shift_direction: "right",
                chart_spec: previewState.chart_spec || null,
            }),
        });
        addLog("agent", `Applied preview into <strong>${escapeHtml(previewState.target_cell)}</strong>.${previewState.chart_spec ? " Chart added." : ""}`);
        if (previewState.target_cell) selectedRange = { start: previewState.target_cell, end: previewState.target_cell };
        clearPreview();
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
    document.querySelectorAll(".scope-btn").forEach((button) => {
        button.classList.toggle("active", button.dataset.scope === mode);
    });
    syncSelectionUI();
}

function setChainMode(enabled) {
    chainMode = Boolean(enabled);
    const toggle = document.getElementById("chain-mode-toggle");
    const toggleLabel = toggle?.closest(".mode-toggle");
    const previewBtn = document.getElementById("preview-btn");
    if (toggle) toggle.checked = chainMode;
    if (toggleLabel) toggleLabel.classList.toggle("active", chainMode);
    if (previewBtn) previewBtn.textContent = chainMode ? "Run Chain" : "Preview Change";
    if (chainMode && previewState) clearPreview();
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
        await fetchWorkbook();
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
    renderGridShell();
    attachGridEvents();
    attachGlobalEvents();
    await fetchWorkbook();
    await fetchGrid();
    setScope("selection");
    toggleAssistant(true);
    refreshUndoRedoButtons();

    document.querySelectorAll("[data-prompt]").forEach((button) => {
        button.addEventListener("click", () => {
            document.getElementById("assistant-input").value = button.dataset.prompt;
            toggleAssistant(true);
            document.getElementById("assistant-input").focus();
        });
    });

    document.getElementById("formula-input").addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            event.preventDefault();
            saveFormulaBar();
        }
    });
    document.getElementById("clear-sheet-btn").addEventListener("click", clearActiveSheet);
    document.getElementById("assistant-toggle").addEventListener("click", () => toggleAssistant());
    document.getElementById("assistant-close").addEventListener("click", () => toggleAssistant(false));
    document.getElementById("preview-btn").addEventListener("click", requestPreview);
    document.getElementById("cancel-preview-btn").addEventListener("click", clearPreview);
    document.getElementById("assistant-input").addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            requestPreview();
        }
    });
    document.getElementById("save-btn").addEventListener("click", saveWorkbook);
    document.getElementById("load-btn").addEventListener("click", loadWorkbook);
    document.getElementById("undo-btn").addEventListener("click", undo);
    document.getElementById("redo-btn").addEventListener("click", redo);

    document.querySelectorAll(".scope-btn").forEach((button) => {
        button.addEventListener("click", () => setScope(button.dataset.scope));
    });
    const chainToggle = document.getElementById("chain-mode-toggle");
    if (chainToggle) {
        chainToggle.addEventListener("change", (event) => setChainMode(event.target.checked));
    }
    setChainMode(false);

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
    // Reposition chart overlays whenever column/row sizes change.
    window.addEventListener("resize", () => sheetCharts.forEach(spec => {
        const el = chartOverlayEls.get(spec.id);
        if (el) positionChartOverlay(el, spec);
    }));
}

bootstrap();
