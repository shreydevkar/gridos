const API_BASE = "http://127.0.0.1:8000";
const COLUMN_COUNT = 60;
const ROW_COUNT = 300;
const DEFAULT_COL_WIDTH = 124;
const DEFAULT_ROW_HEIGHT = 34;

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

const cellEls = new Map();
const colEls = new Map();
const rowEls = new Map();
let populatedCells = new Set();
let paintedSelection = new Set();
let paintedPreview = new Set();
let activeCellId = null;

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
    document.getElementById("active-sheet-pill").textContent = workbook.active_sheet;
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
    document.getElementById("active-sheet-pill").textContent = workbook.active_sheet;
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
    document.getElementById("active-sheet-pill").textContent = workbook.active_sheet;
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
    refreshPopulatedCells();
    syncSelectionUI();
    repaintSelection();
    repaintPreview();
}

function syncSelectionUI() {
    const anchor = selectedRange.end;
    document.getElementById("name-box").textContent = anchor;
    document.getElementById("selection-pill").textContent = selectionLabel();
    document.getElementById("selection-summary").textContent = `Selection: ${selectionLabel()}${scopeMode === "selection" ? " | Assistant will use the selected cells." : " | Assistant will use the entire sheet."}`;
    document.getElementById("assistant-subtitle").textContent = scopeMode === "selection" ? "Focused on the selected cells." : "Focused on the active sheet.";
    document.getElementById("scope-pill").textContent = scopeMode === "selection" ? "Selected Cells" : "Entire Sheet";
    document.getElementById("formula-input").value = getCellDisplay(gridData[anchor]);
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
    try {
        setStatus("Saving");
        await persistSingleCell(selectedRange.end, document.getElementById("formula-input").value);
        await fetchGrid();
        setStatus("Ready");
    } catch (error) {
        addLog("system", escapeHtml(`Save failed: ${error.message}`));
        setStatus("Recover");
    }
}

function startInlineEdit(cell) {
    const state = gridData[cell] || {};
    if (state.locked) {
        addLog("system", `${cell} is locked and cannot be edited.`);
        return;
    }

    editingCell = cell;
    const td = cellEls.get(cell);
    td.innerHTML = `<input class="editing-input" id="inline-editor" value="${escapeHtml(getCellDisplay(state))}" />`;
    const input = document.getElementById("inline-editor");
    input.focus();
    input.select();
    input.addEventListener("keydown", async (event) => {
        if (event.key === "Enter") {
            event.preventDefault();
            await commitInlineEdit(cell, input.value);
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
    try {
        await persistSingleCell(cell, value);
        const td = cellEls.get(cell);
        td.innerHTML = `<div class="cell-content"></div>`;
        await fetchGrid();
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
    try {
        await persistRange(selectedRange.end, matrix);
        await fetchGrid();
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

    try {
        await persistRange(coordsToA1(rowStart, colStart), matrix);
        selectedRange = { start: coordsToA1(rowStart, colStart), end: coordsToA1(rowEnd, colEnd) };
        await fetchGrid();
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
            }),
        });
        addLog("agent", `Applied preview into <strong>${escapeHtml(previewState.target_cell)}</strong>.`);
        if (previewState.target_cell) selectedRange = { start: previewState.target_cell, end: previewState.target_cell };
        clearPreview();
        await fetchGrid();
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
        }
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "v" && !editingText) {
            const text = await navigator.clipboard.readText().catch(() => "");
            if (text) {
                event.preventDefault();
                await pasteSelection(text);
            }
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
}

async function clearActiveSheet() {
    const approved = window.confirm("Clear every unlocked cell in this tab?");
    if (!approved) return;
    await fetch(`${API_BASE}/system/clear?sheet=${encodeURIComponent(workbook.active_sheet)}`, { method: "POST" });
    clearPreview();
    await fetchGrid();
}

async function bootstrap() {
    renderGridShell();
    attachGridEvents();
    attachGlobalEvents();
    await fetchWorkbook();
    await fetchGrid();
    setScope("selection");
    toggleAssistant(true);

    document.querySelectorAll("[data-prompt]").forEach((button) => {
        button.addEventListener("click", () => {
            document.getElementById("assistant-input").value = button.dataset.prompt;
            toggleAssistant(true);
            document.getElementById("assistant-input").focus();
        });
    });

    document.getElementById("save-cell-btn").addEventListener("click", saveFormulaBar);
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
    document.querySelectorAll(".scope-btn").forEach((button) => {
        button.addEventListener("click", () => setScope(button.dataset.scope));
    });
    const chainToggle = document.getElementById("chain-mode-toggle");
    if (chainToggle) {
        chainToggle.addEventListener("change", (event) => setChainMode(event.target.checked));
    }
    setChainMode(false);
}

bootstrap();
