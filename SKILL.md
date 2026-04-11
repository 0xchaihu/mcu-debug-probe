---
name: mcu-debug-probe
description: Use when Claude Code, Codex, or another terminal coding agent needs to control an MCU debug probe or inspect a live ARM Cortex-M target over SWD or JTAG, especially for board bring-up, flashing firmware, resetting the chip, halting or resuming execution, reading core registers, or dumping RAM and peripheral memory during hardware debugging.
---

# MCU Debug Probe

## Overview

Use this skill to drive a connected debug probe from the terminal instead of an IDE.
Prefer the bundled `pyocd_probe.py` helper. It is `pyOCD`-first and intended for ARM Cortex-M targets.
Use it from Claude Code, Codex CLI, or a similar terminal agent workflow.
The same `SKILL.md` also works when this folder is installed under `~/.claude/skills/mcu-debug-probe/` or `.claude/skills/mcu-debug-probe/`; Claude Code does not need a separate `agents/openai.yaml`-style metadata file for skill discovery.

## Minimum Prerequisites

- The active `python` must have `pyocd` and `PyYAML` installed.
- Read `README.md` for host setup on Windows, macOS, and Linux, plus `libusb`, J-Link, and longer examples.

## Core Workflow

- Start with `probe`; if no probe appears, stop and fix tooling, power, permissions, or cabling first.
- If more than one probe appears, ask the user which probe to use; do not guess.
- Use `attach` or `status` before mutating hardware so you capture board name, target state, and key registers first.
- If the project uses a non-pyOCD chip name, resolve it through `pyocd-targets.yaml` or pass `--chip-name <model>` so the helper can create the YAML file. If the chip model is still unknown, ask the user instead of guessing.
- If the resolved target is missing locally, let the helper try CMSIS-Pack auto-install. It first tries direct names, then family-style fuzzy search such as `MCX A36x`, `MCXA366x`, or `a345x`. If one pack matches, it installs it; if multiple packs match, it reports retry suggestions like `MCXA365` or `MCXA365VLL`.
- Default to read-only inspection until the user explicitly wants a hardware-changing action.
- Prefer `debug-open` plus `debug-*` commands for multi-step debug flows that need a continuous session, persistent breakpoints, or a stable halted/running context.
- For `regs`, `stack`, `fault`, `step`, or breakpoint work on a running target, add `--halt-on-connect`.
- Treat `flash`, `erase`, `reset`, and `mem-write` as mutating operations. Treat `halt`, `step`, `breakpoint-set`, and `breakpoint-clear` as debug-intrusive even when they are not destructive.
- Before a mutating command, restate the probe selector, target, image path or address, and expected hardware impact; continue only after explicit confirmation.
- If a workflow needs preserved breakpoints or halted state, use `debug-reset` and `debug-mem-write` inside the same persistent session instead of one-shot commands.
- If the target is not clearly ARM Cortex-M compatible with `pyOCD`, or `pyocd` cannot open it, stop and explain the prerequisite or limitation instead of inventing fallback behavior.

## Primary Commands

Run commands against `scripts/pyocd_probe.py` using a path resolved from the skill directory.
Use forward slashes in examples so the same command form works in PowerShell, bash, and zsh.

```sh
python mcu-debug-probe/scripts/pyocd_probe.py probe
python mcu-debug-probe/scripts/pyocd_probe.py status --uid <probe>
python mcu-debug-probe/scripts/pyocd_probe.py resolve-target --target-config pyocd-targets.yaml
python mcu-debug-probe/scripts/pyocd_probe.py debug-open --uid <probe>
python mcu-debug-probe/scripts/pyocd_probe.py flash --uid <probe> --image build/app.elf --yes
```

Read `references/pyocd-cheatsheet.md` only when you need install help, example commands, or recovery guidance.

The helper prints a short summary first and a JSON object second. Use the JSON for chained reasoning or follow-up commands.

## Common Mistakes

- Skipping `probe` and jumping straight to flash; this hides cable, power, and driver issues.
- Resetting too early; capture `status`, `regs`, and memory first when debugging a fault.
- Reading registers from a running target without `--halt-on-connect`; some targets only expose meaningful core registers while halted.
- Auto-selecting one debugger when more than one is attached; always make the user choose and pin the session with `--uid`.
- Running mutating commands without repeating the image path or address; restate the exact target of the operation before executing it.
