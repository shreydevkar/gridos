<!-- Thanks for opening a PR! Fill this out so the review goes fast. -->

## What changed

<!-- One or two sentences on what this PR does and why. -->


## Checklist

- [ ] `python test_platform.py` passes locally (14 cases, offline)
- [ ] `python test_ast_edge_cases.py` passes locally (21 cases, offline)
- [ ] `python test_plugins.py` passes locally
- [ ] If this adds a user-facing feature, `README.md` updated in the same PR
- [ ] If this adds / changes a plugin seam, `plugins/README.md` updated
- [ ] If this adds / changes an API endpoint, the corresponding Mintlify doc was noted (docs repo is separate: https://github.com/shreydevkar/docs)
- [ ] No secrets committed (check `.env`, `data/api_keys.json`)

## Breaking changes

<!-- None → delete this section. Otherwise list the schema, env var, or API
shape that changed, and what operators need to do before deploying. -->


## Testing notes

<!-- How to verify this works end-to-end — especially for SaaS features,
realtime behavior, or plugin changes that aren't fully covered by the
offline suites. -->

