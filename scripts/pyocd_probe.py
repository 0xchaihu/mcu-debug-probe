#!/usr/bin/env python3
"""Agent-friendly pyOCD wrapper for MCU probe operations."""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import string
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


DEFAULT_REGISTERS = ["pc", "sp", "lr", "xpsr", "msp", "psp"]
DEFAULT_TARGET_CONFIG_FILENAMES = [
    "pyocd-targets.yaml",
    "pyocd-targets.yml",
]
TARGET_CONFIG_NAME_KEYS = [
    "pyocd_target",
    "target_override",
    "target",
    "chip_name",
    "part_number",
    "device",
    "mcu",
    "soc",
    "chip",
    "model",
]
MUTATING_COMMANDS = {"flash", "reset", "mem-write", "erase"}
DEBUG_SESSION_PREFIX = "debug-"
DEBUG_SESSION_READY_TIMEOUT_SECONDS = 15.0
DEBUG_SESSION_REQUEST_TIMEOUT_SECONDS = 15.0
DEBUG_SESSION_IDLE_TIMEOUT_SECONDS = 900
DEBUG_SESSION_POLL_SECONDS = 0.05
PACK_INSTALL_TIMEOUT_SECONDS = 600
AUTO_PACK_INSTALL_ATTEMPTS: set[str] = set()
FAULT_REGISTERS = {
    "ICSR": 0xE000ED04,
    "SHCSR": 0xE000ED24,
    "CFSR": 0xE000ED28,
    "HFSR": 0xE000ED2C,
    "DFSR": 0xE000ED30,
    "MMFAR": 0xE000ED34,
    "BFAR": 0xE000ED38,
    "AFSR": 0xE000ED3C,
}
MMFSR_BITS = {
    0: "IACCVIOL",
    1: "DACCVIOL",
    3: "MUNSTKERR",
    4: "MSTKERR",
    5: "MLSPERR",
    7: "MMARVALID",
}
BFSR_BITS = {
    0: "IBUSERR",
    1: "PRECISERR",
    2: "IMPRECISERR",
    3: "UNSTKERR",
    4: "STKERR",
    5: "LSPERR",
    7: "BFARVALID",
}
UFSR_BITS = {
    0: "UNDEFINSTR",
    1: "INVSTATE",
    2: "INVPC",
    3: "NOCP",
    8: "UNALIGNED",
    9: "DIVBYZERO",
}
HFSR_BITS = {
    1: "VECTTBL",
    30: "FORCED",
    31: "DEBUGEVT",
}
DFSR_BITS = {
    0: "HALTED",
    1: "BKPT",
    2: "DWTTRAP",
    3: "VCATCH",
    4: "EXTERNAL",
}
EXCEPTION_VECTOR_NAMES = {
    0: "InitialSP",
    1: "Reset",
    2: "NMI",
    3: "HardFault",
    4: "MemManage",
    5: "BusFault",
    6: "UsageFault",
    7: "Reserved7",
    8: "Reserved8",
    9: "Reserved9",
    10: "Reserved10",
    11: "SVCall",
    12: "DebugMonitor",
    13: "Reserved13",
    14: "PendSV",
    15: "SysTick",
}


def build_payload(status: str, summary: str, **data: Any) -> dict[str, Any]:
    return {"status": status, "summary": summary, **data}


def emit_payload(payload: dict[str, Any]) -> int:
    print(payload.get("summary", ""))
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload.get("status") == "ok" else 1


def emit(status: str, summary: str, **data: Any) -> int:
    return emit_payload(build_payload(status, summary, **data))


def parse_int(value: str) -> int:
    return int(value, 0)


def normalize_target_name(value: str) -> str:
    legal_chars = string.ascii_letters + string.digits + "_"
    result = ""
    in_replace = False
    for char in value:
        if char in legal_chars:
            result += char.lower()
            in_replace = False
        elif not in_replace:
            result += "_"
            in_replace = True
    return result


def compact_target_name(value: str) -> str:
    return normalize_target_name(value).replace("_", "")


def compact_search_token(value: str, allow_glob: bool = False) -> str:
    legal_chars = string.ascii_letters + string.digits
    if allow_glob:
        legal_chars += "*?"
    return "".join(char.lower() for char in value if char in legal_chars)


def target_token_variants(value: str, allow_glob: bool = False) -> list[str]:
    compact = compact_search_token(value, allow_glob=allow_glob)
    if not compact:
        return []

    variants: list[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        key = token.strip().lower()
        if not key or key in seen:
            return
        seen.add(key)
        variants.append(key)

    add(compact)
    if compact.startswith("a") and len(compact) > 1 and compact[1].isdigit():
        add(f"mcx{compact}")
    if compact.startswith("mcx") and len(compact) > 3 and compact[3].isdigit():
        add(f"mcxa{compact[3:]}")
    return variants


def target_search_keys(value: str) -> set[str]:
    keys = set(target_token_variants(value))
    normalized = normalize_target_name(value)
    compact = compact_target_name(value)
    return {key for key in keys | {normalized, compact} if key}


def target_glob_patterns(value: str) -> list[str]:
    patterns: list[str] = []
    seen: set[str] = set()

    def add(pattern: str) -> None:
        key = pattern.strip().lower()
        if not key or key in seen or ("*" not in key and "?" not in key):
            return
        seen.add(key)
        patterns.append(key)

    for compact in target_token_variants(value, allow_glob=True):
        add(compact)
        add(re.sub(r"(?<=\d)x+$", "*", compact))
    return patterns


def format_hex(value: int, width_bits: int = 32) -> str:
    width_nibbles = max(1, width_bits // 4)
    mask = (1 << width_bits) - 1
    return f"0x{value & mask:0{width_nibbles}X}"


def friendly_exception(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def is_debug_session_command(command: str | None) -> bool:
    return bool(command and command.startswith(DEBUG_SESSION_PREFIX) and command != "debug-open")


def debug_session_command_name(command: str) -> str:
    if not command.startswith(DEBUG_SESSION_PREFIX):
        raise RuntimeError(f"Not a debug-session command: {command}")
    return command[len(DEBUG_SESSION_PREFIX) :]


def effective_command_name(command: str | None) -> str | None:
    if command is None:
        return None
    if is_debug_session_command(command):
        return debug_session_command_name(command)
    return command


def command_uses_target(args: argparse.Namespace) -> bool:
    return getattr(args, "command", None) in {
        "debug-open",
        "attach",
        "status",
        "halt",
        "resume",
        "step",
        "reset",
        "flash",
        "erase",
        "regs",
        "exception-frame",
        "mem-read",
        "mem-write",
        "breakpoint-set",
        "breakpoint-clear",
        "stack",
        "fault",
        "vector-table",
    }


def candidate_config_dirs(args: argparse.Namespace, cwd: Path) -> list[Path]:
    directories = [cwd]
    image_path = getattr(args, "image", None)
    if image_path:
        image_dir = Path(image_path).expanduser().resolve().parent
        if image_dir not in directories:
            directories.append(image_dir)
    return directories


def find_target_config_path(args: argparse.Namespace, cwd: Path | None = None) -> Path | None:
    cwd = Path.cwd() if cwd is None else Path(cwd)
    explicit_path = getattr(args, "target_config", None)
    if explicit_path:
        resolved = Path(explicit_path).expanduser()
        if not resolved.is_absolute():
            resolved = cwd / resolved
        resolved = resolved.resolve()
        if not resolved.exists():
            raise RuntimeError(f"Target config file was not found: {resolved}")
        return resolved

    for directory in candidate_config_dirs(args, cwd):
        for filename in DEFAULT_TARGET_CONFIG_FILENAMES:
            candidate = directory / filename
            if candidate.exists():
                return candidate.resolve()

    return None


def load_target_config(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "PyYAML is required to read target config files. Install it with `python -m pip install pyyaml`."
        ) from exc

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise RuntimeError(f"Target config must contain a YAML mapping at the top level: {path}")
    return data


def dump_target_config(path: Path, chip_name: str, target: str, vendor: str | None = None) -> None:
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "PyYAML is required to write target config files. Install it with `python -m pip install pyyaml`."
        ) from exc

    payload: dict[str, Any] = {
        "chip_name": chip_name,
        "aliases": {
            chip_name: target,
        },
    }
    if vendor:
        payload = {"vendor": vendor, **payload}

    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )


def load_target_catalog() -> list[dict[str, Any]]:
    try:
        from pyocd.tools.lists import ListGenerator
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("pyocd"):
            raise RuntimeError(
                "pyOCD is not installed. Install it with `python -m pip install pyocd` "
                "and ensure the probe drivers are available."
            ) from exc
        raise
    return list(ListGenerator.list_targets()["targets"])


def pyocd_pack_search_terms(value: str) -> list[str]:
    raw = value.strip()
    compact = compact_target_name(raw)
    normalized = normalize_target_name(raw)
    candidates: list[str] = []
    seen: set[str] = set()
    for candidate in (raw, compact.upper(), compact, normalized.upper(), normalized):
        text = candidate.strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(text)
    return candidates


def pyocd_pack_family_patterns(value: str) -> list[str]:
    return [pattern.upper() for pattern in target_glob_patterns(value)]


def parse_pyocd_pack_find_output(output: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in output.splitlines():
        stripped = line.strip()
        if (
            not stripped
            or stripped.lower().startswith("part")
            or set(stripped) == {"-"}
            or stripped.startswith("000")
        ):
            continue
        columns = re.split(r"\s{2,}", stripped)
        if len(columns) < 5:
            continue
        part, vendor, pack, version, installed = columns[:5]
        rows.append(
            {
                "part": part,
                "vendor": vendor,
                "pack": pack,
                "version": version,
                "installed": installed.lower() == "true",
            }
        )
    return rows


def group_pack_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row.get("pack", "")),
            str(row.get("vendor", "")),
            str(row.get("version", "")),
        )
        group = groups.setdefault(
            key,
            {
                "pack": key[0],
                "vendor": key[1],
                "version": key[2],
                "installed": False,
                "parts": [],
            },
        )
        if row.get("installed"):
            group["installed"] = True
        part = str(row.get("part", "")).strip()
        if part and part not in group["parts"]:
            group["parts"].append(part)
    return list(groups.values())


def format_pack_candidates(candidates: list[dict[str, Any]]) -> str:
    summaries: list[str] = []
    for candidate in candidates:
        parts = list(candidate.get("parts", []))
        pack_name = str(candidate.get("pack", "")).strip()
        family_match = re.search(r"(MCX[A-Z0-9]+)_DFP$", pack_name, re.IGNORECASE)
        family_name = family_match.group(1).upper() if family_match else ""
        preferred_parts = sorted(parts, key=lambda part: (0 if part.endswith("VLL") else 1, part))
        suggestions: list[str] = []
        if family_name:
            suggestions.append(family_name)
        for part in preferred_parts:
            upper_part = part.upper()
            if upper_part not in suggestions:
                suggestions.append(upper_part)
            if len(suggestions) >= 3:
                break
        summaries.append(
            f"{pack_name} ({candidate.get('vendor')}, try: {', '.join(suggestions) or 'more specific part number'})"
        )
    return "; ".join(summaries)


def glob_pattern_bases(patterns: list[str]) -> set[str]:
    bases: set[str] = set()
    for pattern in patterns:
        base = pattern.replace("*", "").replace("?", "").strip().lower()
        if base:
            bases.add(base)
    return bases


def format_pack_command_output(completed: subprocess.CompletedProcess[str]) -> str:
    parts: list[str] = []
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(stderr)
    return "\n".join(parts).strip()


def summarize_pack_attempts(attempts: list[dict[str, Any]]) -> str:
    if not attempts:
        return "no pack-install attempt was run"
    summaries: list[str] = []
    for attempt in attempts:
        label = str(attempt.get("kind", "install"))
        term = str(attempt.get("term") or attempt.get("pattern") or "<unknown>")
        if "error" in attempt:
            summaries.append(f"{label} {term}: {attempt['error']}")
            continue
        output = str(attempt.get("output", "")).strip()
        if output:
            summaries.append(f"{label} {term}: rc={attempt.get('returncode')} {output.splitlines()[0]}")
        else:
            summaries.append(f"{label} {term}: rc={attempt.get('returncode')}")
    return "; ".join(summaries)


def attempt_pack_install_for_target(raw_name: str) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    used_index_update = False
    for index, term in enumerate(pyocd_pack_search_terms(raw_name)):
        cache_key = term.lower()
        if cache_key in AUTO_PACK_INSTALL_ATTEMPTS:
            continue
        AUTO_PACK_INSTALL_ATTEMPTS.add(cache_key)
        command = [sys.executable, "-m", "pyocd", "pack", "install"]
        if not used_index_update:
            command.append("-u")
            used_index_update = True
        command.append(term)
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=PACK_INSTALL_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            attempts.append({"term": term, "error": friendly_exception(exc)})
            continue
        attempts.append(
            {
                "kind": "install",
                "term": term,
                "returncode": completed.returncode,
                "output": format_pack_command_output(completed),
            }
        )

    for pattern in pyocd_pack_family_patterns(raw_name):
        command = [sys.executable, "-m", "pyocd", "pack", "find"]
        if not used_index_update:
            command.append("-u")
            used_index_update = True
        command.extend(["--no-header", pattern])
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=PACK_INSTALL_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            attempts.append({"kind": "find", "pattern": pattern, "error": friendly_exception(exc)})
            continue

        output = format_pack_command_output(completed)
        attempts.append(
            {
                "kind": "find",
                "pattern": pattern,
                "returncode": completed.returncode,
                "output": output,
            }
        )
        rows = parse_pyocd_pack_find_output(output)
        if not rows:
            continue

        pack_candidates = group_pack_candidates(rows)
        if len(pack_candidates) > 1:
            return {
                "attempted": True,
                "attempts": attempts,
                "ambiguous_packs": pack_candidates,
                "family_pattern": pattern,
            }

        selected_pack = pack_candidates[0]
        representative_part = next(iter(selected_pack.get("parts", [])), None)
        if selected_pack.get("installed") or not representative_part:
            return {
                "attempted": True,
                "attempts": attempts,
                "selected_pack": selected_pack,
                "family_pattern": pattern,
            }

        install_command = [sys.executable, "-m", "pyocd", "pack", "install", str(representative_part)]
        try:
            install_completed = subprocess.run(
                install_command,
                check=False,
                capture_output=True,
                text=True,
                timeout=PACK_INSTALL_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            attempts.append(
                {
                    "kind": "install-family",
                    "term": str(representative_part),
                    "error": friendly_exception(exc),
                }
            )
            return {
                "attempted": True,
                "attempts": attempts,
                "selected_pack": selected_pack,
                "family_pattern": pattern,
            }

        attempts.append(
            {
                "kind": "install-family",
                "term": str(representative_part),
                "returncode": install_completed.returncode,
                "output": format_pack_command_output(install_completed),
            }
        )
        return {
            "attempted": True,
            "attempts": attempts,
            "selected_pack": selected_pack,
            "family_pattern": pattern,
        }

    return {"attempted": bool(attempts), "attempts": attempts}


def can_prompt_for_target_config(interactive: bool | None = None) -> bool:
    if interactive is not None:
        return interactive
    return sys.stdin.isatty() and sys.stdout.isatty()


def dedupe_target_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for entry in entries:
        unique[str(entry.get("name"))] = entry
    return list(unique.values())


def format_target_candidates(entries: list[dict[str, Any]]) -> str:
    return "; ".join(
        f"{entry.get('name')} ({entry.get('part_number')}, {entry.get('vendor')})"
        for entry in dedupe_target_entries(entries)
    )


def select_unique_target_match(
    entries: list[dict[str, Any]],
    raw_name: str,
    source: str,
) -> dict[str, Any]:
    unique_entries = dedupe_target_entries(entries)
    if len(unique_entries) == 1:
        entry = unique_entries[0]
        return {
            "target": str(entry["name"]),
            "part_number": entry.get("part_number"),
            "vendor": entry.get("vendor"),
            "source": source,
            "input_name": raw_name,
        }
    raise RuntimeError(
        f"Ambiguous target match for '{raw_name}'. Candidates: {format_target_candidates(unique_entries)}"
    )


def filter_catalog_by_vendor(catalog: list[dict[str, Any]], vendor: str | None) -> list[dict[str, Any]]:
    if not vendor:
        return catalog
    vendor_text = vendor.strip().lower()
    filtered = [entry for entry in catalog if vendor_text in str(entry.get("vendor", "")).lower()]
    return filtered or catalog


def _resolve_target_from_catalog_local(
    raw_name: str,
    catalog: list[dict[str, Any]],
    vendor: str | None = None,
) -> dict[str, Any]:
    filtered_catalog = filter_catalog_by_vendor(catalog, vendor)
    raw_keys = target_search_keys(raw_name)
    raw_globs = target_glob_patterns(raw_name)
    raw_glob_bases = glob_pattern_bases(raw_globs)
    exact_matches: list[dict[str, Any]] = []
    family_matches: list[dict[str, Any]] = []
    glob_matches: list[dict[str, Any]] = []
    prefix_matches: list[dict[str, Any]] = []
    contains_matches: list[dict[str, Any]] = []

    for entry in filtered_catalog:
        entry_keys = target_search_keys(str(entry.get("name", ""))) | target_search_keys(
            str(entry.get("part_number", ""))
        )
        if not entry_keys:
            continue
        if raw_keys & entry_keys:
            exact_matches.append(entry)
            continue
        if raw_glob_bases and raw_glob_bases & entry_keys:
            family_matches.append(entry)
            continue
        if raw_globs and any(fnmatch.fnmatch(candidate, pattern) for pattern in raw_globs for candidate in entry_keys):
            glob_matches.append(entry)
            continue
        if any(raw.startswith(candidate) or candidate.startswith(raw) for raw in raw_keys for candidate in entry_keys):
            prefix_matches.append(entry)
            continue
        if any(raw in candidate or candidate in raw for raw in raw_keys for candidate in entry_keys):
            contains_matches.append(entry)

    if exact_matches:
        return select_unique_target_match(exact_matches, raw_name, "catalog-exact")
    if family_matches:
        return select_unique_target_match(family_matches, raw_name, "catalog-family")
    if glob_matches:
        return select_unique_target_match(glob_matches, raw_name, "catalog-glob")
    if prefix_matches:
        return select_unique_target_match(prefix_matches, raw_name, "catalog-prefix")
    if contains_matches:
        return select_unique_target_match(contains_matches, raw_name, "catalog-contains")

    raise RuntimeError(
        f"No pyOCD target match was found for '{raw_name}'. Use --target or add an aliases entry to the YAML file."
    )


def resolve_target_from_catalog(
    raw_name: str,
    catalog: list[dict[str, Any]],
    vendor: str | None = None,
    allow_pack_install: bool = True,
) -> dict[str, Any]:
    try:
        return _resolve_target_from_catalog_local(raw_name, catalog, vendor=vendor)
    except RuntimeError as exc:
        message = str(exc)
        can_retry_with_pack_search = "No pyOCD target match was found" in message or (
            "Ambiguous target match" in message and bool(target_glob_patterns(raw_name))
        )
        if (not allow_pack_install) or (not can_retry_with_pack_search):
            raise

        install_report = attempt_pack_install_for_target(raw_name)
        if not install_report["attempted"]:
            raise

        if install_report.get("ambiguous_packs"):
            raise RuntimeError(
                f"No local pyOCD target match was found for '{raw_name}', and fuzzy CMSIS-Pack search matched multiple packs "
                f"for pattern '{install_report.get('family_pattern')}'. Use a more specific device name. "
                f"Candidate packs: {format_pack_candidates(install_report['ambiguous_packs'])}. "
                f"Attempts: {summarize_pack_attempts(install_report['attempts'])}"
            ) from exc

        refreshed_catalog = load_target_catalog()
        try:
            resolved = _resolve_target_from_catalog_local(raw_name, refreshed_catalog, vendor=vendor)
        except RuntimeError as retry_exc:
            if install_report.get("selected_pack"):
                selected_pack = install_report["selected_pack"]
                raise RuntimeError(
                    f"Automatic CMSIS-Pack search found {selected_pack.get('pack')} for '{raw_name}'"
                    f"{' via pattern ' + repr(install_report.get('family_pattern')) if install_report.get('family_pattern') else ''}, "
                    f"but the name still does not identify a unique pyOCD target. Use a more specific device name. "
                    f"Candidates now visible to pyOCD: {retry_exc}. Attempts: {summarize_pack_attempts(install_report['attempts'])}"
                ) from retry_exc
            raise RuntimeError(
                f"No pyOCD target match was found for '{raw_name}' locally, and automatic CMSIS-Pack install did not add support. "
                "This usually means neither the local installation nor the remote pack index contains a matching device. "
                f"Attempts: {summarize_pack_attempts(install_report['attempts'])}"
            ) from retry_exc

        resolved["pack_auto_install"] = True
        resolved["pack_auto_install_attempts"] = install_report["attempts"]
        return resolved


def prompt_to_create_target_config(
    cwd: Path,
    catalog: list[dict[str, Any]],
    prompt_fn=input,
    output_fn=print,
) -> Path:
    config_path = cwd / DEFAULT_TARGET_CONFIG_FILENAMES[0]
    output_fn(
        f"No {DEFAULT_TARGET_CONFIG_FILENAMES[0]} was found in {cwd}. "
        "Please enter the local MCU model so the helper can create one."
    )
    chip_name = prompt_fn("Local MCU/chip model: ").strip()
    if not chip_name:
        raise RuntimeError("Target config setup was cancelled before a chip model was entered.")

    try:
        resolved = resolve_target_from_catalog(chip_name, catalog)
    except RuntimeError as exc:
        output_fn(str(exc))
        explicit_target = prompt_fn("pyOCD target name to save: ").strip()
        if not explicit_target:
            raise RuntimeError("Target config setup was cancelled before a pyOCD target was entered.")
        resolved = resolve_target_from_catalog(explicit_target, catalog)

    dump_target_config(config_path, chip_name, resolved["target"], resolved.get("vendor"))
    output_fn(f"Created {config_path} for pyOCD target '{resolved['target']}'.")
    return config_path


def create_target_config_from_known_chip_name(
    cwd: Path,
    chip_name: str,
    catalog: list[dict[str, Any]],
    explicit_target: str | None = None,
    output_fn=print,
) -> Path:
    config_path = cwd / DEFAULT_TARGET_CONFIG_FILENAMES[0]
    raw_name = chip_name.strip()
    if not raw_name:
        raise RuntimeError("Known chip name was empty; cannot create pyocd-targets.yaml.")
    lookup_name = explicit_target.strip() if explicit_target else raw_name
    resolved = resolve_target_from_catalog(lookup_name, catalog)
    dump_target_config(config_path, raw_name, resolved["target"], resolved.get("vendor"))
    output_fn(f"Created {config_path} for pyOCD target '{resolved['target']}' from known chip model '{raw_name}'.")
    return config_path


def resolve_target_metadata(
    args: argparse.Namespace,
    catalog: list[dict[str, Any]] | None = None,
    cwd: Path | None = None,
    prompt_fn=input,
    output_fn=print,
    interactive: bool | None = None,
) -> dict[str, Any] | None:
    cwd = Path.cwd() if cwd is None else Path(cwd)
    config_path = find_target_config_path(args, cwd=cwd)
    catalog = load_target_catalog() if catalog is None else catalog
    chip_name = getattr(args, "chip_name", None)
    explicit_target = getattr(args, "target", None)

    if config_path is None and chip_name:
        config_path = create_target_config_from_known_chip_name(
            cwd=cwd,
            chip_name=str(chip_name),
            catalog=catalog,
            explicit_target=str(explicit_target) if explicit_target else None,
            output_fn=output_fn,
        )

    if explicit_target:
        return None

    if config_path is None:
        if not can_prompt_for_target_config(interactive):
            raise RuntimeError(
                f"No {DEFAULT_TARGET_CONFIG_FILENAMES[0]} was found in {cwd}. "
                "Ask the user for the MCU model, pass --chip-name, or create the YAML file before retrying."
            )
        config_path = prompt_to_create_target_config(
            cwd=cwd,
            catalog=catalog,
            prompt_fn=prompt_fn,
            output_fn=output_fn,
        )

    config = load_target_config(config_path)
    vendor = config.get("vendor")

    direct_target = next(
        (
            str(config[key]).strip()
            for key in ("pyocd_target", "target_override", "target")
            if key in config and str(config[key]).strip()
        ),
        None,
    )
    if direct_target:
        resolved = resolve_target_from_catalog(direct_target, catalog, vendor=vendor)
        resolved["source"] = "yaml-target"
        resolved["config_path"] = str(config_path)
        return resolved

    raw_name = next(
        (
            str(config[key]).strip()
            for key in TARGET_CONFIG_NAME_KEYS
            if key in config and str(config[key]).strip()
        ),
        None,
    )
    if raw_name is None:
        raise RuntimeError(
            f"Target config did not provide a target hint. Add one of: {', '.join(TARGET_CONFIG_NAME_KEYS)}"
        )

    aliases = config.get("aliases", {})
    if aliases and not isinstance(aliases, dict):
        raise RuntimeError(f"Target config field 'aliases' must be a YAML mapping: {config_path}")
    alias_map = {normalize_target_name(str(key)): str(value).strip() for key, value in aliases.items()}
    alias_target = alias_map.get(normalize_target_name(raw_name))
    if alias_target:
        resolved = resolve_target_from_catalog(alias_target, catalog, vendor=vendor)
        resolved["source"] = "yaml-alias"
        resolved["config_path"] = str(config_path)
        return resolved

    patterns = config.get("patterns", {})
    if patterns and not isinstance(patterns, dict):
        raise RuntimeError(f"Target config field 'patterns' must be a YAML mapping: {config_path}")
    lowered_raw_name = raw_name.lower()
    compact_raw_name = compact_target_name(raw_name)
    for pattern, target_name in patterns.items():
        normalized_pattern = normalize_target_name(str(pattern))
        compact_pattern = compact_target_name(str(pattern))
        if (
            fnmatch.fnmatch(lowered_raw_name, str(pattern).lower())
            or fnmatch.fnmatch(normalize_target_name(raw_name), normalized_pattern)
            or fnmatch.fnmatch(compact_raw_name, compact_pattern)
        ):
            resolved = resolve_target_from_catalog(str(target_name).strip(), catalog, vendor=vendor)
            resolved["source"] = "yaml-pattern"
            resolved["config_path"] = str(config_path)
            return resolved

    resolved = resolve_target_from_catalog(raw_name, catalog, vendor=vendor)
    resolved["config_path"] = str(config_path)
    return resolved


def apply_target_resolution(args: argparse.Namespace) -> None:
    if not command_uses_target(args):
        return
    resolved = resolve_target_metadata(args)
    if resolved is None and getattr(args, "target", None):
        resolved = resolve_target_from_catalog(str(args.target), load_target_catalog())
        resolved["source"] = "explicit-target"
        resolved["config_path"] = None
    if resolved is None:
        return
    args.resolved_target = resolved["target"]
    args.resolved_target_source = resolved["source"]
    args.resolved_target_config = resolved["config_path"]


def require_confirmation(args: argparse.Namespace) -> int | None:
    effective_command = effective_command_name(getattr(args, "command", None))
    if effective_command in MUTATING_COMMANDS and not getattr(args, "yes", False):
        return emit(
            "error",
            f"{args.command} requires explicit confirmation via --yes.",
            command=args.command,
            next_step="Re-run with --yes after confirming the target, image, and intended hardware change.",
        )
    return None


def load_pyocd() -> tuple[Any, Any, Any]:
    try:
        from pyocd.core.helpers import ConnectHelper
        from pyocd.flash.file_programmer import FileProgrammer
        from pyocd.core.target import Target
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("pyocd"):
            raise RuntimeError(
                "pyOCD is not installed. Install it with `python -m pip install pyocd` "
                "and ensure the probe drivers are available."
            ) from exc
        raise
    return ConnectHelper, FileProgrammer, Target


def load_pyocd_flash_eraser() -> Any:
    try:
        from pyocd.flash.eraser import FlashEraser
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("pyocd"):
            raise RuntimeError(
                "pyOCD is not installed. Install it with `python -m pip install pyocd` "
                "and ensure the probe drivers are available."
            ) from exc
        raise
    return FlashEraser


def load_probe_backend_uids() -> tuple[set[str], set[str]]:
    hidapi_uids: set[str] = set()
    cmsis_dap_v2_uids: set[str] = set()
    try:
        from pyocd.probe.pydapaccess.interface.hidapi_backend import HidApiUSB

        hidapi_uids = {
            str(getattr(interface, "serial_number", ""))
            for interface in HidApiUSB.get_all_connected_interfaces()
            if getattr(interface, "serial_number", None)
        }
    except Exception:
        hidapi_uids = set()

    try:
        from pyocd.probe.pydapaccess.interface.pyusb_v2_backend import PyUSBv2

        cmsis_dap_v2_uids = {
            str(getattr(interface, "serial_number", ""))
            for interface in PyUSBv2.get_all_connected_interfaces()
            if getattr(interface, "serial_number", None)
        }
    except Exception:
        cmsis_dap_v2_uids = set()

    return hidapi_uids, cmsis_dap_v2_uids


def session_options(args: argparse.Namespace) -> dict[str, Any]:
    options: dict[str, Any] = {
        "cmsis_dap.prefer_v1": False,
        "enable_swv": False,
        "swv_raw_enable": False,
    }
    if getattr(args, "persistent_session", False):
        options["resume_on_disconnect"] = False
    if getattr(args, "command", None) in {"halt", "step"} or getattr(args, "halt_after_reset", False):
        options["resume_on_disconnect"] = False
    if getattr(args, "frequency", None):
        options["frequency"] = args.frequency
    if getattr(args, "halt_on_connect", False):
        options["connect_mode"] = "halt"
        options["resume_on_disconnect"] = False
    elif getattr(args, "connect_mode", None):
        options["connect_mode"] = args.connect_mode
    return options


def session_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "blocking": False,
        "options": session_options(args),
    }
    if getattr(args, "uid", None):
        kwargs["unique_id"] = args.uid
    target_override = getattr(args, "resolved_target", None) or getattr(args, "target", None)
    if target_override:
        kwargs["target_override"] = target_override
    return kwargs


def pyocd_image_format(image_path: str) -> str | None:
    suffix = Path(image_path).suffix.lower()
    if suffix in {".elf", ".axf", ".out"}:
        return "elf"
    if suffix == ".hex":
        return "hex"
    if suffix == ".bin":
        return "bin"
    return None


def choose_session(connect_helper: Any, args: argparse.Namespace):
    probes = connect_helper.get_all_connected_probes(blocking=False)
    ensure_probe_selection(probes, getattr(args, "uid", None))
    kwargs = session_kwargs(args)
    try:
        session = connect_helper.session_with_chosen_probe(auto_open=False, **kwargs)
    except TypeError:
        session = connect_helper.session_with_chosen_probe(**kwargs)
    if session is None:
        raise RuntimeError(
            "No probe/target session could be created. Check the connection, power, and target selector."
        )
    return session


def open_session(session) -> None:
    session.open()


def close_session(session) -> None:
    try:
        session.close()
    except Exception:
        pass


def debug_sessions_root() -> Path:
    return Path(tempfile.gettempdir()) / "mcu-debug-probe" / "sessions"


def validate_session_id(session_id: str) -> str:
    if not session_id or session_id != Path(session_id).name or any(sep in session_id for sep in ("/", "\\")):
        raise RuntimeError(f"Invalid debug session ID: {session_id!r}")
    return session_id


def debug_session_dir(session_id: str) -> Path:
    return debug_sessions_root() / validate_session_id(session_id)


def debug_session_metadata_path(session_id: str) -> Path:
    return debug_session_dir(session_id) / "session.json"


def debug_session_requests_dir(session_id: str) -> Path:
    return debug_session_dir(session_id) / "requests"


def debug_session_responses_dir(session_id: str) -> Path:
    return debug_session_dir(session_id) / "responses"


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temp_path.replace(path)


def read_json_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def update_session_metadata(session_id: str, **fields: Any) -> dict[str, Any]:
    path = debug_session_metadata_path(session_id)
    current = read_json_file(path) if path.exists() else {"session_id": session_id}
    current.update(fields)
    current["updated_at"] = time.time()
    write_json_atomic(path, current)
    return current


def load_session_metadata(session_id: str) -> dict[str, Any]:
    path = debug_session_metadata_path(session_id)
    if not path.exists():
        raise RuntimeError(f"Debug session was not found: {session_id}")
    metadata = read_json_file(path)
    metadata.setdefault("session_id", session_id)
    return metadata


def build_debug_session_request(args: argparse.Namespace) -> dict[str, Any]:
    request_args = {
        key: value
        for key, value in vars(args).items()
        if key not in {"command", "session_id"} and value is not None
    }
    return {
        "command": debug_session_command_name(args.command),
        "args": request_args,
    }


def wait_for_debug_session_ready(
    session_id: str,
    timeout_seconds: float = DEBUG_SESSION_READY_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last_metadata: dict[str, Any] | None = None
    while time.time() < deadline:
        metadata_path = debug_session_metadata_path(session_id)
        if metadata_path.exists():
            last_metadata = load_session_metadata(session_id)
            status = last_metadata.get("status")
            if status == "ready":
                return last_metadata
            if status == "error":
                raise RuntimeError(str(last_metadata.get("summary") or last_metadata.get("error") or "Debug session failed to start."))
        time.sleep(DEBUG_SESSION_POLL_SECONDS)
    if last_metadata is not None:
        raise RuntimeError(
            f"Timed out waiting for debug session {session_id} to become ready. Last status: {last_metadata.get('status')}"
        )
    raise RuntimeError(f"Timed out waiting for debug session {session_id} metadata.")


def start_debug_session_server(args: argparse.Namespace) -> str:
    session_id = uuid.uuid4().hex[:12]
    session_dir = debug_session_dir(session_id)
    debug_session_requests_dir(session_id).mkdir(parents=True, exist_ok=True)
    debug_session_responses_dir(session_id).mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    log_path = session_dir / "server.log"
    metadata = {
        "session_id": session_id,
        "status": "starting",
        "summary": "Debug session is starting.",
        "token": token,
        "session_dir": str(session_dir),
        "log_path": str(log_path),
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    write_json_atomic(debug_session_metadata_path(session_id), metadata)

    script_path = Path(__file__).resolve()
    command = [
        sys.executable,
        str(script_path),
        "_session-server",
        "--session-id",
        session_id,
        "--token",
        token,
        "--idle-timeout",
        str(getattr(args, "idle_timeout", DEBUG_SESSION_IDLE_TIMEOUT_SECONDS)),
    ]
    if getattr(args, "uid", None):
        command.extend(["--uid", args.uid])
    target_override = getattr(args, "resolved_target", None) or getattr(args, "target", None)
    if target_override:
        command.extend(["--target", target_override])
    if getattr(args, "frequency", None):
        command.extend(["--frequency", str(args.frequency)])
    if getattr(args, "connect_mode", None):
        command.extend(["--connect-mode", args.connect_mode])
    if getattr(args, "halt_on_connect", False):
        command.append("--halt-on-connect")

    creationflags = 0
    creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)

    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            close_fds=True,
            creationflags=creationflags,
            cwd=str(Path.cwd()),
        )

    update_session_metadata(session_id, pid=process.pid)
    return session_id


def send_session_request(
    metadata: dict[str, Any],
    request: dict[str, Any],
    timeout_seconds: float = DEBUG_SESSION_REQUEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    session_id = validate_session_id(str(metadata.get("session_id") or ""))
    request_id = uuid.uuid4().hex
    request_payload = {
        "request_id": request_id,
        "token": metadata.get("token"),
        **request,
    }
    request_path = debug_session_requests_dir(session_id) / f"{request_id}.json"
    response_path = debug_session_responses_dir(session_id) / f"{request_id}.json"
    write_json_atomic(request_path, request_payload)

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if response_path.exists():
            response = read_json_file(response_path)
            try:
                response_path.unlink()
            except OSError:
                pass
            return response
        current_metadata = load_session_metadata(session_id)
        if current_metadata.get("status") == "error":
            raise RuntimeError(str(current_metadata.get("summary") or current_metadata.get("error") or "Debug session failed."))
        time.sleep(DEBUG_SESSION_POLL_SECONDS)
    raise RuntimeError(f"Timed out waiting for debug session {session_id} to respond to {request.get('command')}.")


def probe_to_dict(probe: Any) -> dict[str, Any]:
    return {
        "description": getattr(probe, "description", None),
        "product_name": getattr(probe, "product_name", None),
        "vendor_name": getattr(probe, "vendor_name", None),
        "unique_id": getattr(probe, "unique_id", None),
    }


def infer_probe_transport(probe: Any) -> str:
    class_name = type(probe).__name__.lower()
    description = str(getattr(probe, "description", "")).lower()
    product_name = str(getattr(probe, "product_name", "")).lower()
    text = " ".join([class_name, description, product_name])
    if "jlink" in text or "j-link" in text:
        return "jlink"
    if "stlink" in text or "st-link" in text:
        return "stlink"
    if "cmsisdap" in text or "cmsis-dap" in text:
        return "cmsis-dap"
    if "picoprobe" in text:
        return "picoprobe"
    return "unknown"


def describe_probe_capability(
    probe: Any,
    cmsis_dap_v1_uids: set[str],
    cmsis_dap_v2_uids: set[str],
) -> dict[str, Any]:
    info = probe_to_dict(probe)
    uid = str(info.get("unique_id") or "")
    transport = infer_probe_transport(probe)
    is_cmsis_dap = transport == "cmsis-dap"
    info.update(
        {
            "transport": transport,
            "cmsis_dap_v1": is_cmsis_dap and (uid in cmsis_dap_v1_uids),
            "cmsis_dap_v2": is_cmsis_dap and (uid in cmsis_dap_v2_uids),
            "jlink": transport == "jlink",
            "stlink": transport == "stlink",
            "picoprobe": transport == "picoprobe",
        }
    )
    if info["cmsis_dap_v2"]:
        info["preferred_backend"] = "cmsis-dap-v2"
    elif info["cmsis_dap_v1"]:
        info["preferred_backend"] = "cmsis-dap-v1"
    elif transport != "unknown":
        info["preferred_backend"] = transport
    else:
        info["preferred_backend"] = None
    return info


def probe_display_lines(probes: list[Any]) -> list[str]:
    lines = []
    for probe in probes:
        lines.append(
            f"{getattr(probe, 'unique_id', None)} - "
            f"{getattr(probe, 'description', None) or getattr(probe, 'product_name', None)}"
        )
    return lines


def ensure_probe_selection(probes: list[Any], uid: str | None) -> None:
    if uid or len(probes) <= 1:
        return
    joined = "; ".join(probe_display_lines(probes))
    raise RuntimeError(
        "Multiple probes detected. Ask the user to choose one and re-run with --uid. "
        f"Candidates: {joined}"
    )


def read_registers(target: Any, names: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for name in names:
        try:
            raw = target.read_core_register_raw(name)
        except Exception:
            continue
        values[name] = format_hex(int(raw))
    return values


def decode_bits(value: int, bit_map: dict[int, str]) -> list[str]:
    return [name for bit, name in bit_map.items() if value & (1 << bit)]


def read_words(target: Any, address: int, count: int) -> list[int]:
    return read_memory(target, address, 32, count)


def read_words_best_effort(target: Any, address: int, count: int) -> list[int]:
    try:
        return read_words(target, address, count)
    except Exception:
        values: list[int] = []
        for index in range(count):
            word_address = address + (index * 4)
            try:
                values.extend(read_words(target, word_address, 1))
            except Exception:
                break
        return values


def read_word(target: Any, address: int) -> int:
    return read_words(target, address, 1)[0]


def read_named_words_best_effort(target: Any, named_addresses: dict[str, int]) -> dict[str, int | None]:
    values: dict[str, int | None] = {}
    for name, address in named_addresses.items():
        try:
            values[name] = read_word(target, address)
        except Exception:
            values[name] = None
    return values


def architecture_name(session) -> str:
    core = getattr(session.target, "selected_core", None)
    arch = getattr(core, "architecture", None)
    return getattr(arch, "name", str(arch) if arch is not None else "unknown")


def vector_name_for_index(index: int) -> str:
    if index in EXCEPTION_VECTOR_NAMES:
        return EXCEPTION_VECTOR_NAMES[index]
    return f"IRQ{index - 16}"


def decode_exception_frame(base_address: int, words: list[int]) -> dict[str, Any]:
    register_names = ["r0", "r1", "r2", "r3", "r12", "lr", "pc", "xpsr"]
    registers = {
        name: format_hex(words[index])
        for index, name in enumerate(register_names[: len(words)])
    }
    return {
        "stack_pointer": format_hex(base_address),
        "complete": len(words) >= 8,
        "registers": registers,
    }


def target_summary(session) -> dict[str, Any]:
    target = session.target
    board = getattr(session, "board", None)
    board_target = getattr(board, "target_type", None)
    state = getattr(target, "get_state", lambda: "unknown")()
    state_name = getattr(state, "name", str(state))
    return {
        "board": getattr(board, "name", None),
        "target_override": getattr(session.options, "get", lambda *_: None)("target_override"),
        "board_target_type": board_target,
        "architecture": architecture_name(session),
        "state": state_name,
        "registers": read_registers(target, DEFAULT_REGISTERS),
    }


def payload_attach_like(session, command: str) -> dict[str, Any]:
    info = target_summary(session)
    return build_payload(
        "ok",
        f"{command} succeeded; target state is {info['state']}.",
        command=command,
        **info,
    )


def payload_halt_resume(session, command: str) -> dict[str, Any]:
    target = session.target
    if command == "halt":
        target.halt()
    else:
        target.resume()
    info = target_summary(session)
    return build_payload(
        "ok",
        f"{command} succeeded; target state is {info['state']}.",
        command=command,
        **info,
    )


def payload_mem_read(session, args: argparse.Namespace) -> dict[str, Any]:
    values = read_memory(session.target, args.address, args.width, args.count)
    formatted = [format_hex(value, args.width) for value in values]
    return build_payload(
        "ok",
        f"Read {len(values)} value(s) from {format_hex(args.address)}.",
        command="mem-read",
        address=format_hex(args.address),
        width=args.width,
        count=len(values),
        values=formatted,
    )


def payload_mem_write(session, args: argparse.Namespace) -> dict[str, Any]:
    write_memory(session.target, args.address, args.width, args.values)
    return build_payload(
        "ok",
        f"Wrote {len(args.values)} value(s) to {format_hex(args.address)}.",
        command="mem-write",
        address=format_hex(args.address),
        width=args.width,
        values=[format_hex(value, args.width) for value in args.values],
    )


def payload_regs(session, args: argparse.Namespace) -> dict[str, Any]:
    registers = read_registers(session.target, args.registers)
    return build_payload(
        "ok",
        f"Read {len(registers)} register(s).",
        command="regs",
        registers=registers,
    )


def payload_reset(session, args: argparse.Namespace, target_module: Any) -> dict[str, Any]:
    target = session.target
    reset_type = None
    if getattr(args, "reset_type", None):
        enum_name = args.reset_type.upper().replace("-", "_")
        reset_type = getattr(target_module.ResetType, enum_name, None)
    if getattr(args, "halt_after_reset", False):
        if reset_type is None:
            target.reset_and_halt()
        else:
            target.reset_and_halt(reset_type)
    else:
        if reset_type is None:
            target.reset()
        else:
            target.reset(reset_type)
    info = target_summary(session)
    return build_payload(
        "ok",
        f"reset succeeded; target state is {info['state']}.",
        command="reset",
        **info,
    )


def payload_step(session, args: argparse.Namespace) -> dict[str, Any]:
    target = session.target
    for _ in range(args.count):
        target.step(disable_interrupts=not args.allow_interrupts)
    info = target_summary(session)
    return build_payload(
        "ok",
        f"step succeeded for {args.count} instruction(s); target state is {info['state']}.",
        command="step",
        steps=args.count,
        **info,
    )


def payload_breakpoint_set(
    session,
    args: argparse.Namespace,
    target_class: Any,
) -> dict[str, Any]:
    bp_type = getattr(target_class.BreakpointType, args.breakpoint_type.upper())
    created = session.target.set_breakpoint(args.address, bp_type)
    info = target_summary(session)
    status = "ok" if created else "error"
    summary = (
        f"Breakpoint set at {format_hex(args.address)}."
        if created
        else f"Breakpoint could not be set at {format_hex(args.address)}."
    )
    return build_payload(
        status,
        summary,
        command="breakpoint-set",
        address=format_hex(args.address),
        breakpoint_type=args.breakpoint_type,
        **info,
    )


def payload_breakpoint_clear(session, args: argparse.Namespace) -> dict[str, Any]:
    session.target.remove_breakpoint(args.address)
    info = target_summary(session)
    return build_payload(
        "ok",
        f"Breakpoint cleared at {format_hex(args.address)}.",
        command="breakpoint-clear",
        address=format_hex(args.address),
        **info,
    )


def payload_stack(session, args: argparse.Namespace) -> dict[str, Any]:
    target = session.target
    registers = read_registers(target, ["sp", "pc", "lr", "xpsr", "msp", "psp"])
    sp_value = parse_int(registers.get("sp", "0x0"))
    base_address = args.address if args.address is not None else sp_value
    words = read_words_best_effort(target, base_address, args.words)
    formatted_words = [format_hex(value) for value in words]
    exception_frame = decode_exception_frame(base_address, words) if words else {}

    status = "ok" if words else "error"
    summary = f"Read {len(words)} stack word(s) from {format_hex(base_address)}."
    if words and len(words) < args.words:
        summary += " Stopped early after a memory access fault."
    elif not words:
        summary = f"Could not read stack words from {format_hex(base_address)}."

    return build_payload(
        status,
        summary,
        command="stack",
        address=format_hex(base_address),
        words=formatted_words,
        registers=registers,
        exception_frame_guess=exception_frame,
    )


def payload_fault(session) -> dict[str, Any]:
    target = session.target
    raw_values = read_named_words_best_effort(target, FAULT_REGISTERS)
    decoded = decode_fault_registers(raw_values)
    registers = read_registers(target, ["pc", "sp", "lr", "xpsr"])
    architecture = architecture_name(session)
    target_type = getattr(session.board, "target_type", None)
    analysis = summarize_fault_causes(decoded, architecture, target_type)
    summary = "Read Cortex-M fault status registers."
    if decoded["HFSR"]["flags"] or decoded["MMFSR"]["flags"] or decoded["BFSR"]["flags"] or decoded["UFSR"]["flags"]:
        summary = "Read Cortex-M fault status registers; fault flags are set."
    return build_payload(
        "ok",
        summary,
        command="fault",
        architecture=architecture,
        target_type=target_type,
        core_registers=registers,
        fault_registers={
            key: (format_hex(value) if value is not None else None)
            for key, value in raw_values.items()
        },
        decoded=decoded,
        analysis=analysis,
    )


def payload_exception_frame(session, args: argparse.Namespace) -> dict[str, Any]:
    target = session.target
    sp_value = int(target.read_core_register_raw("sp"))
    base_address = args.address if args.address is not None else sp_value
    words = read_words_best_effort(target, base_address, 8)
    frame = decode_exception_frame(base_address, words)
    status = "ok" if words else "error"
    summary = "Decoded exception stack frame." if len(words) >= 8 else "Read partial exception stack frame."
    if not words:
        summary = "Could not read an exception stack frame."
    return build_payload(
        status,
        summary,
        command="exception-frame",
        architecture=architecture_name(session),
        frame=frame,
        raw_words=[format_hex(value) for value in words],
    )


def payload_vector_table(session, args: argparse.Namespace) -> dict[str, Any]:
    target = session.target
    vtor = read_word(target, 0xE000ED08)
    vector_base = args.base_address if args.base_address is not None else vtor
    entries = read_words_best_effort(target, vector_base, args.count)
    icsr = read_word(target, 0xE000ED04)
    active_vector = icsr & 0x1FF
    decoded_entries = [
        {
            "index": index,
            "name": vector_name_for_index(index),
            "address": format_hex(vector_base + (index * 4)),
            "value": format_hex(value),
        }
        for index, value in enumerate(entries)
    ]
    return build_payload(
        "ok" if entries else "error",
        f"Read {len(entries)} vector table entrie(s) from {format_hex(vector_base)}.",
        command="vector-table",
        architecture=architecture_name(session),
        vtor=format_hex(vtor),
        vector_base=format_hex(vector_base),
        active_vector={
            "index": active_vector,
            "name": vector_name_for_index(active_vector),
        },
        entries=decoded_entries,
    )


def payload_debug_close(session) -> dict[str, Any]:
    info = target_summary(session)
    return build_payload(
        "ok",
        "Debug session closed; session breakpoints were cleared.",
        command="close",
        board=info.get("board"),
        target_override=info.get("target_override"),
        board_target_type=info.get("board_target_type"),
        architecture=info.get("architecture"),
        registers=info.get("registers"),
    )


def handle_probe(args: argparse.Namespace) -> int:
    connect_helper, _, _ = load_pyocd()
    probes = connect_helper.get_all_connected_probes(
        blocking=False,
        unique_id=getattr(args, "uid", None),
    )
    results = [probe_to_dict(probe) for probe in probes]
    return emit(
        "ok",
        f"Detected {len(results)} probe(s).",
        command="probe",
        probes=results,
        manual_selection_required=len(results) > 1,
    )


def handle_probe_capabilities(args: argparse.Namespace) -> int:
    connect_helper, _, _ = load_pyocd()
    probes = connect_helper.get_all_connected_probes(
        blocking=False,
        unique_id=getattr(args, "uid", None),
    )
    cmsis_dap_v1_uids, cmsis_dap_v2_uids = load_probe_backend_uids()
    results = [
        describe_probe_capability(probe, cmsis_dap_v1_uids, cmsis_dap_v2_uids)
        for probe in probes
    ]
    return emit(
        "ok",
        f"Detected capabilities for {len(results)} probe(s).",
        command="probe-capabilities",
        probes=results,
        manual_selection_required=len(results) > 1,
    )


def handle_resolve_target(args: argparse.Namespace) -> int:
    if getattr(args, "target", None):
        resolved = resolve_target_from_catalog(args.target, load_target_catalog())
        resolved["source"] = "explicit-target"
        resolved["config_path"] = None
    else:
        resolved = resolve_target_metadata(args)
        if resolved is None:
            raise RuntimeError(
                "No target hint was provided. Pass --target, --target-config, or create pyocd-targets.yaml."
            )
    return emit(
        "ok",
        f"Resolved pyOCD target '{resolved['target']}'.",
        command="resolve-target",
        **resolved,
    )


def handle_attach_like(args: argparse.Namespace, command: str) -> int:
    connect_helper, _, _ = load_pyocd()
    session = choose_session(connect_helper, args)
    try:
        open_session(session)
        return emit_payload(payload_attach_like(session, command))
    finally:
        close_session(session)


def handle_halt_resume(args: argparse.Namespace, command: str) -> int:
    connect_helper, _, _ = load_pyocd()
    session = choose_session(connect_helper, args)
    try:
        open_session(session)
        return emit_payload(payload_halt_resume(session, command))
    finally:
        close_session(session)


def handle_reset(args: argparse.Namespace) -> int:
    connect_helper, _, target_module = load_pyocd()
    session = choose_session(connect_helper, args)
    try:
        open_session(session)
        return emit_payload(payload_reset(session, args, target_module))
    finally:
        close_session(session)


def handle_flash(args: argparse.Namespace) -> int:
    connect_helper, file_programmer_cls, _ = load_pyocd()
    session = choose_session(connect_helper, args)
    try:
        open_session(session)

        # Mirror pyOCD's load flow: reset and halt in the same session before
        # invoking the flash algorithm so pack targets start from a clean state.
        secondary_cores = [c for c in session.target.cores.values() if c != session.target.primary_core]
        try:
            for core in secondary_cores:
                core.set_reset_catch()
            session.target.reset_and_halt()
        finally:
            for core in secondary_cores:
                core.clear_reset_catch()

        programmer = file_programmer_cls(session, no_reset=True)
        kwargs: dict[str, Any] = {}
        image_format = pyocd_image_format(args.image)
        if image_format is not None:
            kwargs["file_format"] = image_format
        if args.base_address is not None:
            kwargs["base_address"] = args.base_address
        programmer.program(args.image, **kwargs)

        # Match pyOCD load behavior by resetting after a successful flash so
        # the new image starts running immediately.
        session.target.reset()

        info = target_summary(session)
        return emit(
            "ok",
            f"flash succeeded for {Path(args.image).name} and target reset to run.",
            command="flash",
            image=str(Path(args.image).resolve()),
            **info,
        )
    finally:
        close_session(session)


def handle_erase(args: argparse.Namespace) -> int:
    if not (args.chip or args.mass or args.addresses):
        raise RuntimeError(
            "No erase operation specified. Use --chip, --mass, or provide one or more sector addresses."
        )

    connect_helper, _, _ = load_pyocd()
    flash_eraser_cls = load_pyocd_flash_eraser()
    session = choose_session(connect_helper, args)
    try:
        open_session(session)
        target = session.target
        try:
            target.reset_and_halt()
        except Exception:
            pass

        if args.mass:
            mode = flash_eraser_cls.Mode.MASS
            address_specs = None
            mode_name = "mass"
        elif args.chip:
            mode = flash_eraser_cls.Mode.CHIP
            address_specs = None
            mode_name = "chip"
        else:
            mode = flash_eraser_cls.Mode.SECTOR
            address_specs = args.addresses
            mode_name = "sector"

        eraser = flash_eraser_cls(session, mode)
        eraser.erase(address_specs)
        info = target_summary(session)
        return emit(
            "ok",
            f"{mode_name} erase succeeded.",
            command="erase",
            erase_mode=mode_name,
            addresses=address_specs,
            **info,
        )
    finally:
        close_session(session)


def read_memory(target: Any, address: int, width: int, count: int) -> list[int]:
    if width == 8:
        return list(target.read_memory_block8(address, count))
    if width == 16:
        return list(target.read_memory_block16(address, count))
    return list(target.read_memory_block32(address, count))


def write_memory(target: Any, address: int, width: int, values: list[int]) -> None:
    if width == 8:
        target.write_memory_block8(address, values)
    elif width == 16:
        target.write_memory_block16(address, values)
    else:
        target.write_memory_block32(address, values)


def handle_mem_read(args: argparse.Namespace) -> int:
    connect_helper, _, _ = load_pyocd()
    session = choose_session(connect_helper, args)
    try:
        open_session(session)
        return emit_payload(payload_mem_read(session, args))
    finally:
        close_session(session)


def handle_mem_write(args: argparse.Namespace) -> int:
    connect_helper, _, _ = load_pyocd()
    session = choose_session(connect_helper, args)
    try:
        open_session(session)
        return emit_payload(payload_mem_write(session, args))
    finally:
        close_session(session)


def handle_regs(args: argparse.Namespace) -> int:
    connect_helper, _, _ = load_pyocd()
    session = choose_session(connect_helper, args)
    try:
        open_session(session)
        return emit_payload(payload_regs(session, args))
    finally:
        close_session(session)


def handle_step(args: argparse.Namespace) -> int:
    connect_helper, _, _ = load_pyocd()
    session = choose_session(connect_helper, args)
    try:
        open_session(session)
        return emit_payload(payload_step(session, args))
    finally:
        close_session(session)


def handle_breakpoint_set(args: argparse.Namespace) -> int:
    connect_helper, _, target_class = load_pyocd()
    session = choose_session(connect_helper, args)
    try:
        open_session(session)
        return emit_payload(payload_breakpoint_set(session, args, target_class))
    finally:
        close_session(session)


def handle_breakpoint_clear(args: argparse.Namespace) -> int:
    connect_helper, _, _ = load_pyocd()
    session = choose_session(connect_helper, args)
    try:
        open_session(session)
        return emit_payload(payload_breakpoint_clear(session, args))
    finally:
        close_session(session)


def handle_stack(args: argparse.Namespace) -> int:
    connect_helper, _, _ = load_pyocd()
    session = choose_session(connect_helper, args)
    try:
        open_session(session)
        return emit_payload(payload_stack(session, args))
    finally:
        close_session(session)


def decode_fault_registers(raw_values: dict[str, int]) -> dict[str, Any]:
    cfsr = raw_values.get("CFSR") or 0
    mmfsr = cfsr & 0xFF
    bfsr = (cfsr >> 8) & 0xFF
    ufsr = (cfsr >> 16) & 0xFFFF
    return {
        "MMFSR": {
            "value": format_hex(mmfsr, 8),
            "flags": decode_bits(mmfsr, MMFSR_BITS),
        },
        "BFSR": {
            "value": format_hex(bfsr, 8),
            "flags": decode_bits(bfsr, BFSR_BITS),
        },
        "UFSR": {
            "value": format_hex(ufsr, 16),
            "flags": decode_bits(ufsr, UFSR_BITS),
        },
        "HFSR": {
            "value": format_hex(raw_values.get("HFSR") or 0),
            "flags": decode_bits(raw_values.get("HFSR") or 0, HFSR_BITS),
        },
        "DFSR": {
            "value": format_hex(raw_values.get("DFSR") or 0),
            "flags": decode_bits(raw_values.get("DFSR") or 0, DFSR_BITS),
        },
        "MMFAR": format_hex(raw_values.get("MMFAR") or 0),
        "BFAR": format_hex(raw_values.get("BFAR") or 0),
        "AFSR": format_hex(raw_values.get("AFSR") or 0),
        "ICSR": format_hex(raw_values.get("ICSR") or 0),
        "SHCSR": format_hex(raw_values.get("SHCSR") or 0),
    }


def summarize_fault_causes(decoded: dict[str, Any], architecture: str, target_type: str | None) -> list[str]:
    notes: list[str] = []
    arch_upper = architecture.upper()
    if "ARMV6M" in arch_upper:
        notes.append(
            "ARMv6-M targets expose a smaller fault model; MemManage/BusFault/UsageFault details may be limited or aliased."
        )
    if target_type:
        notes.append(f"Target type hint: {target_type}.")

    mmfsr = decoded.get("MMFSR", {}).get("flags", [])
    bfsr = decoded.get("BFSR", {}).get("flags", [])
    ufsr = decoded.get("UFSR", {}).get("flags", [])
    hfsr = decoded.get("HFSR", {}).get("flags", [])
    dfsr = decoded.get("DFSR", {}).get("flags", [])

    if "PRECISERR" in bfsr:
        notes.append("PRECISERR suggests a synchronous data bus fault; inspect the instruction at PC and the accessed address.")
    if "IMPRECISERR" in bfsr:
        notes.append("IMPRECISERR suggests an asynchronous bus fault; inspect recent buffered writes and DMA activity.")
    if "BFARVALID" in bfsr:
        notes.append("BFAR is valid and points to the bus-faulting address.")
    if "IACCVIOL" in mmfsr:
        notes.append("IACCVIOL suggests execution from an invalid or protected address.")
    if "DACCVIOL" in mmfsr:
        notes.append("DACCVIOL suggests an invalid data access; check MPU/protection settings and pointer validity.")
    if "MMARVALID" in mmfsr:
        notes.append("MMFAR is valid and points to the memory-management fault address.")
    if "DIVBYZERO" in ufsr:
        notes.append("DIVBYZERO indicates divide-by-zero trapping is enabled and a zero divisor was used.")
    if "UNALIGNED" in ufsr:
        notes.append("UNALIGNED indicates an unaligned access trap.")
    if "INVSTATE" in ufsr or "INVPC" in ufsr:
        notes.append("INVSTATE/INVPC often points to a corrupted exception return value, bad stack frame, or wrong Thumb bit.")
    if "UNDEFINSTR" in ufsr:
        notes.append("UNDEFINSTR indicates the core tried to execute an invalid instruction.")
    if "FORCED" in hfsr:
        notes.append("HFSR.FORCED means a configurable fault escalated into HardFault.")
    if "VECTTBL" in hfsr:
        notes.append("HFSR.VECTTBL suggests a fault during vector table fetch; verify VTOR and vector table contents.")
    if "BKPT" in dfsr:
        notes.append("DFSR.BKPT indicates execution stopped on a breakpoint instruction or debugger breakpoint.")
    if "HALTED" in dfsr and len(notes) == 0:
        notes.append("DFSR.HALTED indicates the debugger halted the core without a specific fault flag.")
    return notes


def handle_fault(args: argparse.Namespace) -> int:
    connect_helper, _, _ = load_pyocd()
    session = choose_session(connect_helper, args)
    try:
        open_session(session)
        return emit_payload(payload_fault(session))
    finally:
        close_session(session)


def handle_exception_frame(args: argparse.Namespace) -> int:
    connect_helper, _, _ = load_pyocd()
    session = choose_session(connect_helper, args)
    try:
        open_session(session)
        return emit_payload(payload_exception_frame(session, args))
    finally:
        close_session(session)


def handle_vector_table(args: argparse.Namespace) -> int:
    connect_helper, _, _ = load_pyocd()
    session = choose_session(connect_helper, args)
    try:
        open_session(session)
        return emit_payload(payload_vector_table(session, args))
    finally:
        close_session(session)


def handle_debug_open(args: argparse.Namespace) -> int:
    session_id = start_debug_session_server(args)
    metadata = wait_for_debug_session_ready(session_id)
    state = metadata.get("state", "unknown")
    return emit(
        "ok",
        f"Opened debug session {session_id}; target state is {state}.",
        command="debug-open",
        session_id=session_id,
        pid=metadata.get("pid"),
        log_path=metadata.get("log_path"),
        board=metadata.get("board"),
        target_override=metadata.get("target_override"),
        board_target_type=metadata.get("board_target_type"),
        architecture=metadata.get("architecture"),
        state=state,
        registers=metadata.get("registers"),
    )


def handle_debug_session_command(args: argparse.Namespace) -> int:
    metadata = load_session_metadata(args.session_id)
    response = send_session_request(metadata, build_debug_session_request(args))
    return emit_payload(response)


def execute_debug_session_request(
    session,
    request: dict[str, Any],
    target_class: Any,
    target_module: Any | None = None,
) -> dict[str, Any]:
    command = str(request.get("command"))
    args = argparse.Namespace(command=command, **request.get("args", {}))
    if command == "status":
        return payload_attach_like(session, "status")
    if command == "halt":
        return payload_halt_resume(session, "halt")
    if command == "resume":
        return payload_halt_resume(session, "resume")
    if command == "reset":
        return payload_reset(session, args, target_module if target_module is not None else target_class)
    if command == "regs":
        return payload_regs(session, args)
    if command == "mem-read":
        return payload_mem_read(session, args)
    if command == "mem-write":
        return payload_mem_write(session, args)
    if command == "step":
        return payload_step(session, args)
    if command == "breakpoint-set":
        return payload_breakpoint_set(session, args, target_class)
    if command == "breakpoint-clear":
        return payload_breakpoint_clear(session, args)
    if command == "stack":
        return payload_stack(session, args)
    if command == "fault":
        return payload_fault(session)
    if command == "exception-frame":
        return payload_exception_frame(session, args)
    if command == "vector-table":
        return payload_vector_table(session, args)
    if command == "close":
        return payload_debug_close(session)
    raise RuntimeError(f"Unsupported debug-session command: {command}")


def handle_session_server(args: argparse.Namespace) -> int:
    session_id = args.session_id
    metadata = load_session_metadata(session_id)
    connect_helper, _, target_module = load_pyocd()
    target_class = target_module
    session = None
    last_activity = time.time()
    try:
        session = choose_session(connect_helper, args)
        open_session(session)
        info = target_summary(session)
        metadata = update_session_metadata(
            session_id,
            status="ready",
            summary=f"Debug session {session_id} is ready.",
            board=info.get("board"),
            target_override=info.get("target_override"),
            board_target_type=info.get("board_target_type"),
            architecture=info.get("architecture"),
            state=info.get("state"),
            registers=info.get("registers"),
            idle_timeout=args.idle_timeout,
        )

        while True:
            request_paths = sorted(debug_session_requests_dir(session_id).glob("*.json"))
            if not request_paths:
                if (time.time() - last_activity) > args.idle_timeout:
                    update_session_metadata(
                        session_id,
                        status="expired",
                        summary=f"Debug session {session_id} expired after {args.idle_timeout} seconds of inactivity.",
                    )
                    return 0
                time.sleep(DEBUG_SESSION_POLL_SECONDS)
                continue

            for request_path in request_paths:
                request = read_json_file(request_path)
                request_id = str(request.get("request_id") or request_path.stem)
                response_path = debug_session_responses_dir(session_id) / f"{request_id}.json"
                if request.get("token") != metadata.get("token"):
                    response = build_payload(
                        "error",
                        f"Invalid token for debug session {session_id}.",
                        command=request.get("command"),
                    )
                else:
                    try:
                        response = execute_debug_session_request(session, request, target_class, target_module)
                    except RuntimeError as exc:
                        response = build_payload("error", str(exc), command=request.get("command"))
                    except Exception as exc:
                        response = build_payload(
                            "error",
                            f"{request.get('command')} failed: {friendly_exception(exc)}",
                            command=request.get("command"),
                        )
                write_json_atomic(response_path, response)
                try:
                    request_path.unlink()
                except OSError:
                    pass
                last_activity = time.time()
                info = target_summary(session)
                metadata = update_session_metadata(
                    session_id,
                    status="ready",
                    summary=f"Debug session {session_id} is ready.",
                    state=info.get("state"),
                    registers=info.get("registers"),
                    board=info.get("board"),
                    target_override=info.get("target_override"),
                    board_target_type=info.get("board_target_type"),
                    architecture=info.get("architecture"),
                )
                if request.get("command") == "close":
                    update_session_metadata(
                        session_id,
                        status="closed",
                        summary=f"Debug session {session_id} has been closed.",
                    )
                    return 0
    except Exception as exc:
        update_session_metadata(
            session_id,
            status="error",
            summary=f"Debug session {session_id} failed: {friendly_exception(exc)}",
        )
        return 1
    finally:
        if session is not None:
            close_session(session)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Agent-friendly MCU probe wrapper around pyOCD."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(session_parser: argparse.ArgumentParser) -> None:
        session_parser.add_argument("--uid", help="Probe unique ID or unique prefix.")
        session_parser.add_argument("--target", help="Explicit pyOCD target override.")
        session_parser.add_argument(
            "--chip-name",
            help="Known local MCU/chip model string to save into pyocd-targets.yaml when the YAML file is missing.",
        )
        session_parser.add_argument(
            "--target-config",
            help="Optional YAML file describing the local chip model and/or alias mappings for pyOCD target names.",
        )
        session_parser.add_argument(
            "--frequency",
            type=parse_int,
            help="Probe frequency in Hz, e.g. 1000000.",
        )
        session_parser.add_argument(
            "--connect-mode",
            choices=["attach", "under-reset", "halt"],
            default="attach",
            help="Connection strategy for target attach.",
        )
        session_parser.add_argument(
            "--halt-on-connect",
            action="store_true",
            help="Prefer a halted target when attaching.",
        )

    def add_debug_session_ref(session_parser: argparse.ArgumentParser) -> None:
        session_parser.add_argument(
            "--session-id",
            required=True,
            help="Debug session ID returned by debug-open.",
        )

    add_common(subparsers.add_parser("probe", help="List connected probes."))
    add_common(subparsers.add_parser("probe-capabilities", help="List connected probes plus transport/backend capability hints."))
    resolve_parser = subparsers.add_parser("resolve-target", help="Resolve a local chip name or YAML mapping to a pyOCD target.")
    resolve_parser.add_argument("--target", help="Explicit pyOCD target name or target-like local chip string.")
    resolve_parser.add_argument(
        "--chip-name",
        help="Known local MCU/chip model string to save into pyocd-targets.yaml when the YAML file is missing.",
    )
    resolve_parser.add_argument(
        "--target-config",
        help="Optional YAML file describing the local chip model and/or alias mappings for pyOCD target names.",
    )
    debug_open_parser = subparsers.add_parser("debug-open", help="Open a persistent debug session.")
    add_common(debug_open_parser)
    debug_open_parser.add_argument(
        "--idle-timeout",
        type=parse_int,
        default=DEBUG_SESSION_IDLE_TIMEOUT_SECONDS,
        help="Auto-close the debug session after this many idle seconds.",
    )
    add_debug_session_ref(subparsers.add_parser("debug-close", help="Close a persistent debug session."))
    add_debug_session_ref(subparsers.add_parser("debug-status", help="Read target state from a persistent debug session."))
    add_debug_session_ref(subparsers.add_parser("debug-halt", help="Halt the target inside a persistent debug session."))
    add_debug_session_ref(subparsers.add_parser("debug-resume", help="Resume the target inside a persistent debug session."))

    debug_step_parser = subparsers.add_parser("debug-step", help="Single-step one or more instructions inside a persistent debug session.")
    add_debug_session_ref(debug_step_parser)
    debug_step_parser.add_argument(
        "--count",
        type=parse_int,
        default=1,
        help="Number of step operations to perform.",
    )
    debug_step_parser.add_argument(
        "--allow-interrupts",
        action="store_true",
        help="Allow interrupts during step operations.",
    )

    debug_regs_parser = subparsers.add_parser("debug-regs", help="Read core registers inside a persistent debug session.")
    add_debug_session_ref(debug_regs_parser)
    debug_regs_parser.add_argument(
        "--registers",
        nargs="+",
        default=DEFAULT_REGISTERS,
        help="Register names to read.",
    )

    debug_mem_read_parser = subparsers.add_parser("debug-mem-read", help="Read memory values inside a persistent debug session.")
    add_debug_session_ref(debug_mem_read_parser)
    debug_mem_read_parser.add_argument("--address", type=parse_int, required=True, help="Start address.")
    debug_mem_read_parser.add_argument(
        "--width",
        type=int,
        choices=[8, 16, 32],
        default=32,
        help="Element width in bits.",
    )
    debug_mem_read_parser.add_argument(
        "--count",
        type=parse_int,
        required=True,
        help="Number of elements to read.",
    )

    debug_mem_write_parser = subparsers.add_parser("debug-mem-write", help="Write memory values inside a persistent debug session.")
    add_debug_session_ref(debug_mem_write_parser)
    debug_mem_write_parser.add_argument("--address", type=parse_int, required=True, help="Start address.")
    debug_mem_write_parser.add_argument(
        "--width",
        type=int,
        choices=[8, 16, 32],
        default=32,
        help="Element width in bits.",
    )
    debug_mem_write_parser.add_argument(
        "--values",
        type=parse_int,
        nargs="+",
        required=True,
        help="One or more values to write.",
    )
    debug_mem_write_parser.add_argument(
        "--yes",
        action="store_true",
        help="Acknowledge the memory write mutates hardware state.",
    )

    debug_reset_parser = subparsers.add_parser("debug-reset", help="Reset the target inside a persistent debug session.")
    add_debug_session_ref(debug_reset_parser)
    debug_reset_parser.add_argument(
        "--reset-type",
        choices=["sw", "hw", "sw-system", "sw-core", "sw-emulated"],
        help="pyOCD reset strategy override.",
    )
    debug_reset_parser.add_argument(
        "--halt-after-reset",
        action="store_true",
        help="Halt the core after reset.",
    )
    debug_reset_parser.add_argument(
        "--yes",
        action="store_true",
        help="Acknowledge the reset mutates hardware state.",
    )

    debug_bp_set_parser = subparsers.add_parser("debug-breakpoint-set", help="Set a breakpoint inside a persistent debug session.")
    add_debug_session_ref(debug_bp_set_parser)
    debug_bp_set_parser.add_argument("--address", type=parse_int, required=True, help="Breakpoint address.")
    debug_bp_set_parser.add_argument(
        "--breakpoint-type",
        choices=["auto", "hw", "sw"],
        default="auto",
        help="Preferred breakpoint implementation.",
    )

    debug_bp_clear_parser = subparsers.add_parser("debug-breakpoint-clear", help="Remove a breakpoint inside a persistent debug session.")
    add_debug_session_ref(debug_bp_clear_parser)
    debug_bp_clear_parser.add_argument("--address", type=parse_int, required=True, help="Breakpoint address.")

    debug_stack_parser = subparsers.add_parser("debug-stack", help="Read stack memory inside a persistent debug session.")
    add_debug_session_ref(debug_stack_parser)
    debug_stack_parser.add_argument("--address", type=parse_int, help="Optional stack base address override.")
    debug_stack_parser.add_argument(
        "--words",
        type=parse_int,
        default=8,
        help="Number of 32-bit words to read.",
    )

    debug_fault_parser = subparsers.add_parser("debug-fault", help="Read and decode fault registers inside a persistent debug session.")
    add_debug_session_ref(debug_fault_parser)

    debug_exception_parser = subparsers.add_parser("debug-exception-frame", help="Decode an exception stack frame inside a persistent debug session.")
    add_debug_session_ref(debug_exception_parser)
    debug_exception_parser.add_argument("--address", type=parse_int, help="Optional frame base address override.")

    debug_vector_parser = subparsers.add_parser("debug-vector-table", help="Read vector table entries inside a persistent debug session.")
    add_debug_session_ref(debug_vector_parser)
    debug_vector_parser.add_argument("--base-address", type=parse_int, help="Optional vector table base override.")
    debug_vector_parser.add_argument(
        "--count",
        type=parse_int,
        default=16,
        help="Number of vector entries to read.",
    )

    add_common(subparsers.add_parser("attach", help="Attach and summarize target state."))
    add_common(subparsers.add_parser("status", help="Attach and summarize target state."))
    add_common(subparsers.add_parser("halt", help="Halt the core and report state."))
    add_common(subparsers.add_parser("resume", help="Resume the core and report state."))

    step_parser = subparsers.add_parser("step", help="Single-step one or more instructions.")
    add_common(step_parser)
    step_parser.add_argument(
        "--count",
        type=parse_int,
        default=1,
        help="Number of step operations to perform.",
    )
    step_parser.add_argument(
        "--allow-interrupts",
        action="store_true",
        help="Allow interrupts during step operations.",
    )

    reset_parser = subparsers.add_parser("reset", help="Reset the target.")
    add_common(reset_parser)
    reset_parser.add_argument(
        "--reset-type",
        choices=["sw", "hw", "sw-system", "sw-core", "sw-emulated"],
        help="pyOCD reset strategy override.",
    )
    reset_parser.add_argument(
        "--halt-after-reset",
        action="store_true",
        help="Halt the core after reset.",
    )
    reset_parser.add_argument(
        "--yes",
        action="store_true",
        help="Acknowledge the reset mutates hardware state.",
    )

    flash_parser = subparsers.add_parser("flash", help="Program an image file.")
    add_common(flash_parser)
    flash_parser.add_argument(
        "--image",
        required=True,
        help="ELF (.elf/.axf/.out), HEX, or BIN image path.",
    )
    flash_parser.add_argument(
        "--base-address",
        type=parse_int,
        help="Base address for raw BIN images.",
    )
    flash_parser.add_argument(
        "--yes",
        action="store_true",
        help="Acknowledge the flash operation mutates hardware state.",
    )

    erase_parser = subparsers.add_parser("erase", help="Erase target flash.")
    add_common(erase_parser)
    erase_parser.add_argument("--chip", action="store_true", help="Perform a chip erase.")
    erase_parser.add_argument("--mass", action="store_true", help="Perform a mass erase when the target supports it.")
    erase_parser.add_argument(
        "addresses",
        nargs="*",
        help="Optional sector addresses or ranges like 0x1000, 0x1000-0x1FFF, or 0x1000+0x800.",
    )
    erase_parser.add_argument(
        "--yes",
        action="store_true",
        help="Acknowledge the erase operation mutates hardware state.",
    )

    regs_parser = subparsers.add_parser("regs", help="Read core registers.")
    add_common(regs_parser)
    regs_parser.add_argument(
        "--registers",
        nargs="+",
        default=DEFAULT_REGISTERS,
        help="Register names to read.",
    )

    ex_frame_parser = subparsers.add_parser("exception-frame", help="Decode a standard Cortex-M exception stack frame.")
    add_common(ex_frame_parser)
    ex_frame_parser.add_argument("--address", type=parse_int, help="Optional frame base address override.")

    mem_read_parser = subparsers.add_parser("mem-read", help="Read memory values.")
    add_common(mem_read_parser)
    mem_read_parser.add_argument("--address", type=parse_int, required=True, help="Start address.")
    mem_read_parser.add_argument(
        "--width",
        type=int,
        choices=[8, 16, 32],
        default=32,
        help="Element width in bits.",
    )
    mem_read_parser.add_argument(
        "--count",
        type=parse_int,
        required=True,
        help="Number of elements to read.",
    )

    mem_write_parser = subparsers.add_parser("mem-write", help="Write memory values.")
    add_common(mem_write_parser)
    mem_write_parser.add_argument("--address", type=parse_int, required=True, help="Start address.")
    mem_write_parser.add_argument(
        "--width",
        type=int,
        choices=[8, 16, 32],
        default=32,
        help="Element width in bits.",
    )
    mem_write_parser.add_argument(
        "--values",
        type=parse_int,
        nargs="+",
        required=True,
        help="One or more values to write.",
    )
    mem_write_parser.add_argument(
        "--yes",
        action="store_true",
        help="Acknowledge the memory write mutates hardware state.",
    )

    bp_set_parser = subparsers.add_parser("breakpoint-set", help="Set a breakpoint.")
    add_common(bp_set_parser)
    bp_set_parser.add_argument("--address", type=parse_int, required=True, help="Breakpoint address.")
    bp_set_parser.add_argument(
        "--breakpoint-type",
        choices=["auto", "hw", "sw"],
        default="auto",
        help="Preferred breakpoint implementation.",
    )

    bp_clear_parser = subparsers.add_parser("breakpoint-clear", help="Remove a breakpoint.")
    add_common(bp_clear_parser)
    bp_clear_parser.add_argument("--address", type=parse_int, required=True, help="Breakpoint address.")

    stack_parser = subparsers.add_parser("stack", help="Read stack memory from SP or an explicit address.")
    add_common(stack_parser)
    stack_parser.add_argument("--address", type=parse_int, help="Optional stack base address override.")
    stack_parser.add_argument(
        "--words",
        type=parse_int,
        default=8,
        help="Number of 32-bit words to read.",
    )

    fault_parser = subparsers.add_parser("fault", help="Read and decode Cortex-M fault registers.")
    add_common(fault_parser)

    vector_parser = subparsers.add_parser("vector-table", help="Read and label vector table entries.")
    add_common(vector_parser)
    vector_parser.add_argument("--base-address", type=parse_int, help="Optional vector table base override.")
    vector_parser.add_argument(
        "--count",
        type=parse_int,
        default=16,
        help="Number of vector entries to read.",
    )

    session_server_parser = subparsers.add_parser("_session-server", help=argparse.SUPPRESS)
    add_common(session_server_parser)
    session_server_parser.add_argument("--session-id", required=True, help=argparse.SUPPRESS)
    session_server_parser.add_argument("--token", required=True, help=argparse.SUPPRESS)
    session_server_parser.add_argument(
        "--idle-timeout",
        type=parse_int,
        default=DEBUG_SESSION_IDLE_TIMEOUT_SECONDS,
        help=argparse.SUPPRESS,
    )
    session_server_parser.set_defaults(persistent_session=True)

    return parser


def dispatch(args: argparse.Namespace) -> int:
    if args.command == "_session-server":
        return handle_session_server(args)

    confirmation = require_confirmation(args)
    if confirmation is not None:
        return confirmation

    try:
        apply_target_resolution(args)
        if args.command == "debug-open":
            return handle_debug_open(args)
        if is_debug_session_command(args.command):
            return handle_debug_session_command(args)
        if args.command == "probe":
            return handle_probe(args)
        if args.command == "probe-capabilities":
            return handle_probe_capabilities(args)
        if args.command == "resolve-target":
            return handle_resolve_target(args)
        if args.command == "attach":
            return handle_attach_like(args, "attach")
        if args.command == "status":
            return handle_attach_like(args, "status")
        if args.command == "halt":
            return handle_halt_resume(args, "halt")
        if args.command == "resume":
            return handle_halt_resume(args, "resume")
        if args.command == "reset":
            return handle_reset(args)
        if args.command == "flash":
            return handle_flash(args)
        if args.command == "erase":
            return handle_erase(args)
        if args.command == "regs":
            return handle_regs(args)
        if args.command == "exception-frame":
            return handle_exception_frame(args)
        if args.command == "step":
            return handle_step(args)
        if args.command == "mem-read":
            return handle_mem_read(args)
        if args.command == "mem-write":
            return handle_mem_write(args)
        if args.command == "breakpoint-set":
            return handle_breakpoint_set(args)
        if args.command == "breakpoint-clear":
            return handle_breakpoint_clear(args)
        if args.command == "stack":
            return handle_stack(args)
        if args.command == "fault":
            return handle_fault(args)
        if args.command == "vector-table":
            return handle_vector_table(args)
    except RuntimeError as exc:
        return emit("error", str(exc), command=args.command)
    except Exception as exc:
        return emit(
            "error",
            f"{args.command} failed: {friendly_exception(exc)}",
            command=args.command,
        )

    return emit("error", f"Unsupported command {args.command}.", command=args.command)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return dispatch(args)


if __name__ == "__main__":
    sys.exit(main())
