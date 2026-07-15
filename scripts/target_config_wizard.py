"""Local browser wizard for creating pyocd-targets.yaml."""

from __future__ import annotations

import html
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse


SaveConfigFn = Callable[[str, str | None, str | None, str | None, str | None, bool, bool], dict[str, Any]]
SearchCatalogFn = Callable[[str, str | None, int], list[dict[str, Any]]]


def friendly_exception(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def target_config_wizard_html(config_path: Path) -> str:
    escaped_path = html.escape(str(config_path))
    config_path_json = json.dumps(str(config_path))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>pyOCD Target Config</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bench: #f5f7f2;
      --panel: #fffef9;
      --ink: #15211d;
      --muted: #5f6d67;
      --line: #d9dfd4;
      --line-strong: #b8c4b7;
      --solder: #087f62;
      --solder-deep: #075c49;
      --copper: #b76635;
      --signal: #2f68a8;
      --danger: #b42318;
      --warning: #8a4b0f;
      --soft-green: #e6f3ed;
      --soft-copper: #f7eadf;
      --mono: "Cascadia Mono", "Consolas", monospace;
      font-family: "Segoe UI", Arial, sans-serif;
      background: #f5f7f2;
      color: var(--ink);
    }}
    body {{
      margin: 0;
      height: 100vh;
      padding: 20px 24px 24px;
      box-sizing: border-box;
      overflow: hidden;
      background:
        linear-gradient(90deg, rgba(8, 127, 98, 0.06) 1px, transparent 1px),
        linear-gradient(180deg, rgba(183, 102, 53, 0.05) 1px, transparent 1px),
        var(--bench);
      background-size: 44px 44px;
    }}
    main {{
      width: min(1120px, 100%);
      height: 100%;
      margin: 0 auto;
      display: grid;
      grid-template-columns: minmax(340px, 420px) minmax(0, 1fr);
      grid-template-rows: auto minmax(0, 1fr);
      gap: 16px;
      align-items: stretch;
      min-height: 0;
    }}
    header {{
      grid-column: 1 / -1;
      position: relative;
      padding: 0 0 12px 22px;
      border-bottom: 1px solid var(--line);
    }}
    header::before {{
      content: "";
      position: absolute;
      left: 0;
      top: 4px;
      bottom: 12px;
      width: 4px;
      border-radius: 999px;
      background: linear-gradient(var(--solder), var(--copper));
    }}
    h1 {{
      margin: 0 0 6px;
      font-family: "Bahnschrift", "Segoe UI", Arial, sans-serif;
      font-size: 26px;
      line-height: 1.08;
      letter-spacing: 0;
      color: var(--ink);
    }}
    p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.45;
    }}
    .intro {{
      margin: 6px 0 0;
      max-width: 820px;
    }}
    section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      box-sizing: border-box;
      box-shadow: 0 18px 45px rgba(41, 52, 46, 0.08);
      min-height: 0;
    }}
    .matches-panel {{
      display: flex;
      flex-direction: column;
      min-height: 0;
    }}
    h2 {{
      display: flex;
      align-items: center;
      gap: 9px;
      margin: 0 0 12px;
      font-family: "Bahnschrift", "Segoe UI", Arial, sans-serif;
      font-size: 16px;
      line-height: 1.25;
      letter-spacing: 0;
      color: var(--ink);
    }}
    h2::before {{
      content: "";
      width: 10px;
      height: 10px;
      border: 2px solid var(--solder);
      border-radius: 3px;
      box-sizing: border-box;
      background: var(--panel);
    }}
    .path {{
      display: block;
      margin-top: 6px;
      padding: 7px 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: rgba(255, 255, 255, 0.72);
      color: #26332f;
      overflow-wrap: anywhere;
      font-family: var(--mono);
      font-size: 13px;
    }}
    .source-status {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 8px;
    }}
    .status-chip {{
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 0 10px;
      border-radius: 999px;
      background: var(--soft-green);
      color: var(--solder-deep);
      font-size: 12px;
      font-weight: 700;
      border: 1px solid rgba(8, 127, 98, 0.16);
    }}
    button.status-chip {{
      border: 0;
      cursor: pointer;
      font: inherit;
      font-size: 12px;
      font-weight: 700;
    }}
    .status-chip.warn {{
      background: var(--soft-copper);
      color: var(--warning);
      border-color: rgba(183, 102, 53, 0.22);
    }}
    .guide {{
      display: grid;
      gap: 6px;
      margin: 0 0 12px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-left: 4px solid var(--copper);
      border-radius: 7px;
      background: #fbfaf4;
      color: #3f4d47;
      font-size: 13px;
      line-height: 1.35;
    }}
    .guide-title {{
      font-weight: 700;
      color: var(--ink);
    }}
    .guide ol {{
      margin: 0;
      padding-left: 20px;
    }}
    .guide li + li {{
      margin-top: 2px;
    }}
    .mapping-panel {{
      height: 100%;
      max-height: 100%;
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }}
    #form {{
      display: flex;
      flex-direction: column;
      min-height: 0;
      flex: 1;
    }}
    .form-scroll {{
      min-height: 0;
      overflow: auto;
      padding-right: 2px;
    }}
    label {{
      display: grid;
      gap: 6px;
      margin: 10px 0;
      font-size: 14px;
      font-weight: 700;
      color: #26332f;
    }}
    input {{
      width: 100%;
      box-sizing: border-box;
      border: 1px solid var(--line-strong);
      border-radius: 6px;
      padding: 9px 11px;
      font: inherit;
      font-weight: 400;
      color: var(--ink);
      background: #fffdf8;
    }}
    input:focus {{
      outline: 3px solid rgba(8, 127, 98, 0.18);
      outline-offset: 1px;
      border-color: var(--solder);
    }}
    input[type="checkbox"] {{
      width: auto;
      margin: 0;
    }}
    .install-option {{
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      gap: 9px;
      align-items: center;
      margin-top: 2px;
      font-weight: 700;
    }}
    .install-option input:disabled + span {{
      color: #79847f;
    }}
    .field-help {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 400;
      line-height: 1.4;
    }}
    .install-option .field-help {{
      grid-column: 2;
    }}
    .actions {{
      display: flex;
      gap: 12px;
      align-items: center;
      margin: 12px -18px -18px;
      padding: 12px 18px;
      border-top: 1px solid var(--line);
      background: linear-gradient(rgba(255, 254, 249, 0), var(--panel) 18px);
      flex-wrap: wrap;
      flex-shrink: 0;
      z-index: 2;
    }}
    button {{
      border: 0;
      border-radius: 6px;
      background: var(--solder);
      color: #ffffff;
      padding: 10px 14px;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
      box-shadow: inset 0 -2px 0 rgba(0, 0, 0, 0.14);
    }}
    button:not(.status-chip):hover {{
      background: var(--solder-deep);
    }}
    button:disabled {{
      cursor: wait;
      opacity: 0.72;
    }}
    .result-list {{
      display: grid;
      gap: 8px;
      min-height: 124px;
      max-height: none;
      overflow: auto;
      padding-right: 2px;
      flex: 1;
    }}
    .result {{
      width: 100%;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      align-items: center;
      padding: 12px 12px 12px 14px;
      border: 1px solid var(--line);
      border-left: 4px solid transparent;
      border-radius: 7px;
      background: #fffdf8;
      color: inherit;
      text-align: left;
      box-shadow: none;
    }}
    .result:hover {{
      border-color: rgba(8, 127, 98, 0.28);
      border-left-color: var(--solder);
      background: #f9fcf7;
    }}
    .result.selected {{
      border-color: rgba(183, 102, 53, 0.42);
      border-left-color: var(--copper);
      background: #fff7ef;
    }}
    .result-pack .pill {{
      background: var(--soft-green);
      color: var(--solder-deep);
      border-color: rgba(8, 127, 98, 0.18);
    }}
    .result strong {{
      display: block;
      font-size: 15px;
      line-height: 1.3;
      color: var(--ink);
      overflow-wrap: anywhere;
      font-family: var(--mono);
      font-weight: 700;
    }}
    .meta {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 5px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 400;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      padding: 0 8px;
      border-radius: 999px;
      background: var(--soft-copper);
      color: #7b411f;
      font-size: 12px;
      font-weight: 700;
      border: 1px solid rgba(183, 102, 53, 0.18);
    }}
    .empty {{
      display: grid;
      place-items: center;
      gap: 6px;
      min-height: 124px;
      border: 1px dashed var(--line-strong);
      border-radius: 7px;
      color: var(--muted);
      text-align: center;
      padding: 16px;
      box-sizing: border-box;
      background: rgba(255, 255, 255, 0.46);
    }}
    .empty strong {{
      color: #34433e;
      font-size: 14px;
    }}
    .empty span {{
      display: block;
      max-width: 520px;
      overflow-wrap: anywhere;
      line-height: 1.45;
    }}
    #status {{
      min-height: 24px;
      line-height: 1.4;
      color: var(--muted);
    }}
    #status.error, #matchStatus.error {{
      color: var(--danger);
    }}
    #status.ok {{
      color: var(--solder-deep);
    }}
    #matchStatus {{
      margin: 0 0 12px;
      min-height: 20px;
      color: var(--muted);
      font-size: 14px;
    }}
    .spinner {{
      display: inline-block;
      width: 14px;
      height: 14px;
      margin-right: 8px;
      border: 2px solid var(--line);
      border-top-color: var(--solder);
      border-radius: 50%;
      vertical-align: -2px;
      animation: spin 0.8s linear infinite;
    }}
    @keyframes spin {{
      to {{
        transform: rotate(360deg);
      }}
    }}
    @media (max-width: 820px) {{
      body {{
        height: auto;
        min-height: 100vh;
        overflow: auto;
        padding: 16px;
      }}
      main {{
        height: auto;
        grid-template-columns: 1fr;
        grid-template-rows: auto;
      }}
      .mapping-panel {{
        position: static;
        height: auto;
        max-height: none;
        overflow: visible;
      }}
      #form {{
        display: block;
      }}
      .form-scroll {{
        overflow: visible;
        padding-right: 0;
      }}
      .matches-panel {{
        display: block;
      }}
    }}
    @media (max-height: 760px) and (min-width: 821px) {{
      body {{
        padding-top: 14px;
      }}
      header {{
        padding-bottom: 8px;
      }}
      .intro {{
        display: none;
      }}
      .source-status {{
        margin-top: 6px;
      }}
      .guide {{
        grid-template-columns: auto minmax(0, 1fr);
        align-items: start;
      }}
      .guide ol {{
        display: flex;
        gap: 12px;
        flex-wrap: wrap;
        padding-left: 18px;
      }}
      .guide li + li {{
        margin-top: 0;
      }}
      .field-help {{
        display: none;
      }}
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bench: #111812;
        --panel: #17211c;
        --ink: #edf2ed;
        --muted: #a9b8af;
        --line: #334139;
        --line-strong: #46564e;
        --soft-green: #102d25;
        --soft-copper: #352115;
        background: #111812;
        color: var(--ink);
      }}
      section {{
        background: var(--panel);
        border-color: var(--line);
      }}
      p, #status, #matchStatus, .meta, .empty, .field-help {{
        color: #a9b6c7;
      }}
      h2, label, .result strong, .empty strong {{
        color: #edf2f7;
      }}
      input, .path, .result {{
        background: #101711;
        border-color: var(--line);
        color: var(--ink);
      }}
      .guide {{
        background: #101711;
        border-color: var(--line);
        border-left-color: var(--copper);
        color: #c9d4e5;
      }}
      .guide-title {{
        color: #edf2f7;
      }}
      .result:hover {{
        background: #16241d;
      }}
      .result.selected {{
        background: #241a13;
        border-color: rgba(183, 102, 53, 0.5);
        border-left-color: var(--copper);
      }}
      .pill {{
        background: var(--soft-copper);
        color: #f0c6a6;
      }}
      .result-pack .pill {{
        background: var(--soft-green);
        color: #8be0c5;
      }}
      .status-chip {{
        background: #263244;
        color: #c9d4e5;
      }}
      .status-chip.warn {{
        background: #3b2a17;
        color: #f6c779;
      }}
      .spinner {{
        border-color: var(--line);
        border-top-color: #60a5fa;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>pyOCD Target Config</h1>
      <p class="intro">Create the target mapping needed before this command can continue.</p>
      <p>Output file<span class="path">{escaped_path}</span></p>
      <div id="sourceStatus" class="source-status" aria-live="polite"></div>
    </header>
    <section class="mapping-panel">
      <h2>Target Mapping</h2>
      <form id="form">
        <div class="form-scroll">
          <div class="guide" role="note" aria-label="Setup steps">
            <div class="guide-title">Typical flow</div>
            <ol>
              <li>Type the chip marking from the board or schematic.</li>
              <li>Select the closest supported match from the list.</li>
              <li>Save the mapping so the original command can continue.</li>
            </ol>
          </div>
          <label>
            Chip name
            <input id="chipName" name="chip_name" required autocomplete="off" placeholder="LPC55S69JBD100">
            <span class="field-help">Use the full package or part marking when possible. Broad family names such as MCX also work for search.</span>
          </label>
          <label>
            Vendor
            <input id="vendor" name="vendor" autocomplete="off" placeholder="NXP">
            <span class="field-help">Optional. Leave blank unless the same part number appears under multiple vendors.</span>
          </label>
          <label>
            pyOCD target
            <input id="pyocdTarget" name="pyocd_target" autocomplete="off" placeholder="lpc55s69">
            <span class="field-help">Usually filled by selecting a match. Enter it manually only if you already know the pyOCD target name.</span>
          </label>
          <label class="install-option">
            <input id="installPack" name="install_pack" type="checkbox" checked disabled>
            <span>Install required CMSIS-Pack</span>
            <span class="field-help">Enabled when the selected remote match needs a pack that is not installed yet.</span>
          </label>
        </div>
        <div class="actions">
          <button id="submit" type="submit">Resolve and Save</button>
          <span id="status" role="status" aria-live="polite"></span>
        </div>
      </form>
    </section>
    <section class="matches-panel">
      <h2>Supported Matches</h2>
      <p id="matchStatus" role="status" aria-live="polite">Start typing a chip name.</p>
      <div id="matches" class="result-list"></div>
    </section>
  </main>
  <script>
    const form = document.getElementById("form");
    const submit = document.getElementById("submit");
    const status = document.getElementById("status");
    const matchStatus = document.getElementById("matchStatus");
    const matches = document.getElementById("matches");
    const chipName = document.getElementById("chipName");
    const vendor = document.getElementById("vendor");
    const pyocdTarget = document.getElementById("pyocdTarget");
    const installPack = document.getElementById("installPack");
    const sourceStatus = document.getElementById("sourceStatus");
    const configPath = {config_path_json};
    const searchLimit = 0;
    let searchTimer = 0;
    let searchSerial = 0;
    let selectedMatch = null;
    let wizardComplete = false;

    function setStatus(kind, text, busy = false) {{
      status.className = kind;
      status.innerHTML = "";
      status.setAttribute("aria-busy", busy ? "true" : "false");
      if (busy) {{
        const spinner = document.createElement("span");
        spinner.className = "spinner";
        spinner.setAttribute("aria-hidden", "true");
        status.appendChild(spinner);
      }}
      status.appendChild(document.createTextNode(text));
    }}

    function setMatchStatus(kind, text, busy = false) {{
      matchStatus.className = kind;
      matchStatus.innerHTML = "";
      matchStatus.setAttribute("aria-busy", busy ? "true" : "false");
      if (busy) {{
        const spinner = document.createElement("span");
        spinner.className = "spinner";
        spinner.setAttribute("aria-hidden", "true");
        matchStatus.appendChild(spinner);
      }}
      matchStatus.appendChild(document.createTextNode(text));
    }}

    function renderEmpty(text, detail = "") {{
      matches.innerHTML = "";
      const empty = document.createElement("div");
      empty.className = "empty";
      const title = document.createElement("strong");
      title.textContent = text;
      empty.appendChild(title);
      if (detail) {{
        const detailNode = document.createElement("span");
        detailNode.textContent = detail;
        empty.appendChild(detailNode);
      }}
      matches.appendChild(empty);
    }}

    function friendlyFetchMessage(error, fallback) {{
      const message = error && error.message ? error.message : fallback;
      if (message === "Failed to fetch" || message.includes("NetworkError")) {{
        return wizardComplete
          ? "This wizard has finished and its local server has closed."
          : "The local wizard server is not reachable. Re-run the command to open a new wizard.";
      }}
      return message;
    }}

    function completeWizard(summary, detail) {{
      wizardComplete = true;
      window.clearTimeout(searchTimer);
      chipName.disabled = true;
      vendor.disabled = true;
      pyocdTarget.disabled = true;
      installPack.disabled = true;
      submit.disabled = true;
      submit.textContent = "Done";
      setMatchStatus("ok", "Wizard complete. The original command is continuing.");
      renderEmpty(summary, detail || "Open a new wizard if you need to create another target mapping.");
    }}

    function isRemoteIndexUpdating(payload) {{
      return (payload.catalog_status || {{}}).state === "updating";
    }}

    function selectedMatchCanInstall() {{
      if (!selectedMatch) return false;
      if ((selectedMatch.target || "") !== pyocdTarget.value.trim()) return false;
      return selectedMatch.source === "pack" && Boolean(selectedMatch.pack) && !selectedMatch.installed;
    }}

    function updateInstallOption() {{
      const canInstall = selectedMatchCanInstall();
      installPack.disabled = !canInstall;
      if (canInstall) {{
        installPack.checked = true;
      }}
    }}

    function selectedPackPayload() {{
      if (!selectedMatchCanInstall()) {{
        return {{}};
      }}
      return {{
        selected_pack: selectedMatch.pack || "",
        selected_part_number: selectedMatch.part_number || selectedMatch.target || "",
        selected_pack_installed: Boolean(selectedMatch.installed),
      }};
    }}

    function renderMatches(items, payload) {{
      matches.innerHTML = "";
      if (!items.length) {{
        renderEmpty(
          isRemoteIndexUpdating(payload)
            ? "Updating remote CMSIS-Pack index. Matches will appear when it finishes."
            : "No supported target matched the current input."
        );
        return;
      }}
      for (const item of items) {{
        const button = document.createElement("button");
        button.type = "button";
        button.className = item.source === "pack" ? "result result-pack" : "result";
        button.addEventListener("click", () => {{
          matches.querySelectorAll(".result.selected").forEach((node) => node.classList.remove("selected"));
          button.classList.add("selected");
          selectedMatch = item;
          pyocdTarget.value = item.target || "";
          if (!vendor.value && item.vendor) {{
            vendor.value = item.vendor;
          }}
          updateInstallOption();
          setMatchStatus("", "Selected " + item.target + ". Review the pack option, then save.");
        }});

        const body = document.createElement("span");
        const title = document.createElement("strong");
        title.textContent = item.part_number || item.target || "Unknown";
        body.appendChild(title);

        const meta = document.createElement("span");
        meta.className = "meta";
        for (const value of [item.target, item.vendor, item.source, item.match, item.pack]) {{
          if (!value) continue;
          const span = document.createElement("span");
          span.textContent = value;
          meta.appendChild(span);
        }}
        body.appendChild(meta);

        const pill = document.createElement("span");
        pill.className = "pill";
        pill.textContent = item.source === "pack" && !item.installed ? "Pack" : "Use";
        button.appendChild(body);
        button.appendChild(pill);
        matches.appendChild(button);
      }}
    }}

    function renderSourceStatus(payload) {{
      sourceStatus.innerHTML = "";
      const status = payload.catalog_status || {{}};
      const chips = [];
      if (Number.isFinite(status.local_target_count)) {{
        chips.push({{text: status.local_target_count + " local pyOCD targets", warn: status.local_target_count === 0}});
      }}
      if (status.pack_index_search) {{
        chips.push({{text: "Remote CMSIS-Pack index search enabled", warn: false}});
      }}
      if (status.pack_index_detail) {{
        chips.push({{text: status.pack_index_detail, warn: status.state === "error"}});
      }}
      if (status.warning) {{
        chips.push({{text: status.warning, warn: true}});
      }}
      for (const chip of chips) {{
        const span = document.createElement("span");
        span.className = chip.warn ? "status-chip warn" : "status-chip";
        span.textContent = chip.text;
        sourceStatus.appendChild(span);
      }}
      if (status.state === "error") {{
        const retry = document.createElement("button");
        retry.type = "button";
        retry.className = "status-chip warn";
        retry.textContent = "Retry Remote";
        retry.addEventListener("click", retryRemoteIndex);
        sourceStatus.appendChild(retry);
      }}
    }}

    async function retryRemoteIndex() {{
      if (wizardComplete) return;
      setMatchStatus("", "Retrying remote index...", true);
      try {{
        const response = await fetch("/api/pack-index/retry", {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          body: "{{}}",
        }});
        const payload = await response.json();
        if (!response.ok || payload.status !== "ok") {{
          throw new Error(payload.error || "Remote retry failed.");
        }}
        renderSourceStatus(payload);
        await searchMatches();
      }} catch (error) {{
        const message = friendlyFetchMessage(error, "Remote retry failed.");
        setMatchStatus("error", message);
        renderEmpty("Remote index retry failed.", message);
      }}
    }}

    async function searchMatches() {{
      if (wizardComplete) return;
      const query = chipName.value.trim();
      const vendorText = vendor.value.trim();
      const serial = ++searchSerial;
      if (!query) {{
        setMatchStatus("", "Start typing a chip name.");
        renderEmpty("Type a chip name such as LPC55S69JBD100 or MCXA153 to search local and remote pyOCD support.");
        return;
      }}
      setMatchStatus("", "Searching supported targets...", true);
      try {{
        const params = new URLSearchParams({{q: query, vendor: vendorText, limit: String(searchLimit)}});
        const response = await fetch("/api/search?" + params.toString(), {{cache: "no-store"}});
        const payload = await response.json();
        if (serial !== searchSerial) return;
        if (!response.ok || payload.status !== "ok") {{
          throw new Error(payload.error || "Search failed.");
        }}
        renderSourceStatus(payload);
        const count = payload.matches.length;
        const updating = isRemoteIndexUpdating(payload);
        const statusText = count
          ? "Showing all " + count + " supported match" + (count === 1 ? "" : "es") + "."
          : updating
            ? "Updating remote CMSIS-Pack index..."
            : "No supported matches. Try the full chip marking or clear the vendor field.";
        setMatchStatus("", statusText, updating);
        renderMatches(payload.matches, payload);
        if (!count && updating && serial === searchSerial) {{
          window.setTimeout(() => {{
            if (serial === searchSerial) searchMatches();
          }}, 2500);
        }}
      }} catch (error) {{
        if (serial !== searchSerial) return;
        const message = friendlyFetchMessage(error, "Search failed.");
        setMatchStatus("error", message);
        renderEmpty("Search failed.", message);
      }}
    }}

    function queueSearch() {{
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(searchMatches, 180);
    }}

    chipName.addEventListener("input", () => {{
      if (wizardComplete) return;
      selectedMatch = null;
      updateInstallOption();
      queueSearch();
    }});
    vendor.addEventListener("input", () => {{
      if (wizardComplete) return;
      selectedMatch = null;
      updateInstallOption();
      queueSearch();
    }});
    pyocdTarget.addEventListener("input", () => {{
      if (wizardComplete) return;
      selectedMatch = null;
      updateInstallOption();
    }});
    searchMatches();

    form.addEventListener("submit", async (event) => {{
      event.preventDefault();
      const originalSubmitText = submit.textContent;
      submit.disabled = true;
      submit.textContent = installPack.checked && selectedMatchCanInstall() ? "Installing..." : "Saving...";
      setStatus(
        "",
        installPack.checked && selectedMatchCanInstall()
          ? "Resolving target and installing the selected CMSIS-Pack..."
          : "Resolving target and writing pyocd-targets.yaml...",
        true
      );
      const data = {{
        ...Object.fromEntries(new FormData(form).entries()),
        ...selectedPackPayload(),
        install_pack: installPack.checked && selectedMatchCanInstall(),
      }};
      try {{
        const response = await fetch("/api/save", {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify(data),
        }});
        const payload = await response.json();
        if (!response.ok || payload.status !== "ok") {{
          throw new Error(payload.error || "Unable to save " + configPath);
        }}
        const packInstall = payload.pack_install || {{}};
        if (packInstall.attempted && packInstall.success === false) {{
          setStatus("error", "Saved " + payload.target + ". Pack install failed.");
          completeWizard(
            "Configuration saved, but pack installation failed.",
            "The YAML file was written. Re-run the command to open a new wizard if you need to retry the pack install."
          );
          return;
        }}
        if (packInstall.attempted) {{
          setStatus("ok", "Saved " + payload.target + " and installed pack. You can close this tab.");
          completeWizard(
            "Configuration saved and CMSIS-Pack installed.",
            "The local wizard server has closed because the original command can continue."
          );
        }} else {{
          setStatus("ok", "Saved " + payload.target + ". You can close this tab.");
          completeWizard(
            "Configuration saved.",
            "The local wizard server has closed because the original command can continue."
          );
        }}
      }} catch (error) {{
        const message = friendlyFetchMessage(error, "Unable to save " + configPath);
        setStatus("error", message);
        submit.textContent = originalSubmitText;
        submit.disabled = false;
      }}
    }});
  </script>
</body>
</html>
"""


def run_target_config_wizard(
    cwd: Path,
    catalog: list[dict[str, Any]],
    config_filename: str,
    save_config_fn: SaveConfigFn,
    search_catalog_fn: SearchCatalogFn,
    output_fn=print,
    timeout_seconds: float = 300.0,
    open_browser_fn: Callable[[str], bool] | None = None,
    catalog_status: dict[str, Any] | None = None,
    catalog_status_fn: Callable[[], dict[str, Any]] | None = None,
    retry_pack_index_fn: Callable[[], dict[str, Any]] | None = None,
) -> Path | None:
    config_path = cwd / config_filename
    done = threading.Event()
    state: dict[str, Any] = {}
    open_browser = open_browser_fn or (lambda url: webbrowser.open(url, new=2))
    status_payload = {
        "local_target_count": len(catalog),
        "pack_index_search": True,
        **(catalog_status or {}),
    }

    class TargetConfigWizardHandler(BaseHTTPRequestHandler):
        server_version = "TargetConfigWizard/2.0"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def send_bytes(self, status_code: int, content_type: str, payload: bytes) -> None:
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def send_json(self, status_code: int, payload: dict[str, Any]) -> None:
            self.send_bytes(
                status_code,
                "application/json; charset=utf-8",
                json.dumps(payload, sort_keys=True).encode("utf-8"),
            )

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/favicon.ico":
                self.send_bytes(204, "text/plain; charset=utf-8", b"")
                return
            if parsed.path in {"", "/"}:
                self.send_bytes(
                    200,
                    "text/html; charset=utf-8",
                    target_config_wizard_html(config_path).encode("utf-8"),
                )
                return
            if parsed.path == "/api/search":
                params = parse_qs(parsed.query)
                query = params.get("q", [""])[0]
                vendor = params.get("vendor", [""])[0].strip() or None
                try:
                    limit = int(params.get("limit", ["0"])[0])
                except ValueError:
                    limit = 0
                try:
                    matches = search_catalog_fn(query, vendor, limit)
                except Exception as exc:
                    self.send_json(500, {"status": "error", "error": friendly_exception(exc)})
                    return
                dynamic_status = dict(status_payload)
                if catalog_status_fn is not None:
                    dynamic_status.update(catalog_status_fn())
                self.send_json(
                    200,
                    {
                        "status": "ok",
                        "matches": matches,
                        "catalog_status": dynamic_status,
                    },
                )
                return
            self.send_json(404, {"status": "error", "error": "Not found."})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/pack-index/retry":
                if retry_pack_index_fn is None:
                    self.send_json(404, {"status": "error", "error": "Remote retry is not available."})
                    return
                try:
                    retry_status = dict(status_payload)
                    retry_status.update(retry_pack_index_fn())
                except Exception as exc:
                    self.send_json(500, {"status": "error", "error": friendly_exception(exc)})
                    return
                self.send_json(200, {"status": "ok", "catalog_status": retry_status})
                return
            if parsed.path not in {"/api/save", "/save"}:
                self.send_json(404, {"status": "error", "error": "Not found."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length > 16384:
                self.send_json(413, {"status": "error", "error": "Request body is too large."})
                return

            try:
                raw_body = self.rfile.read(length).decode("utf-8")
                fields = json.loads(raw_body or "{}")
                if not isinstance(fields, dict):
                    raise RuntimeError("Request body must be a JSON object.")
                result = save_config_fn(
                    str(fields.get("chip_name", "")),
                    str(fields.get("vendor", "")).strip() or None,
                    str(fields.get("pyocd_target", "")).strip() or None,
                    str(fields.get("selected_pack", "")).strip() or None,
                    str(fields.get("selected_part_number", "")).strip() or None,
                    bool(fields.get("selected_pack_installed")),
                    bool(fields.get("install_pack")),
                )
            except RuntimeError as exc:
                self.send_json(400, {"status": "error", "error": str(exc)})
                return
            except Exception as exc:
                self.send_json(500, {"status": "error", "error": friendly_exception(exc)})
                return

            state["path"] = result["path"]
            state["target"] = result["target"]
            self.send_json(
                200,
                {
                    "status": "ok",
                    "path": str(result["path"]),
                    "chip_name": result["chip_name"],
                    "target": result["target"],
                    "vendor": result.get("vendor"),
                    "pack_install": result.get("pack_install"),
                },
            )
            done.set()

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), TargetConfigWizardHandler)
    except OSError as exc:
        output_fn(f"Could not start the local target config wizard: {friendly_exception(exc)}")
        return None

    server.daemon_threads = True
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        output_fn(f"No {config_filename} was found in {cwd}. Opening local target config wizard: {url}")
        if not open_browser(url):
            output_fn(f"Could not open a browser for the local target config wizard. Open this URL manually: {url}")
            return None
        if not done.wait(timeout_seconds):
            output_fn(f"Local target config wizard timed out before {config_filename} was created.")
            return None
        created_path = state.get("path")
        if isinstance(created_path, Path):
            output_fn(f"Created {created_path} for pyOCD target '{state.get('target')}'.")
            return created_path
        return config_path if config_path.exists() else None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)
