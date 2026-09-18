# Composed-consumer fixture

Fixed copies of the three passive pack bodies the maintainer's consumers compose into `AGENTS.md`, used by `tests/test_composed_consumer_size.py` to hold a composed consumer under 61,440 bytes without a network fetch. Refresh a copy when its pack body changes on purpose, and record the new size and hash here.

| Pack | Source | Path | Bytes | SHA-256 |
|---|---|---|---|---|
| `agent-style` | github.com/yzhao062/agent-style at `v0.4.1` | `docs/rule-pack-compact.md` | 21,201 | `45bd5e852d4ff62ce8949ff437849c80e24106c0c55631739eb059b6e9e60c1d` |
| `profile` | github.com/yzhao062/agent-pack (compact bodies, 2026-09-17) | `docs/rule-pack-compact.md` | 4,686 | `dfafb6208f2e44ab85a0c79382f2d8b4b6d47be256f32238d103ec48610bfee7` |
| `paper-workflow` | github.com/yzhao062/agent-pack (compact bodies, 2026-09-17) | `docs/paper-workflow-compact.md` | 5,630 | `f4fcef7341bc90e6ba88c947c4961c5548a8cd7481b5881de449837d5160d6f3` |

The composition order is the order consumers use: the bundled `agent-style` first, then the two `agent-pack` packs from `agent-config.yaml`.
