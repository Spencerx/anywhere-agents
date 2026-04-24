# pack-cli-demo.tape — anywhere-agents v0.4.0 pack CLI demo
#
# Render:
#   bash docs/_render_gif.sh
# Output: docs/pack-cli-demo.gif (embed in README "Pack Management CLI" section).
#
# Narrative (5 commands, ~12s runtime):
#   1. pack list           → fresh state, nothing yet
#   2. pack add <pack>     → seeds user-level config + adds pack
#   3. pack list           → shows default agent-style + added pack
#   4. pack remove <pack>  → removes added pack
#   5. pack list           → back to default-only state
#
# Honesty: the demo only shows the CLI writing user-level config.
# It does NOT imply bootstrap consumes that file in v0.4.0.
# README prose around the embed says "CLI writes now; bootstrap reads in v0.4.x".

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

# ── 2. Add a pack, pinned to a version via --ref ──────────────────────────
# The v0.4.0 CLI stores '--ref' as a separate field; embedding a version in
# the pack name (aa-core-skills@0.4.0) would be stored literally as the name.
Type "anywhere-agents pack add aa-core-skills --ref v0.4.0"
Sleep 300ms
Enter
Sleep 1800ms

# ── 3. List again → default agent-style + added pack ──────────────────────
Type "anywhere-agents pack list"
Sleep 300ms
Enter
Sleep 2000ms

# ── 4. Remove the added pack ──────────────────────────────────────────────
Type "anywhere-agents pack remove aa-core-skills"
Sleep 300ms
Enter
Sleep 1500ms

# ── 5. List → back to default only ────────────────────────────────────────
Type "anywhere-agents pack list"
Sleep 300ms
Enter
Sleep 2000ms
