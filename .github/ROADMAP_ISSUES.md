# Roadmap issues — one-shot setup

This file is the source-of-truth for the public roadmap, ready to bulk-create as GitHub Issues so the repo's Issues tab reflects what's actually being built. Run `scripts/seed_github_issues.sh` once to create everything (needs `gh` CLI + `gh auth login`).

## Labels used

| Label | Color | Purpose |
| :--- | :--- | :--- |
| `roadmap` | `#0e8a16` | On the public roadmap |
| `enhancement` | `#a2eeef` | New capability |
| `bug` | `#d73a4a` | Something's broken |
| `good first issue` | `#7057ff` | Low-friction start for new contributors |
| `help wanted` | `#008672` | Would welcome PRs |
| `docs` | `#1d76db` | Documentation-only |
| `plugin` | `#fbca04` | Plugin system / connectors |
| `saas` | `#5319e7` | Only relevant in SaaS mode |
| `security` | `#b60205` | Security-sensitive area |

## Issues to create

### 1. Cross-sheet formula references (`=Sheet2!A1`)
**Labels:** `roadmap`, `enhancement`, `help wanted`

The formula parser currently doesn't recognize `!` as a token and `_resolve_cell_ref` only looks up in the active sheet. Adding cross-sheet refs is table-stakes for any spreadsheet and would be a big trust signal for new visitors.

**Acceptance criteria:**
- [ ] Tokenizer recognizes `SheetName!A1` as a qualified cell reference.
- [ ] Tokenizer handles quoted sheet names with spaces: `'Monthly Budget'!A1`.
- [ ] `_resolve_cell_ref` + `_resolve_range_values` take an optional `sheet_name` and route to `self.sheets[<name>]` when set.
- [ ] Dependency tracker keys cross-sheet dependencies so a recalc fires the right sheet's formulas.
- [ ] New tests in `test_ast_edge_cases.py` for qualified references (single cell, range, missing sheet → `#REF!`, quoted names with spaces).

---

### 2. Viewer role enforcement on write endpoints
**Labels:** `roadmap`, `saas`, `security`

The share endpoint currently rejects `role='viewer'` because enforcement isn't wired. The infrastructure (`_current_role` ContextVar + `_require_editor()` helper) exists; it just needs plumbing into every mutation endpoint.

**Acceptance criteria:**
- [ ] Every write endpoint (`/grid/cell`, `/grid/range`, `/grid/clear`, `/agent/apply`, `/agent/chat/chain`, `/workbook/sheet*`, `/system/save`, chart/macro CRUD) calls `_require_editor()` at the top.
- [ ] Unit test via `TestClient` that a viewer caller gets 403 on each.
- [ ] Share modal adds a role picker (editor / viewer).
- [ ] Invite endpoint starts accepting `role='viewer'` again.

---

### 3. Invite unregistered users by email via share link
**Labels:** `roadmap`, `saas`, `enhancement`

Currently the invite lands in `pending_invites` and auto-promotes on signup, but the invitee has no way to *discover* that they've been invited — they have to be told out-of-band. Wire email delivery via Supabase's SMTP so the invitee gets a "Shrey invited you to Income Statement" email with the sign-up link.

**Acceptance criteria:**
- [ ] On `add_collaborator_by_email` with `kind='pending'`, queue a transactional email through Supabase Auth or a separate SMTP provider.
- [ ] Email includes the inviter's email and the workbook title for context.
- [ ] Configurable via `INVITE_EMAIL_ENABLED` env flag so it's off by default in self-host.

---

### 4. Realtime broadcast for sheet structure (rename, add, delete)
**Labels:** `roadmap`, `saas`, `enhancement`

`cells_changed` + `cursor_at` sync live, but adding/renaming/deleting a sheet doesn't broadcast. Collaborators have to refresh to see the new tab.

**Acceptance criteria:**
- [ ] New broadcast event `sheet_changed` emitted on create / rename / delete / activate.
- [ ] Client handler updates `workbook.sheets` + tab bar.
- [ ] Active-sheet changes from a collaborator don't force-switch the peer's active sheet (just tell them a sheet changed).

---

### 5. Optimistic-locking client integration
**Labels:** `roadmap`, `saas`, `enhancement`

The server supports `expected_versions` + 409 Conflict on `/grid/cell` and `/grid/range`. The frontend doesn't send it or handle it yet. Wire a "someone else edited that cell — reload?" toast when a local write races a remote one.

**Acceptance criteria:**
- [ ] `persistSingleCell` / `persistRange` send `expected_versions` from the last known grid snapshot.
- [ ] 409 response renders a non-modal toast with Reload / Dismiss actions.
- [ ] Reload refetches the grid without clobbering in-progress edits on *other* cells.

---

### 6. Per-collaborator connector credentials override
**Labels:** `roadmap`, `saas`, `plugin`

Collaborators currently inherit the owner's connector keys (Shopify / Stripe / GitHub). That's right for most cases but sometimes you want each user's formula to hit their own account (e.g. "collaborate on a dashboard template but show MY Stripe numbers"). Let each collaborator optionally override the owner's keys with their own.

**Acceptance criteria:**
- [ ] `user_plugin_secrets` lookup tries the caller first, falls back to the owner if no override.
- [ ] Configure modal in the marketplace shows which slots are "yours" vs "inherited from owner" when the workbook is shared.

---

### 7. Workbook deletion broadcasts
**Labels:** `roadmap`, `saas`, `bug`

If the owner deletes a workbook while a collaborator has it open, the collaborator hits 404s on every autosave. Should broadcast `workbook_deleted` and redirect the peer to the landing page with a toast.

**Acceptance criteria:**
- [ ] `DELETE /workbooks/{id}` fires a `workbook_deleted` broadcast.
- [ ] Client redirects to `/` with a toast.

---

### 8. Embedding-based agent router
**Labels:** `roadmap`, `enhancement`

The current router is a classifier prompt against a small Groq model. Works fine for ~4 agents; won't scale past ~10. Replace with an embedding nearest-neighbor match across agent `router_description` vectors.

**Acceptance criteria:**
- [ ] One-time embedding generation on agent registration.
- [ ] Per-request embedding of the user prompt + cosine match against agent vectors.
- [ ] Fallback to the current classifier prompt if the embedding provider isn't configured.

---

### 9. Prompt caching on Gemini + Anthropic
**Labels:** `roadmap`, `enhancement`

Long chain-mode turns re-send the full sheet context every time. Enable prompt caching on providers that support it (Anthropic beta, Gemini `CachedContent`) to cut latency and cost on the 2nd-plus turn.

**Acceptance criteria:**
- [ ] Provider abstraction gets a `cache_id` / `cache_name` field.
- [ ] System-prompt + context turn gets cached on the first call.
- [ ] Follow-up calls reference the cached prefix.

---

### 10. `README.md`: add demo GIF for shared-workbook + realtime
**Labels:** `good first issue`, `docs`

The hero GIF shows the single-user experience. Add a second GIF (or split-screen) showing two browser windows editing the same workbook with live cursors — that's the most-undersold capability of the current build.

**Acceptance criteria:**
- [ ] GIF of two GridOS tabs editing the same workbook, with cursors visible on each side.
- [ ] README references it in the "Capabilities" section under Shared workbooks.
