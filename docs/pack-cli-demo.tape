# pack-cli-demo.tape — anywhere-agents v0.5.0 pack CLI demo
#
# Render:
#   bash docs/_render_gif.sh
# Output: docs/pack-cli-demo.gif (embed in README "Pack Management CLI" section).
#
# Narrative (5 commands, ~14s runtime):
#   1. pack list                              → fresh state, nothing yet
#   2. pack add <ap-URL> --ref v0.1.0          → fetches manifest, expands to 3 rows
#   3. pack list                              → profile, paper-workflow, acad-skills
#   4. pack remove profile                    → removes one row
#   5. pack list                              → 2 rows remain
#
# v0.5.0 reality: pack add reads the remote pack.yaml and writes one row
# per declared pack. agent-pack declares 3 (profile, paper-workflow,
# acad-skills) so a single add expands to three rows in user-level config.
# pack update / pack list --drift exist (CLI v0.5.0) but are not exercised
# here to keep the demo tight.
#
# Honesty: the demo only shows the CLI writing user-level config. It does
# NOT imply bootstrap consumes that file in v0.5.0. README prose around the
# embed says "user-level rows are forward-compat scaffolding; bootstrap
# selection assembly is staged for v0.5.x".

Output docs/pack-cli-demo.gif

Set Theme "Catppuccin Mocha"
Set FontSize 18
Set FontFamily "JetBrains Mono"
Set Width 1200
Set Height 700
Set PlaybackSpeed 1.0
Set Padding 30
Set Shell bash

# macOS-style window chrome (traffic lights).
# Note: WindowBarTitle is a vhs 0.12+ feature; on 0.11 the bar stays blank.
Set WindowBar Colorful
Set WindowBarSize 50

# Setup runs inside the vhs Docker container; the render script already
# placed the wrapper at /usr/local/bin/anywhere-agents.

Hide
# /tmp/init.sh does: set HOME, source PS1, clear (wipes the `source` line),
# then print banner. End state when Show kicks in: banner + blank prompt.
Type "source /tmp/init.sh"
Enter
Sleep 300ms
Show

# ── 1. List in fresh state ────────────────────────────────────────────────
Type "anywhere-agents pack list"
Sleep 300ms
Enter
Sleep 1500ms

# ── 2. Add agent-pack pinned to v0.1.0 — expands to 3 rows ────────────────
# v0.5.0 direct-URL fetch: pack add reads the remote pack.yaml and writes
# one user-level row per declared pack (profile, paper-workflow, acad-skills).
Type "anywhere-agents pack add https://github.com/yzhao062/agent-pack --ref v0.1.0"
Sleep 300ms
Enter
Sleep 3500ms

# ── 3. List → profile, paper-workflow, acad-skills ────────────────────────
Type "anywhere-agents pack list"
Sleep 300ms
Enter
Sleep 2000ms

# ── 4. Remove one of the rows ─────────────────────────────────────────────
Type "anywhere-agents pack remove profile"
Sleep 300ms
Enter
Sleep 1500ms

# ── 5. List → 2 rows remain ───────────────────────────────────────────────
Type "anywhere-agents pack list"
Sleep 300ms
Enter
Sleep 2000ms
