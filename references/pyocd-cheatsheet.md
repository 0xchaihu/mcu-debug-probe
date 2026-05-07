# pyOCD Cheatsheet

## Minimum Checks

- Use a `python` that has `pyocd` and `PyYAML` installed.
- Keep commands interpreter-agnostic; do not hardcode a machine-specific venv path.
- Make sure the board is powered, the probe is connected, and no other tool already owns it.
- If local chip naming differs from pyOCD naming, use `pyocd-targets.yaml` or `--chip-name <model>`.

## First Commands

```sh
python mcu-debug-probe/scripts/pyocd_probe.py probe
python mcu-debug-probe/scripts/pyocd_probe.py probe-capabilities
python mcu-debug-probe/scripts/pyocd_probe.py status --uid <probe-id>
python mcu-debug-probe/scripts/pyocd_probe.py resolve-target --target-config pyocd-targets.yaml
```

- If multiple probes are listed, stop and re-run with the user-selected `--uid`.
- If the chip model is known but no YAML exists yet, re-run a target-aware command with `--chip-name <model>`.
- If the helper still cannot map the target, prefer an explicit YAML alias or `--target <pyocd-target>`.

## Common Recovery Moves

- `pyOCD is not installed`
  - Install it in the active Python environment, then re-run the same command.
- `No probe/target session could be created`
  - Check USB, power, permissions, and whether another debugger owns the probe.
  - Retry with `--uid`.
  - Retry with `--connect-mode under-reset` if the target locks up immediately after boot.
- `Multiple probes detected`
  - Show the candidates to the user and re-run with the exact `--uid`.
- `regs` looks empty or unstable
  - Retry with `--halt-on-connect`.
- Target name resolution is wrong or ambiguous
  - Use `pyocd-targets.yaml`, `--chip-name`, or a more explicit `--target`.
  - For fuzzy CMSIS-Pack matches, the helper uses pyOCD glob patterns such as `N236*`; direct partial names like `N236` can return no devices.
  - Prefer the helper's retry suggestions such as `MCXA365`, `MCXA365VLL`, or `MCXN236VDF`.
- `CMSIS-DAPv2` is unavailable
  - Run `probe-capabilities`.
  - Fix the host `libusb` runtime or accept CMSIS-DAPv1/HID if the probe firmware does not expose a v2 bulk interface.
- Flash/program command needs address context
  - For raw `.bin`, add `--base-address 0x...`.
  - For `.out`, `.elf`, or `.axf`, let the helper treat the file as ELF.

## High-Value Commands

```sh
python mcu-debug-probe/scripts/pyocd_probe.py regs --uid <probe-id> --halt-on-connect --registers pc sp lr xpsr
python mcu-debug-probe/scripts/pyocd_probe.py fault --uid <probe-id> --halt-on-connect
python mcu-debug-probe/scripts/pyocd_probe.py stack --uid <probe-id> --halt-on-connect --words 8
python mcu-debug-probe/scripts/pyocd_probe.py flash --uid <probe-id> --image build/app.elf --yes
python mcu-debug-probe/scripts/pyocd_probe.py debug-open --uid <probe-id> --halt-on-connect
```

- Use one-shot commands for quick inspection or flashing.
- Use `debug-open` plus `debug-*` commands when breakpoints or halted state must survive across steps.

## Output Contract

Every helper call prints:

1. A short human summary
2. One JSON line with the same result in structured form

Prefer the JSON line for follow-up automation or for carrying probe and target state across steps.
