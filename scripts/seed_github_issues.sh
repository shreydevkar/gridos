#!/usr/bin/env bash
# Bulk-create the public roadmap as GitHub Issues + their labels.
#
# One-time setup to light up the repo's Issues tab with real items so
# visitors see the project has active work going on, rather than 0 issues
# and a README roadmap that rots.
#
# Prereqs:
#   1. `gh` installed (`winget install GitHub.cli` on Windows, `brew install gh` on mac).
#   2. `gh auth login` run once.
#   3. cwd is this repo.
#
# Idempotent: re-running creates labels with `--force`, and skips issues whose
# title already exists.

set -euo pipefail

REPO="$(gh repo view --json nameWithOwner --jq '.nameWithOwner' 2>/dev/null || echo 'shreydevkar/gridos')"
echo "Targeting $REPO"

# ---------- Labels ----------
echo "== Creating labels =="
gh label create roadmap              --color 0e8a16 --description "On the public roadmap"                               --force
gh label create "good first issue"   --color 7057ff --description "Low-friction start for new contributors"             --force
gh label create "help wanted"        --color 008672 --description "Maintainer would welcome a PR"                       --force
gh label create plugin               --color fbca04 --description "Plugin system or a connector plugin"                 --force
gh label create saas                 --color 5319e7 --description "Only relevant in SaaS mode"                          --force
gh label create security             --color b60205 --description "Security-sensitive surface"                          --force
# `bug`, `enhancement`, and `docs` already exist on every GH repo; the --force
# upsert above keeps the colors consistent without breaking existing assignments.
gh label create bug                  --color d73a4a --description "Something's broken"                                  --force || true
gh label create enhancement          --color a2eeef --description "New capability"                                      --force || true
gh label create docs                 --color 1d76db --description "Documentation-only"                                  --force || true

existing_titles() {
    gh issue list --state all --limit 200 --json title --jq '.[].title'
}

# ---------- Issues ----------
# Each block: title + label(s) + body (HEREDOC). Wrapped in a "skip if exists"
# guard so re-running never creates duplicates.
create_issue() {
    local title="$1"
    local labels="$2"
    local body="$3"
    if existing_titles | grep -Fxq "$title"; then
        echo "  SKIP '$title' (already exists)"
        return 0
    fi
    echo "  + $title"
    gh issue create --title "$title" --label "$labels" --body "$body" >/dev/null
}

echo "== Creating issues =="

create_issue "Cross-sheet formula references (=Sheet2!A1)" "roadmap,enhancement,help wanted" "$(cat <<'EOF'
The formula parser currently doesn't recognize `!` as a token and `_resolve_cell_ref` only looks up in the active sheet. Adding cross-sheet refs is table-stakes for any spreadsheet and would be a big trust signal for new visitors.

**Acceptance criteria:**
- [ ] Tokenizer recognizes `SheetName!A1` as a qualified cell reference.
- [ ] Tokenizer handles quoted sheet names with spaces: `'Monthly Budget'!A1`.
- [ ] `_resolve_cell_ref` + `_resolve_range_values` take an optional `sheet_name` and route to `self.sheets[<name>]` when set.
- [ ] Dependency tracker keys cross-sheet dependencies so a recalc fires the right sheet's formulas.
- [ ] New tests in `test_ast_edge_cases.py` for qualified references (single cell, range, missing sheet → `#REF!`, quoted names with spaces).
EOF
)"

create_issue "Viewer role enforcement on write endpoints" "roadmap,saas,security" "$(cat <<'EOF'
The share endpoint currently rejects `role='viewer'` because enforcement isn't wired. The infrastructure (`_current_role` ContextVar + `_require_editor()` helper) exists; it just needs plumbing into every mutation endpoint.

**Acceptance criteria:**
- [ ] Every write endpoint (`/grid/cell`, `/grid/range`, `/grid/clear`, `/agent/apply`, `/agent/chat/chain`, `/workbook/sheet*`, `/system/save`, chart/macro CRUD) calls `_require_editor()` at the top.
- [ ] Unit test via `TestClient` that a viewer caller gets 403 on each.
- [ ] Share modal adds a role picker (editor / viewer).
- [ ] Invite endpoint starts accepting `role='viewer'` again.
EOF
)"

create_issue "Invite unregistered users by email via share link" "roadmap,saas,enhancement" "$(cat <<'EOF'
Currently the invite lands in `pending_invites` and auto-promotes on signup, but the invitee has no way to *discover* they've been invited — they have to be told out-of-band. Wire email delivery via Supabase's SMTP so the invitee gets an invitation email with the sign-up link.

**Acceptance criteria:**
- [ ] On `add_collaborator_by_email` with `kind='pending'`, queue a transactional email through Supabase Auth or a separate SMTP provider.
- [ ] Email includes the inviter's email and the workbook title for context.
- [ ] Configurable via `INVITE_EMAIL_ENABLED` env flag so it's off by default in self-host.
EOF
)"

create_issue "Realtime broadcast for sheet structure (rename, add, delete)" "roadmap,saas,enhancement" "$(cat <<'EOF'
`cells_changed` + `cursor_at` sync live, but adding/renaming/deleting a sheet doesn't broadcast. Collaborators have to refresh to see the new tab.

**Acceptance criteria:**
- [ ] New broadcast event `sheet_changed` emitted on create / rename / delete / activate.
- [ ] Client handler updates `workbook.sheets` + tab bar.
- [ ] Active-sheet changes from a collaborator don't force-switch the peer's active sheet (just tell them a sheet changed).
EOF
)"

create_issue "Wire optimistic-locking 409 into the client" "roadmap,saas,enhancement" "$(cat <<'EOF'
The server supports `expected_versions` + 409 Conflict on `/grid/cell` and `/grid/range`. The frontend doesn't send it or handle it yet. Wire a "someone else edited that cell — reload?" toast when a local write races a remote one.

**Acceptance criteria:**
- [ ] `persistSingleCell` / `persistRange` send `expected_versions` from the last known grid snapshot.
- [ ] 409 response renders a non-modal toast with Reload / Dismiss actions.
- [ ] Reload refetches the grid without clobbering in-progress edits on *other* cells.
EOF
)"

create_issue "Per-collaborator connector credentials override" "roadmap,saas,plugin" "$(cat <<'EOF'
Collaborators currently inherit the owner's connector keys (Shopify / Stripe / GitHub). That's right for most cases but sometimes each user's formula should hit their own account. Let each collaborator optionally override the owner's keys with their own.

**Acceptance criteria:**
- [ ] `user_plugin_secrets` lookup tries the caller first, falls back to the owner if no override.
- [ ] Configure modal shows which slots are "yours" vs "inherited from owner" when the workbook is shared.
EOF
)"

create_issue "Workbook deletion broadcasts" "roadmap,saas,bug" "$(cat <<'EOF'
If the owner deletes a workbook while a collaborator has it open, the collaborator hits 404s on every autosave. Should broadcast `workbook_deleted` and redirect the peer to the landing page with a toast.

**Acceptance criteria:**
- [ ] `DELETE /workbooks/{id}` fires a `workbook_deleted` broadcast.
- [ ] Client redirects to `/` with a toast.
EOF
)"

create_issue "Embedding-based agent router" "roadmap,enhancement" "$(cat <<'EOF'
The current router is a classifier prompt against a small Groq model. Works fine for ~4 agents; won't scale past ~10. Replace with an embedding nearest-neighbor match across agent `router_description` vectors.

**Acceptance criteria:**
- [ ] One-time embedding generation on agent registration.
- [ ] Per-request embedding of the user prompt + cosine match against agent vectors.
- [ ] Fallback to the current classifier prompt if the embedding provider isn't configured.
EOF
)"

create_issue "Prompt caching on Gemini + Anthropic" "roadmap,enhancement" "$(cat <<'EOF'
Long chain-mode turns re-send the full sheet context every time. Enable prompt caching on providers that support it (Anthropic beta, Gemini CachedContent) to cut latency and cost on the 2nd-plus turn.

**Acceptance criteria:**
- [ ] Provider abstraction gets a `cache_id` / `cache_name` field.
- [ ] System-prompt + context turn gets cached on the first call.
- [ ] Follow-up calls reference the cached prefix.
EOF
)"

create_issue "Demo GIF for shared-workbook + realtime" "good first issue,docs" "$(cat <<'EOF'
The README hero GIF shows the single-user experience. Add a second GIF (or split-screen) showing two browser windows editing the same workbook with live cursors — that's the most undersold capability of the current build.

**Acceptance criteria:**
- [ ] GIF of two GridOS tabs editing the same workbook, with cursors visible on each side.
- [ ] README references it in the "Capabilities" section under Shared workbooks.
EOF
)"

echo ""
echo "Done. Check the Issues tab: https://github.com/$REPO/issues"
echo ""
echo "Next manual steps (one-time, in the GitHub UI):"
echo "  1. Settings → General → scroll to 'Features' → tick 'Discussions' to enable the Discussions tab."
echo "  2. Issues tab → pin the 3 most important issues (e.g. the roadmap overview, viewer enforcement, and a 'first-time contributor?' pointer)."
echo "  3. Insights → Community Standards → add a Code of Conduct from the template if you want the green checkmark."
