# anywhere-agents

**Your AI agents, configured once and running everywhere.**

A maintained, opinionated configuration for Claude Code and Codex that follows you across every project, every machine, every session.

> Maintained by [Yue Zhao](https://yzhao062.github.io) — Assistant Professor of Computer Science at USC (Viterbi) and author of [PyOD](https://github.com/yzhao062/pyod) (9.8k stars, 38M+ downloads). His open-source ML libraries have ~20k combined GitHub stars and his research has ~12k citations on Google Scholar. This repository is the sanitized public release of the working agent config behind that portfolio — the shared bootstrap, guard, and review/router pieces tested and iterated over months across many repositories, machines (macOS, Windows, Linux), and workflows. Not a weekend project.

## The problem

You use AI coding agents across many repositories. You have preferences — how reviews should happen, what writing style to use, which Git operations must require confirmation, which overused words the agent should never emit. Today those preferences live in one of these broken states:

- Scattered across per-repo `CLAUDE.md` / `AGENTS.md` files that drift over time
- Copy-pasted from project to project, diverging on every tweak
- Only in your head, re-explained to every agent in every session

`anywhere-agents` fixes this by publishing one curated, maintained agent stack that any project can consume in two lines of setup. When the maintainer improves something, every consuming repo picks it up on the next session.

## What you get after setup (5 minutes)

Every agent session in any repo pointed at `anywhere-agents` inherits:

- **Safety that is loud but not noisy.** `guard.py` intercepts destructive commands (`git push`, `git reset --hard`, `gh pr merge`, `rm -rf`, `gh pr close`) with impossible-to-auto-dismiss warnings like `STOP! HAMMER TIME! A wild git push appeared!`. Read-only operations (`git status`, `git diff`, `git log`, `ls`, `cat`) stay silent and fast. Tuned from daily use: it catches real destructive commands without triggering on every other keystroke. That balance is the product.
- **Automatic skill dispatch — no memorizing.** The `my-router` skill reads your prompt, the files you are working on, and the directory structure, then picks the right skill automatically. Say "review this" with staged changes present → it runs `implement-review` with the correct lens. You never type a skill name.
- **Structured dual-agent code review.** Claude Code implements, Codex reviews. Triggered automatically by the router when you have staged changes → a structured review loop with content-type-specific lenses (code via Google eng-practices, paper via NeurIPS / ICLR criteria, proposal via NSF / NIH Simplified Peer Review), round history tracking, and a reviewer save contract. No equivalent exists as a packaged protocol elsewhere.
- **Consistent writing style.** 40+ AI-tell words auto-avoided (`delve`, `pivotal`, `underscore`, `harmonize`, `bolster`, `groundbreaking`, ...). No em-dashes as casual punctuation. Full forms over contractions. Preserves original format when input is LaTeX / Markdown / reStructuredText. Your agent stops writing like a chatbot.
- **Git safety that does not nag.** `git commit` and `git push` always require explicit confirmation. Read-only `git status` / `git diff` / `git log` stay fast. The exact allow and ask lists are tuned from daily use across many repos.
- **Shell hygiene.** Claude Code's hidden compound-command protection trips on `cd <path> && <cmd>` chains even when both commands are individually allowed. The config teaches the agent to use `git -C <path>` and path arguments instead — fewer approval prompts, less friction.
- **Session start checks.** Agents automatically report OS, model and effort level, Codex config validity, and outdated GitHub Actions version pins at the start of every session. Catches misconfigurations before they waste your time.

No CLI to install, no YAML to configure. Git handles subscription and updates. Override anything per project in `AGENTS.local.md` and your override never gets touched by sync.

## Quickstart

Pick one path.

### Path A — Consume directly (get my updates automatically)

You want the maintainer's setup and ongoing refinements. You do not plan to diverge much.

Add this block to the top of any project's `AGENTS.md`:

````markdown
## Shared Agent Config (auto-fetched)

PowerShell (Windows):

```powershell
New-Item -ItemType Directory -Force -Path .agent-config, .claude, .claude/commands | Out-Null
Invoke-WebRequest -UseBasicParsing -Uri https://raw.githubusercontent.com/yzhao062/anywhere-agents/main/bootstrap/bootstrap.ps1 -OutFile .agent-config/bootstrap.ps1
& .\.agent-config\bootstrap.ps1
```

Bash (macOS/Linux):

```bash
mkdir -p .agent-config .claude/commands
curl -sfL https://raw.githubusercontent.com/yzhao062/anywhere-agents/main/bootstrap/bootstrap.sh -o .agent-config/bootstrap.sh
bash .agent-config/bootstrap.sh
```
````

Next time Claude Code or Codex starts a session in that project, it refreshes `AGENTS.md`, skills, pointer commands, and settings from upstream. Customization goes in `AGENTS.local.md` — never overwritten by sync.

**Trust tradeoff:** direct consume applies upstream changes on every session, including bootstrap script changes. Use this path only if you trust the maintainer and want automatic updates. Use Path B if you want to review changes before they land.

### Path B — Fork and customize (make it yours)

You want to diverge — change the writing defaults, add your own skills, tune the safety rules, use a different reviewer. Standard Git workflow:

```bash
# 1. Fork yzhao062/anywhere-agents to your account
# 2. Clone your fork and customize
#    - edit AGENTS.md (user profile, writing style, agent roles)
#    - add skills to skills/<your-skill>/
#    - update skills/my-router/references/routing-table.md to register new skills
# 3. Point your project repos at your fork by changing the URL in the bootstrap block
# 4. Pull my updates when you want them:
git remote add upstream https://github.com/yzhao062/anywhere-agents.git
git fetch upstream
git merge upstream/main   # resolve conflicts as usual
```

No special tooling. Git is the subscription engine. Cherry-pick what you want, skip what you do not.

## Using it day-to-day

**Adding to a new project:**
Copy the bootstrap block (from Path A above) into the top of the new project's `AGENTS.md`. That is the entire setup. Next session, the agent runs bootstrap and everything lands.

**Getting the latest on an existing project:**
Bootstrap runs at the start of every agent session automatically. Just start a new session in any consuming project and upstream changes (new skill, updated writing defaults, changed safety rules) land immediately. No manual refresh step.

<details>
<summary><b>Force a refresh mid-session</b> (e.g., maintainer pushed a fix you need right now)</summary>

```bash
# Bash (macOS/Linux)
bash .agent-config/bootstrap.sh

# PowerShell (Windows)
& .\.agent-config\bootstrap.ps1
```

Both scripts are idempotent — safe to run any time. They fetch the latest `AGENTS.md`, sync skills, merge settings, and add `.agent-config/` to `.gitignore` if it is not already there.

</details>

<details>
<summary><b>Customize one specific project without touching upstream</b></summary>

Create `AGENTS.local.md` in the project root. Anything in it overrides the shared defaults and is never overwritten by bootstrap. Useful for project-specific permissions, domain glossaries, or opt-outs.

</details>

<details>
<summary><b>Repo layout</b> (what lives where)</summary>

```
anywhere-agents/
├── AGENTS.md                    # the opinionated configuration (curated defaults)
├── bootstrap/
│   ├── bootstrap.sh             # idempotent sync for macOS/Linux
│   └── bootstrap.ps1            # idempotent sync for Windows
├── scripts/
│   └── guard.py                 # PreToolUse hook: blocks destructive commands with loud warnings
├── skills/
│   ├── implement-review/        # structured dual-agent review loop (signature skill)
│   └── my-router/               # context-aware skill dispatcher (template — extend with your own)
├── .claude/
│   ├── commands/                # pointer files so Claude Code discovers the skills
│   └── settings.json            # project-level permissions
├── user/
│   └── settings.json            # user-level permissions, hook wiring, CLAUDE_CODE_EFFORT_LEVEL=max
├── tests/                       # bootstrap contract + smoke tests (Ubuntu + Windows CI)
└── .github/workflows/           # validation CI
```

</details>

## What is opinionated and why

Review these before adoption — they are the product, not background defaults:

- **Safety-first by default.** `git commit` / `git push` always require confirmation. Guard hook has no bypass mode.
- **Dual-agent review as the default review loop.** Claude Code is the implementer; Codex is the reviewer. If you use only one agent, the review skill still works but the second-opinion value disappears.
- **Writing style is strongly opinionated.** 40+ banned words, no em-dashes as casual punctuation, no bullet-conversion of prose, no summary sentence ritual at the end of every paragraph. Sound like you, not like a chatbot.
- **Session-start checks report, not fix.** Agents will flag outdated Actions versions, wrong Codex config, model preferences — not silently change anything without telling you.

Disagree with any of this? Path B, and fork.

<details>
<summary><b>What this is not</b></summary>

- Not a framework or CLI tool. No install step beyond the shell bootstrap. No YAML manifest.
- Not a universal multi-agent sync tool. Claude Code + Codex is the supported set. Other agents (Cursor, Aider, Gemini CLI) may work via the `AGENTS.md` convention but are not tested here.
- Not a marketplace or registry. One curated configuration, one maintainer.

</details>

<details>
<summary><b>Related projects</b> (if <code>anywhere-agents</code> is not the right fit)</summary>

If you want a general-purpose multi-agent sync tool or a broader skill catalog, these take different approaches:

- [iannuttall/dotagents](https://github.com/iannuttall/dotagents) — central location for hooks, commands, skills, AGENTS/CLAUDE.md files
- [microsoft/agentrc](https://github.com/microsoft/agentrc) — repo-ready-for-AI tooling
- [agentfiles on PyPI](https://pypi.org/project/agentfiles/) — CLI that syncs configurations across multiple agents

`anywhere-agents` is intentionally narrower: a published, maintained, opinionated configuration — not a tool that manages configurations. Fork it if you like the setup; use one of the tools above if you want a universal manager.

</details>

<details>
<summary><b>Maintenance and support</b></summary>

- **Maintained:** the author's daily-use workflow. Changes land when the author needs them.
- **Not maintained:** feature requests that do not match the author's work. Users should fork.
- **Best-effort:** bug reports, PRs for clear fixes, documentation improvements.

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to propose changes.

</details>

<details>
<summary><b>Limitations and caveats</b></summary>

- Primary support is Claude Code + Codex. Cursor, Aider, Gemini CLI may work via `AGENTS.md` but are untested here.
- Requires `git` everywhere. Requires Python (stdlib only) for settings merge; bootstrap continues without merge if Python is unavailable.
- Guard hook deploys to `~/.claude/hooks/guard.py` and modifies `~/.claude/settings.json`. To opt out of user-level modifications, remove the user-level section from `bootstrap/bootstrap.sh` / `bootstrap/bootstrap.ps1` in your fork.

</details>

## License

Apache 2.0. See [LICENSE](LICENSE).
