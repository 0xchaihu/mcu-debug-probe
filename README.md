# mcu-debug-probe

Use pyOCD-driven SWD/JTAG probe control from terminal agents such as Claude Code or Codex to inspect Cortex-M targets, flash images, reset MCUs, read registers and RAM, and perform basic live-debug operations without opening a full IDE.

## Use In Claude Code

Claude Code supports skills natively through the standard `SKILL.md` entrypoint. The Claude-side install locations are:

- Personal: `~/.claude/skills/mcu-debug-probe/SKILL.md`
- Project: `.claude/skills/mcu-debug-probe/SKILL.md`

That means this skill does not need a separate Claude-only equivalent of `agents/openai.yaml`. The closest Claude-specific controls live in `SKILL.md` frontmatter, such as `disable-model-invocation`, `allowed-tools`, `context`, `agent`, `paths`, and `shell`.

If you share this skill with Claude Code users, tell them to copy or symlink the whole `mcu-debug-probe/` folder into one of those Claude skill directories so `SKILL.md`, `scripts/`, and `references/` stay together.

## What This Skill Requires

### Required

- `python`
- `pyocd`
- `PyYAML`

Install the minimum Python dependencies with:

```sh
python -m pip install pyocd pyyaml
```

Use the active Python environment or whatever `python` resolves to on the host. Do not hardcode a machine-specific interpreter or virtualenv path into shared commands or docs.

### Usually Installed Alongside pyOCD

These are normally pulled in as pyOCD dependencies, but they still need to work correctly on the host:

- `hidapi`
- `pyusb`
- `libusb-package`
- `pylink-square`

### Platform Notes

- The command examples in this README use forward slashes so they work in PowerShell, bash, and zsh.
- Run them from the parent directory that contains the `mcu-debug-probe/` folder, or replace the path with an absolute path to `scripts/pyocd_probe.py`.

### Windows / Probe-Specific Notes

- `CMSIS-DAPv1` usually works through HID with fewer host-side requirements.
- `CMSIS-DAPv2` needs a working `libusb` runtime on the host, even though many probes are otherwise driverless on modern Windows.
- `J-Link` support also depends on a working J-Link installation on the machine.
- Some CMSIS-DAP probes only expose a v1 HID interface in firmware; in that case pyOCD cannot force them into v2.

### macOS / Linux Notes

- On macOS and Linux, prefer a Python environment where `pyocd`, `pyusb`, and `PyYAML` are installed together.
- `CMSIS-DAPv2` also depends on a usable `libusb` runtime on these hosts.
- On Linux, probe access may require udev rules or running inside a session that already has permission to access the USB device.
- J-Link support still depends on a working Segger J-Link installation on the host.

## Hardware Preconditions

- The target board is powered.
- SWD/JTAG wiring is correct.
- If multiple probes are connected, the user chooses the exact probe with `--uid`.

## Recommended First Check

```sh
python mcu-debug-probe/scripts/pyocd_probe.py probe
python mcu-debug-probe/scripts/pyocd_probe.py probe-capabilities
```

This tells you:

- whether pyOCD can see the probes at all
- whether a probe is being used as CMSIS-DAPv1, CMSIS-DAPv2, J-Link, etc.
- whether you need to supply a manual `--uid`

## Target Name Resolution

This skill uses pyOCD target names internally. Your project may use a different chip model string, such as:

- `LPC55S69JBD100`
- `KE15-Z7`
- `MIMXRT1062DVJ6A`

To bridge that gap, the skill supports `pyocd-targets.yaml`.

### Preferred Workflow

Place a `pyocd-targets.yaml` file in the project directory, or pass one explicitly with `--target-config`.

Example:

```yaml
vendor: NXP
chip_name: LPC55S69JBD100

aliases:
  LPC55S69JBD100: lpc55s69
  KE15-Z7: ke15z7

patterns:
  LPC55S69*: lpc55s69
  MIMXRT1062*: mimxrt1060
```

### If No YAML Exists Yet

Target-aware commands can generate `pyocd-targets.yaml` automatically when you already know the local chip model and pass it with `--chip-name`.

If you do not know the chip model yet, ask for it first instead of guessing.

For example:

```sh
python mcu-debug-probe/scripts/pyocd_probe.py resolve-target --chip-name LPC55S69JBD100
```

or

```sh
python mcu-debug-probe/scripts/pyocd_probe.py status --uid <probe-id> --chip-name LPC55S69JBD100
```

If the helper cannot uniquely infer the pyOCD target from the chip model you provide, it will ask for a more explicit pyOCD target name and save that mapping.

If a resolved target is missing from the local pyOCD catalog, the helper now attempts to install the matching CMSIS-Pack automatically. It first tries direct device names, then family-style fuzzy search for inputs such as `MCX A36x`, `MCXA366x`, or shorthand family names like `a345x`. If the fuzzy search points to one pack, it installs it automatically and prefers the matching family target when pyOCD exposes one; if multiple packs match, it reports candidate packs with concrete retry suggestions such as `MCXA365` or `MCXA365VLL`. If pyOCD still cannot resolve the device afterward, it reports that support was not found locally or in the remote pack index.

## Command Reference

| Goal | Command | Notes |
| --- | --- | --- |
| Enumerate probes | `python mcu-debug-probe/scripts/pyocd_probe.py probe` | Safe, read-only first step. |
| Inspect probe capabilities | `python mcu-debug-probe/scripts/pyocd_probe.py probe-capabilities` | Reports whether each connected probe is seen as CMSIS-DAPv1, CMSIS-DAPv2, J-Link, etc. |
| Resolve a local chip name | `python mcu-debug-probe/scripts/pyocd_probe.py resolve-target --target-config pyocd-targets.yaml` | Resolves local board naming to a pyOCD target without touching hardware. If the YAML file is missing and you already know the chip model, pass `--chip-name <model>` to generate it automatically. |
| Attach and summarize state | `python mcu-debug-probe/scripts/pyocd_probe.py status --uid <probe-id>` | Captures target state and common registers. Add `--target-config pyocd-targets.yaml` when local chip naming needs translation. |
| Halt or resume | `python mcu-debug-probe/scripts/pyocd_probe.py halt --uid <probe-id>` | Use `resume` to continue execution. |
| Read registers | `python mcu-debug-probe/scripts/pyocd_probe.py regs --uid <probe-id> --halt-on-connect --registers pc sp lr xpsr` | Register names are passed through to pyOCD. |
| Read memory | `python mcu-debug-probe/scripts/pyocd_probe.py mem-read --uid <probe-id> --address 0x20000000 --width 32 --count 8` | Width is `8`, `16`, or `32`. |
| Read stack snapshot | `python mcu-debug-probe/scripts/pyocd_probe.py stack --uid <probe-id> --halt-on-connect --words 8` | Starts from `SP` unless `--address` is set. |
| Decode exception frame | `python mcu-debug-probe/scripts/pyocd_probe.py exception-frame --uid <probe-id> --halt-on-connect` | Decodes the standard Cortex-M exception frame from `SP` or `--address`. |
| Read fault registers | `python mcu-debug-probe/scripts/pyocd_probe.py fault --uid <probe-id> --halt-on-connect` | Decodes CFSR/HFSR/DFSR and related SCB registers. |
| Read vector table | `python mcu-debug-probe/scripts/pyocd_probe.py vector-table --uid <probe-id> --count 16` | Labels core exception vectors and IRQ entries from `VTOR` or `--base-address`. |
| Single-step | `python mcu-debug-probe/scripts/pyocd_probe.py step --uid <probe-id> --halt-on-connect --count 1` | Intrusive: advances execution while staying halted, including after disconnect. |
| Set breakpoint | `python mcu-debug-probe/scripts/pyocd_probe.py breakpoint-set --uid <probe-id> --halt-on-connect --address 0x08001234` | Use `breakpoint-clear` to remove it. |
| Flash image | `python mcu-debug-probe/scripts/pyocd_probe.py flash --uid <probe-id> --image build/app.elf --yes` | Mutating: confirm first. |
| Erase flash | `python mcu-debug-probe/scripts/pyocd_probe.py erase --uid <probe-id> --chip --yes` | Mutating: `--mass` and sector-address forms are also supported. |
| Reset target | `python mcu-debug-probe/scripts/pyocd_probe.py reset --uid <probe-id> --halt-after-reset --yes` | Mutating: confirm first. With `--halt-after-reset`, the target remains halted after disconnect. |
| Write memory | `python mcu-debug-probe/scripts/pyocd_probe.py mem-write --uid <probe-id> --address 0x20000000 --width 32 --values 0x1 0x2 --yes` | Mutating: confirm first. |
| Open persistent debug session | `python mcu-debug-probe/scripts/pyocd_probe.py debug-open --uid <probe-id> --halt-on-connect` | Use this for real debug workflows that need breakpoints and repeated inspection without disconnecting. |
| Halt in persistent session | `python mcu-debug-probe/scripts/pyocd_probe.py debug-halt --session-id <id>` | Keeps the same pyOCD session alive. |
| Set breakpoint in persistent session | `python mcu-debug-probe/scripts/pyocd_probe.py debug-breakpoint-set --session-id <id> --address 0x08001234` | Breakpoints persist until `debug-close` or `debug-breakpoint-clear`. |
| Read registers in persistent session | `python mcu-debug-probe/scripts/pyocd_probe.py debug-regs --session-id <id> --registers pc sp lr xpsr` | Reuses the same attached session. |
| Read memory in persistent session | `python mcu-debug-probe/scripts/pyocd_probe.py debug-mem-read --session-id <id> --address 0x20000000 --width 32 --count 8` | Reuses the same attached session. |
| Reset in persistent session | `python mcu-debug-probe/scripts/pyocd_probe.py debug-reset --session-id <id> --halt-after-reset --yes` | Mutating: keeps the same live session and can leave the target halted. |
| Write memory in persistent session | `python mcu-debug-probe/scripts/pyocd_probe.py debug-mem-write --session-id <id> --address 0x20000000 --width 32 --values 0x1 --yes` | Mutating: writes RAM/peripheral memory without tearing down the debug session. |
| Close persistent debug session | `python mcu-debug-probe/scripts/pyocd_probe.py debug-close --session-id <id>` | Removes transient debugger state such as breakpoints by closing the pyOCD session. |

`.out` images are treated as ELF during `flash`, so GCC-style `firmware.out` outputs work like `.elf` or `.axf`.
`halt`, `step`, and `reset --halt-after-reset` now keep the target halted even after the pyOCD session disconnects.

## Persistent Debug Sessions

One-shot commands are still useful for probe discovery, flash, erase, or a single inspection. For real debugging flows such as:

- attach
- halt
- set breakpoint
- inspect registers
- inspect memory

use the persistent `debug-*` commands so pyOCD keeps one live session open.

Example:

```sh
python mcu-debug-probe/scripts/pyocd_probe.py debug-open --uid <probe-id> --halt-on-connect
python mcu-debug-probe/scripts/pyocd_probe.py debug-breakpoint-set --session-id <id> --address 0x08001234
python mcu-debug-probe/scripts/pyocd_probe.py debug-regs --session-id <id> --registers pc sp lr xpsr
python mcu-debug-probe/scripts/pyocd_probe.py debug-mem-read --session-id <id> --address 0x20000000 --width 32 --count 8
python mcu-debug-probe/scripts/pyocd_probe.py debug-close --session-id <id>
```

Notes:

- `debug-open` returns a `session_id`.
- Breakpoints and halt state now persist across `debug-*` commands because the helper keeps the same pyOCD session alive.
- Sessions auto-expire after idle timeout unless you close them first with `debug-close`.
- Session state is stored under the system temp directory, so no machine-specific virtualenv path is baked into the skill.

Fuller debug flow example:

```sh
python mcu-debug-probe/scripts/pyocd_probe.py debug-open --uid <probe-id> --halt-on-connect
python mcu-debug-probe/scripts/pyocd_probe.py debug-reset --session-id <id> --halt-after-reset --yes
python mcu-debug-probe/scripts/pyocd_probe.py debug-breakpoint-set --session-id <id> --address 0x08001234
python mcu-debug-probe/scripts/pyocd_probe.py debug-resume --session-id <id>
python mcu-debug-probe/scripts/pyocd_probe.py debug-halt --session-id <id>
python mcu-debug-probe/scripts/pyocd_probe.py debug-regs --session-id <id> --registers pc sp lr xpsr
python mcu-debug-probe/scripts/pyocd_probe.py debug-mem-read --session-id <id> --address 0x20000000 --width 32 --count 8
python mcu-debug-probe/scripts/pyocd_probe.py debug-mem-write --session-id <id> --address 0x20000000 --width 32 --values 0x1 --yes
python mcu-debug-probe/scripts/pyocd_probe.py debug-close --session-id <id>
```

Use `debug-reset` and `debug-mem-write` when you want those mutating actions to happen inside the same live pyOCD session rather than forcing a disconnect/reconnect cycle.
