# Security policy

## Supported versions

GridOS is pre-1.0 and ships from `master`. Fixes land on `master` and the hosted deploy (gridos.onrender.com) follows automatically.

## Reporting a vulnerability

If you believe you've found a security issue in GridOS — secret exposure, auth bypass, SQL injection, privilege escalation between collaborators, kernel RCE through a formula, etc. — **please do not open a public issue**.

Instead, email **marcocyl04@gmail.com** with:

- A description of the vulnerability.
- Steps to reproduce, or a proof-of-concept.
- Which version / commit SHA / hosted deploy you tested against.
- Whether you'd like to be credited publicly after the fix lands.

You'll get an acknowledgment within 72 hours. For confirmed, high-severity issues we aim to ship a fix within 7 days; lower-severity issues are triaged into the regular roadmap.

## Scope

**In scope:**
- The GridOS kernel, API server, plugin system, and the hosted SaaS deploy (gridos.onrender.com).
- The auto-generated Mintlify docs site at gridos.mintlify.app.
- Any Supabase schema or RLS policy in `cloud/migrations/`.

**Out of scope:**
- Vulnerabilities in upstream dependencies (report to the upstream maintainer; we'll ship a pinned update once the upstream has patched).
- Rate-limiting on the public deploy — Render free tier has limited throughput and that's expected.
- Issues that require a user to install an untrusted third-party plugin (the developer plugin portal is intentionally a full RCE surface when enabled).
- Third-party API provider issues (Shopify, Stripe, GitHub, etc.).

## Known sensitive surfaces

Listed so reviewers know where to look first:

- **Preview token stash** (`main.py` `_preview_stash_*`) — single-use TTL-bounded tokens gate agent writes.
- **ACL resolution** (`current_kernel_dep` + `_scope_from_context`) — the mistake of falling back to the caller's id on ACL failure previously caused silent ownership transfers; the current build raises 503 instead.
- **Plugin secrets** (`public.user_plugin_secrets`, `cloud/user_plugin_secrets.py`) — per-user BYOK. The GET endpoint returns only *which slots are set*, never values.
- **Developer plugin portal** (`/dev/plugins/*`) — full RCE when `GRIDOS_DEV_PORTAL_ENABLED=1`, refused unconditionally in SaaS.
- **Service-role key usage** — every cloud/ module that calls `_client()` uses the service-role key and bypasses RLS. RLS is defense-in-depth.
