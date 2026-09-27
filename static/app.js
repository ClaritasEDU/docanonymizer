// Doc Anonymizer frontend logic.
// Vanilla JS, no frameworks. Wires the single-page UI to the Flask API.

(() => {
  "use strict";

  // ----- Bootstrap data from the server -----
  const bootstrap = JSON.parse(document.getElementById("bootstrap-data").textContent);
  const VALID_TAGS = bootstrap.valid_tags;
  const OUTPUT_NOTES = bootstrap.output_notes;

  // PII metadata for the grid (sensitive flag + label + example).
  const PII_META = {
    PERSON:      ["Full Names",         "Jane Smith",                false],
    EMAIL:       ["Email Addresses",    "jane@school.org",           false],
    PHONE:       ["Phone Numbers",      "(512) 555-0198",            false],
    ADDRESS:     ["Physical Addresses", "123 Main St, Austin TX",    false],
    ID:          ["SSN / Tax IDs",      "123-45-6789",               false],
    ORG:         ["Organizations",      "St. Theresa School",        false],
    FINANCIAL:   ["Financial Data",     "Acct #4482",                false],
    DOB:         ["Dates of Birth",     "03/14/1992",                false],
    SID:         ["Student / Emp ID",   "Student ID 40021",          false],
    IP:          ["IP Addresses",       "192.168.1.100",             false],
    USERNAME:    ["Usernames",          "@jsmith",                   false],
    GRADE:       ["Grades / GPA",       "GPA 3.87",                  true],
    MEDICAL:     ["Medical / Health",   "IEP, Type 1 diabetes",      true],
    IMMIGRATION: ["Immigration",        "F-1 visa",                  true],
    DEMO:        ["Race / Ethnicity",   "Hispanic",                  true],
    RELIGION:    ["Religion",           "Catholic",                  true],
    GENDER:      ["Gender / Pronouns",  "she/her",                   true],
  };

  // ----- State -----
  const state = {
    sessionId: null,
    sanitizeAll: true,
    deselected: new Set(),
    deselectedTypes: new Set(),           // whole types kept as original text
    preview: null,
    polling: null,
    anonFile: null,                       // selected file for anonymize
    unanon: { file: null },               // file to restore
    keys: [],                             // saved keys from /api/keys
    keysSelected: new Set(),              // key names ticked in the picker
    keysSeen: new Set(),                  // names already shown (new ones default on)
    restoredText: "",
    restoredName: "",
    finished: false,                      // current session reached verify
  };

  // Prepended by [ COPY FOR AI ]. Asking the AI to keep identifiers intact is
  // the single biggest factor in getting a restorable answer back.
  const AI_NOTE =
    "Note: this data has been anonymized. Tokens like [PERSON_3A4F9C2B1D0E] stand in for " +
    "real names, emails, phone numbers, addresses, and other personal details. Each token " +
    "is unique to one real value. When you refer to any of them in your answer, copy the " +
    "token exactly as written, brackets included. Do not shorten, merge, or invent tokens.";

  // ----- DOM helpers -----
  const $ = (id) => document.getElementById(id);
  const show = (el) => el.classList.remove("hidden");
  const hide = (el) => el.classList.add("hidden");
  const create = (tag, attrs = {}, children = []) => {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") el.className = v;
      else if (k === "html") el.innerHTML = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v);
    }
    for (const c of children) el.append(c);
    return el;
  };

  async function api(path, opts = {}) {
    const resp = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    const ct = resp.headers.get("content-type") || "";
    const body = ct.includes("application/json") ? await resp.json() : await resp.text();
    if (!resp.ok) {
      const msg = (body && body.error) || resp.statusText || "request failed";
      const err = new Error(msg);
      err.status = resp.status;
      err.body = body;
      throw err;
    }
    return body;
  }

  // ----- Theme toggle -----
  function initTheme() {
    const saved = localStorage.getItem("docanon-theme");
    if (saved === "dark" || saved === "light") {
      document.documentElement.setAttribute("data-theme", saved);
    }
    updateThemeBtn();
    $("btn-toggle-theme").addEventListener("click", () => {
      const cur = document.documentElement.getAttribute("data-theme")
        || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      const next = cur === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      localStorage.setItem("docanon-theme", next);
      updateThemeBtn();
    });
  }
  function updateThemeBtn() {
    const cur = document.documentElement.getAttribute("data-theme")
      || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    $("btn-toggle-theme").textContent = cur === "dark" ? "[ DARK ]" : "[ LIGHT ]";
  }

  // ----- Endpoint manager -----
  async function loadEndpoints() {
    const data = await api("/api/endpoints");
    const sel = $("endpoint-select");
    sel.innerHTML = "";
    for (const ep of data.endpoints) {
      const opt = create("option", { value: ep.id });
      opt.textContent = `${ep.nickname} [${ep.model}]`;
      if (ep.id === data.active) opt.selected = true;
      sel.append(opt);
    }
    const active = data.endpoints.find((e) => e.id === data.active) || data.endpoints[0];
    if (active) {
      $("status-name").textContent = active.nickname;
      $("status-model").textContent = `[${active.model}]`;
    } else {
      $("status-name").textContent = "(none configured)";
      $("status-model").textContent = "";
    }
    renderEndpointRows(data.endpoints, data.active);
    healthCheck();
    return data;
  }

  function renderEndpointRows(endpoints, activeId) {
    const host = $("endpoints-rows");
    host.innerHTML = "";
    if (endpoints.length === 0) {
      host.append(create("div", { class: "muted", html: "no endpoints configured" }));
      return;
    }
    for (const ep of endpoints) {
      const dot = create("span", { class: "dot unk", "data-id": ep.id, "aria-label": "status" });
      const styleSpan = create("span", { class: "muted" });
      styleSpan.textContent = `[${ep.api_style} | ${ep.model}]`;
      const meta = create("div", {}, [
        create("div", {}, [document.createTextNode(`${ep.nickname} `), styleSpan]),
        create("div", { class: "meta url" }, [document.createTextNode(ep.base_url)]),
      ]);
      const setActive = create("button", {
        class: "btn secondary sm", type: "button", title: "set active",
        onclick: async () => { await api(`/api/endpoints/${ep.id}/select`, { method: "POST" }); loadEndpoints(); },
      });
      setActive.textContent = activeId === ep.id ? "[ ACTIVE ]" : "[ USE ]";
      const edit = create("button", {
        class: "btn secondary sm", type: "button",
        onclick: () => openEndpointForm(ep),
      });
      edit.textContent = "[ EDIT ]";
      const del = create("button", {
        class: "btn danger sm", type: "button",
        onclick: async () => {
          if (!confirm(`Delete endpoint "${ep.nickname}"?`)) return;
          await api(`/api/endpoints/${ep.id}`, { method: "DELETE" });
          loadEndpoints();
        },
      });
      del.textContent = "[ DEL ]";
      const row = create("div", { class: "row" }, [dot, meta, setActive,
        create("div", { class: "actions-cell" }, [edit, del])]);
      host.append(row);
    }
  }

  async function healthCheck() {
    try {
      const data = await api("/api/endpoints/health", { method: "POST", body: "{}" });
      const activeId = $("endpoint-select").value;
      const dot = $("status-dot");
      dot.classList.remove("ok", "err", "unk");
      const active = data.results.find((r) => r.id === activeId);
      if (active) dot.classList.add(active.status === "ok" ? "ok" : "err");
      else dot.classList.add("unk");
      // Per-row dots in the panel.
      for (const r of data.results) {
        const d = document.querySelector(`.dot[data-id="${r.id}"]`);
        if (d) {
          d.classList.remove("ok", "err", "unk");
          d.classList.add(r.status === "ok" ? "ok" : "err");
        }
      }
    } catch (exc) {
      // health check failure is non-fatal; show err dot.
      $("status-dot").classList.remove("ok", "unk");
      $("status-dot").classList.add("err");
    }
  }

  function openEndpointForm(ep) {
    show($("form-endpoint"));
    $("ep-id").value = ep ? ep.id : "";
    $("ep-nick").value = ep ? ep.nickname : "";
    $("ep-url").value = ep ? ep.base_url : "http://localhost:11434";
    document.querySelector(`input[name=api_style][value="${ep ? ep.api_style : "ollama"}"]`).checked = true;
    $("ep-model").value = ep ? ep.model : "llama3.2";
    $("ep-chunk").value = ep ? ep.chunk_tokens : 2000;
    hide($("ep-warn"));
  }
  function closeEndpointForm() { hide($("form-endpoint")); }

  async function checkLocalUrl(url) {
    try {
      const data = await api("/api/endpoints/check-local", {
        method: "POST", body: JSON.stringify({ base_url: url }),
      });
      return data.local;
    } catch { return false; }
  }

  // The server rejects non-local endpoints unless the payload carries an
  // explicit allow_nonlocal flag - set it only after the operator confirms.
  async function confirmNonlocal(payload) {
    const isLocal = await checkLocalUrl(payload.base_url);
    if (isLocal) return true;
    if (!confirm("This URL is NON-LOCAL. Document text WILL be sent to an external server. Continue anyway?")) return false;
    payload.allow_nonlocal = true;
    return true;
  }

  // ----- GitHub manager -----
  async function loadGithub() {
    const data = await api("/api/github");
    const host = $("github-rows");
    host.innerHTML = "";
    if (!data.connections.length) {
      host.append(create("div", { class: "muted", html: "no GitHub connections configured" }));
    } else {
      for (const c of data.connections) {
        const dot = create("span", { class: "dot unk" });
        const repoSpan = create("span", { class: "muted" });
        repoSpan.textContent = `[${c.repo}@${c.branch}]`;
        const meta = create("div", {}, [
          create("div", {}, [document.createTextNode(`${c.nickname} `), repoSpan]),
          create("div", { class: "meta" },
            [document.createTextNode(c.token_set ? "PAT set: ***" : "PAT NOT SET")]),
        ]);
        const test = create("button", {
          class: "btn secondary sm", type: "button",
          onclick: async () => {
            const r = await api(`/api/github/${c.id}/test`, { method: "POST" });
            dot.classList.remove("ok", "err", "unk");
            dot.classList.add(r.status === "ok" ? "ok" : "err");
            alert(r.status === "ok"
              ? `OK - ${r.repo} (default: ${r.default_branch})`
              : `FAIL - ${r.error || r.http_status}`);
          },
        });
        test.textContent = "[ TEST ]";
        const edit = create("button", {
          class: "btn secondary sm", type: "button",
          onclick: () => openGithubForm(c),
        });
        edit.textContent = "[ EDIT ]";
        const del = create("button", {
          class: "btn danger sm", type: "button",
          onclick: async () => {
            if (!confirm(`Delete connection "${c.nickname}"?`)) return;
            await api(`/api/github/${c.id}`, { method: "DELETE" });
            loadGithub();
          },
        });
        del.textContent = "[ DEL ]";
        host.append(create("div", { class: "row" },
          [dot, meta, test, create("div", { class: "actions-cell" }, [edit, del])]));
      }
    }
    // Also populate the push-panel select
    const pSel = $("push-conn");
    pSel.innerHTML = "";
    for (const c of data.connections) {
      const opt = create("option", { value: c.id });
      opt.textContent = `${c.nickname} - ${c.repo}@${c.branch}`;
      pSel.append(opt);
    }
    return data;
  }

  function openGithubForm(c) {
    show($("form-github"));
    $("gh-id").value = c ? c.id : "";
    $("gh-nick").value = c ? c.nickname : "";
    $("gh-repo").value = c ? c.repo : "";
    $("gh-branch").value = c ? c.branch : "main";
    $("gh-path").value = c ? c.path : "";
    $("gh-token").value = "";  // never echo PATs back
    if (c && c.token_set) {
      $("gh-token").placeholder = "PAT set; leave blank to keep";
    } else {
      $("gh-token").placeholder = "ghp_...";
    }
  }
  function closeGithubForm() { hide($("form-github")); }

  // ----- PII grid -----
  function renderPiiGrid() {
    const rows = document.querySelectorAll(".pii-row");
    rows.forEach((row) => {
      const tag = row.querySelector(".tag").textContent;
      const meta = PII_META[tag] || ["", "", false];
      const labelEl = row.querySelector(".label");
      labelEl.innerHTML = "";
      labelEl.append(document.createTextNode(meta[0]));
      labelEl.append(create("span", { class: "ex" },
        [document.createTextNode(` · ${meta[1]}`)]));
      const cell = row.querySelector(".check-cell");
      cell.innerHTML = "";
      if (meta[2]) {
        cell.append(create("span", { class: "sens-flag", html: "[SENSITIVE]" }));
      } else {
        const cb = create("input", { type: "checkbox", "data-tag": tag });
        cb.checked = true;
        cb.addEventListener("change", onPiiCheckboxChange);
        cell.append(cb);
      }
    });
    updateSanitizeAllButton();
  }
  function allTypesChecked() {
    const cbs = [...document.querySelectorAll(".pii-row input[type=checkbox]")];
    return cbs.length > 0 && cbs.every((cb) => cb.checked);
  }
  function onPiiCheckboxChange() {
    state.sanitizeAll = allTypesChecked();
    updateSanitizeAllButton();
  }
  function updateSanitizeAllButton() {
    // Solid button = everything selected (sanitize-all). Outlined = a subset
    // is selected; clicking it re-checks every type.
    const btn = $("btn-sanitize-all");
    btn.classList.toggle("secondary", !state.sanitizeAll);
    btn.textContent = "[ SANITIZE ALL ]";
  }

  function selectedTags() {
    if (state.sanitizeAll) return "ALL";
    const checked = [];
    document.querySelectorAll(".pii-row input[type=checkbox]:checked").forEach((cb) => {
      checked.push(cb.dataset.tag);
    });
    // Include sensitive-tier tags too (no checkbox; always part of ALL)
    for (const t of VALID_TAGS) {
      if (PII_META[t] && PII_META[t][2]) checked.push(t);
    }
    return checked.join(",");
  }

  // ----- File handling -----
  function bindDropzone(zoneId, inputId, onFile) {
    // Bound once per zone; listeners delegate so the hidden <input> can be
    // recreated by innerHTML rewrites without re-binding (listener-leak fix).
    const zone = $(zoneId);
    if (zone.dataset.bound) return;
    zone.dataset.bound = "1";
    // No click handler here: the zone is a <label> wrapping the file input,
    // so the browser opens the picker natively. A manual inp.click() would
    // fire a second activation and the picker opens twice per click.
    zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("dragover"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("dragover"));
    zone.addEventListener("drop", (e) => {
      e.preventDefault(); zone.classList.remove("dragover");
      if (e.dataTransfer.files[0]) {
        const inp = $(inputId);
        if (inp) { try { inp.files = e.dataTransfer.files; } catch {} }
        onFile(e.dataTransfer.files[0]);
      }
    });
    // "change" bubbles from the inner file input to the zone.
    zone.addEventListener("change", (e) => {
      if (e.target && e.target.files && e.target.files[0]) onFile(e.target.files[0]);
    });
  }

  // Map: dropzone id -> id of the <input type=file> inside it.
  const ZONE_TO_INPUT = {
    "dropzone": "file-input",
    "dropzone-unanon": "unanon-file-input",
  };

  function showFileLoaded(zoneId, file) {
    const zone = $(zoneId);
    const ext = (file.name.split(".").pop() || "").toLowerCase();
    const note = OUTPUT_NOTES[ext];
    const meta = note
      ? `${formatBytes(file.size)} · ${ext.toUpperCase()} · OUTPUT: ${note[0]} (${note[1]})`
      : `${formatBytes(file.size)} · ${ext.toUpperCase()} · UNSUPPORTED`;
    const inputId = ZONE_TO_INPUT[zoneId];
    // Preserve the file already attached to the input across the rerender.
    const oldFiles = inputId ? $(inputId)?.files : null;
    zone.classList.add("loaded");
    zone.innerHTML = `<div>${escapeHtml(file.name)}</div><div class="meta">${escapeHtml(meta)}</div>`;
    if (inputId) {
      const inp = document.createElement("input");
      inp.type = "file";
      inp.id = inputId;
      inp.style.display = "none";
      zone.append(inp);
      // Re-bind so click/change still flow.
      // (inputs cannot have their FileList programmatically reassigned in
      // modern browsers from another input; the file is held in our state
      // for the unanon zones, and `showFileLoaded` is only called after
      // the file was already captured upstream.)
    }
  }

  function formatBytes(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(1)} MB`;
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  // ----- Anonymize flow -----
  // An unfinished session still holds the original upload on the server.
  // Cancel it (never a finished one - its output is the user's work product).
  function cancelUnfinished(useBeacon) {
    const sid = state.sessionId;
    if (!sid || state.finished) return;
    const url = `/api/anonymize/${sid}/cancel`;
    if (useBeacon && navigator.sendBeacon) navigator.sendBeacon(url);
    else fetch(url, { method: "POST", keepalive: true }).catch(() => {});
  }

  async function uploadAndDetect(file) {
    cancelUnfinished(false);
    resetAnonymizeFlow();
    const fd = new FormData();
    fd.append("file", file);
    fd.append("tags", selectedTags());
    fd.append("custom_terms", $("custom-terms") ? $("custom-terms").value : "");
    const epId = $("endpoint-select").value;
    if (epId) fd.append("endpoint_id", epId);

    let data;
    try {
      const resp = await fetch("/api/anonymize/upload", { method: "POST", body: fd });
      data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "upload failed");
    } catch (exc) {
      alert(`Upload failed: ${exc.message}`);
      return;
    }

    state.sessionId = data.session_id;
    show($("lbl-detection"));
    show($("progress-detection"));
    renderDetectionSteps([
      { glyph: "[>]", text: "extracting document...", kind: "active", blink: true },
    ]);
    pollDetection();
  }

  async function pollDetection() {
    if (state.polling) clearInterval(state.polling);
    state.polling = setInterval(async () => {
      try {
        const data = await api(`/api/anonymize/${state.sessionId}/status`);
        if (data.error) {
          clearInterval(state.polling);
          renderDetectionSteps([{ glyph: "[!]", text: data.error, kind: "err" }]);
          return;
        }
        const steps = [];
        steps.push({ glyph: "[x]", text: "extracting document...", kind: "done" });
        for (let i = 0; i < (data.progress || []).length; i++) {
          const p = data.progress[i];
          steps.push({
            glyph: "[x]",
            text: `chunk ${p.index}/${p.total}: ${p.found} pii found`,
            kind: p.error ? "err" : "done",
          });
        }
        const total = (data.progress || [])[0]?.total || null;
        const lastIdx = (data.progress || []).length;
        if (total && lastIdx < total && !data.detection_complete) {
          steps.push({
            glyph: "[>]",
            text: `chunk ${lastIdx + 1}/${total}: detecting pii`,
            kind: "active", blink: true,
          });
          for (let i = lastIdx + 2; i <= total; i++) {
            steps.push({ glyph: "[ ]", text: `chunk ${i}/${total}: pending`, kind: "pending" });
          }
        }
        if (data.detection_complete) {
          steps.push({ glyph: "[x]", text: "building replacement map", kind: "done" });
          steps.push({ glyph: "[x]", text: "preparing preview", kind: "done" });
        }
        renderDetectionSteps(steps);

        if (data.detection_complete) {
          clearInterval(state.polling);
          state.preview = data.preview;
          renderPreview(data.preview);
          await refreshLog();
        }
      } catch (exc) {
        // don't kill the interval on transient errors
      }
    }, 800);
  }

  function renderDetectionSteps(steps) {
    const host = $("progress-detection");
    host.innerHTML = "";
    for (const s of steps) {
      const div = create("div", { class: `step ${s.kind || "pending"}` });
      div.append(create("span", { class: "glyph" }, [document.createTextNode(s.glyph)]));
      const t = create("span", { class: "text" }, [document.createTextNode(s.text)]);
      if (s.blink) t.append(create("span", { class: "blink" }));
      div.append(t);
      host.append(div);
    }
  }

  function renderPreview(prev) {
    if (!prev) return;
    show($("lbl-preview"));
    show($("block-preview"));
    $("prev-entities").textContent = prev.entities;
    renderTypeToggles(prev.counts || {});
    const kept = prev.kept_amounts || 0;
    $("prev-amounts").textContent = kept
      ? `${kept} amount-like value(s) the model tagged FINANCIAL/ID (gifts, totals, prices) are kept as original text. If any is a real account or ID number, add it under CUSTOM TERMS and detect again.`
      : "";
    $("prev-amounts").classList.toggle("hidden", !kept);

    const body = $("preview-body");
    body.innerHTML = "";
    const text = prev.text_head || "";
    let cursor = 0;
    for (const span of (prev.spans || [])) {
      if (span.start > cursor) {
        body.append(document.createTextNode(text.slice(cursor, span.start)));
      }
      const ph = create("span", {
        class: "placeholder",
        title: `Original: ${span.original}`,
        "data-original": span.original,
        "data-tag": span.placeholder.replace(/^\[|\]$/g, "").split("_")[0],
      });
      ph.textContent = span.placeholder;
      ph.addEventListener("click", () => {
        const orig = ph.dataset.original;
        if (state.deselected.has(orig)) {
          state.deselected.delete(orig);
          ph.classList.remove("deselected");
        } else {
          state.deselected.add(orig);
          ph.classList.add("deselected");
        }
      });
      body.append(ph);
      cursor = span.end;
    }
    if (cursor < text.length) body.append(document.createTextNode(text.slice(cursor)));
    if (prev.truncated) {
      body.append(create("div", { class: "muted", style: "margin-top:12px;" },
        [document.createTextNode(`Showing first 10,000 of ${prev.char_count} characters. All detected PII will be scrubbed regardless of scroll position.`)]));
    }
  }

  function renderTypeToggles(counts) {
    const host = $("prev-types");
    host.innerHTML = "";
    for (const tag of Object.keys(counts).sort()) {
      const b = create("button", { type: "button", class: "btn secondary sm", "data-tag": tag });
      const paint = () => {
        const on = !state.deselectedTypes.has(tag);
        b.setAttribute("aria-pressed", String(on));
        b.textContent = `[${on ? "x" : " "}] ${tag} ${counts[tag]}`;
        b.title = on ? `Click to keep every ${tag} value as original text` : `Click to replace every ${tag} value again`;
        document.querySelectorAll(`#preview-body .placeholder[data-tag="${tag}"]`).forEach((p) => {
          p.classList.toggle("deselected", !on || state.deselected.has(p.dataset.original));
        });
      };
      b.addEventListener("click", () => {
        if (state.deselectedTypes.has(tag)) state.deselectedTypes.delete(tag);
        else state.deselectedTypes.add(tag);
        paint();
      });
      host.append(b);
      paint();
    }
  }

  async function confirmScrub() {
    if (!state.sessionId) return;
    hide($("lbl-preview"));
    hide($("block-preview"));
    show($("lbl-scrub"));
    show($("progress-scrub"));
    renderScrubSteps([{ glyph: "[>]", text: "applying replacements...", kind: "active", blink: true }]);

    try {
      await api(`/api/anonymize/${state.sessionId}/confirm`, {
        method: "POST",
        body: JSON.stringify({ deselected: [...state.deselected], deselected_types: [...state.deselectedTypes] }),
      });
    } catch (exc) {
      renderScrubSteps([{ glyph: "[!]", text: `confirm failed: ${exc.message}`, kind: "err" }]);
      return;
    }

    // poll results
    if (state.polling) clearInterval(state.polling);
    state.polling = setInterval(async () => {
      try {
        const r = await api(`/api/anonymize/${state.sessionId}/results`);
        renderScrubSteps((r.scrub_steps || []).map(s => ({
          glyph: s.status === "done" ? "[x]" : s.status === "active" ? "[>]" : s.status === "err" ? "[!]" : "[ ]",
          text: s.step,
          kind: s.status,
          blink: s.status === "active",
        })));
        if (r.error) {
          clearInterval(state.polling);
          renderScrubSteps([{ glyph: "[!]", text: r.error, kind: "err" }]);
          return;
        }
        if (r.verify_result) {
          clearInterval(state.polling);
          renderResults(r);
          await refreshLog();
        }
      } catch {}
    }, 600);
  }

  function renderScrubSteps(steps) {
    const host = $("progress-scrub");
    host.innerHTML = "";
    for (const s of steps) {
      const div = create("div", { class: `step ${s.kind || "pending"}` });
      div.append(create("span", { class: "glyph" }, [document.createTextNode(s.glyph)]));
      const t = create("span", { class: "text" }, [document.createTextNode(s.text)]);
      if (s.blink) t.append(create("span", { class: "blink" }));
      div.append(t);
      host.append(div);
    }
  }

  function renderResults(r) {
    state.finished = true;
    show($("lbl-results"));
    show($("block-results"));
    const tbl = $("results-table");
    tbl.innerHTML = "";
    const rule = "─".repeat(40);
    const verified = !!(r.verify_result && r.verify_result.passed);
    const lines = [];
    lines.push({ text: rule, cls: "rule" });
    lines.push({ text: `COMPLETE: ${r.output_filename || "-"}` });
    if (verified) {
      lines.push({ text: "VERIFIED: 0 RESIDUAL PII DETECTED", cls: "verified" });
      const warns = (r.verify_result && r.verify_result.warnings) || [];
      if (warns.length) {
        lines.push({ text: `[!] PATTERNS RESEMBLING ${warns.join(", ")} REMAIN`, cls: "warnline" });
        lines.push({ text: `    These can be false alarms (any 10-digit number looks`, cls: "warnline" });
        lines.push({ text: `    like a phone). Skim the output before sharing.`, cls: "warnline" });
      }
    } else if (r.verify_result) {
      const types = r.verify_result.map_match_types || [];
      lines.push({ text: `[!] VERIFICATION FAILED: ${r.verify_result.total_matches} original value(s) survived`, cls: "failed" });
      lines.push({ text: `MATCH TYPES: ${types.join(", ") || "-"}`, cls: "failed" });
      lines.push({ text: `The unsafe output file was deleted. Nothing was released.`, cls: "failed" });
    }
    lines.push({ text: rule, cls: "rule" });
    const counts = r.counts || {};
    const keys = Object.keys(counts).sort();
    for (const k of keys) {
      lines.push({ text: padRight(k, 12) + padRight(`${counts[k]} replacements`, 18) });
    }
    if (keys.length) {
      lines.push({ text: rule, cls: "rule" });
      lines.push({ text: padRight("TOTAL", 12) + `${r.totals.entities} entities    ${r.totals.replacements} replacements` });
      lines.push({ text: rule, cls: "rule" });
    }
    for (const l of lines) {
      const div = create("div", l.cls ? { class: l.cls } : {});
      div.textContent = l.text;
      tbl.append(div);
    }

    $("btn-dl-file").disabled = !verified;
    $("btn-dl-key").disabled = !verified;
    $("btn-view-text").disabled = !verified;
    $("btn-push-github").disabled = !verified;
    // Reset the view-on-screen panel for each new run, then show the verified
    // output straight away - seeing it is the next step in the flow.
    hide($("block-screen-text"));
    $("screen-text").value = "";
    if (verified) loadScreenText(false);

    if ((r.formula_warnings || []).length) {
      const w = create("div", { class: "note" }, [document.createTextNode(
        `[!] ${r.formula_warnings.length} formula(s) reference PII strings. Review before sharing.`)]);
      $("block-results").append(w);
    }
  }

  function padRight(s, n) { return s + " ".repeat(Math.max(0, n - s.length)); }

  function resetAnonymizeFlow() {
    state.sessionId = null;
    state.finished = false;
    state.preview = null;
    state.deselected.clear();
    state.deselectedTypes.clear();
    if (state.polling) clearInterval(state.polling);
    state.polling = null;
    hide($("lbl-detection"));
    hide($("progress-detection"));
    $("progress-detection").innerHTML = "";
    hide($("lbl-preview"));
    hide($("block-preview"));
    hide($("lbl-scrub"));
    hide($("progress-scrub"));
    $("progress-scrub").innerHTML = "";
    hide($("lbl-results"));
    hide($("block-results"));
    hide($("panel-push"));
    hide($("block-screen-text"));
    $("screen-text").value = "";
  }

  async function cancelSession() {
    if (state.sessionId) {
      try { await api(`/api/anonymize/${state.sessionId}/cancel`, { method: "POST" }); } catch {}
    }
    resetAnonymizeFlow();
    state.anonFile = null;
    // reset drop zone
    const z = $("dropzone");
    z.classList.remove("loaded");
    z.innerHTML = `<div>&gt; DROP FILE HERE</div><div class="sub">&nbsp;&nbsp;or [ BROWSE ]</div>`;
    const inp = document.createElement("input");
    inp.type = "file"; inp.id = "file-input"; inp.style.display = "none";
    z.append(inp);
    rebindDropzones();
    $("btn-detect").disabled = true;
  }

  // ----- On-screen anonymized output -----
  async function loadScreenText(focus) {
    if (!state.sessionId) return;
    try {
      const data = await api(`/api/anonymize/${state.sessionId}/text`);
      setTabular($("screen-text"), data.filename);
      $("screen-text").value = data.text;
      $("screen-text-meta").textContent =
        `${data.char_count.toLocaleString()} chars · re-extracted from ${data.filename}`;
      show($("block-screen-text"));
      if (focus) { $("screen-text").focus(); $("screen-text").select(); }
    } catch (exc) {
      alert(`Could not load text: ${exc.message}`);
    }
  }

  // Spreadsheet output reads as one row per line; wrapping splits rows.
  function setTabular(textarea, filename) {
    const tabular = /\.(xlsx|xls|ods|csv)$/i.test(filename || "");
    textarea.setAttribute("wrap", tabular ? "off" : "soft");
    textarea.classList.toggle("tabular", tabular);
  }

  async function copyText(text, btn) {
    let ok = false;
    try {
      await navigator.clipboard.writeText(text);
      ok = true;
    } catch {
      // Fallback for browsers that block the async clipboard API.
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.append(ta);
      ta.select();
      try { ok = document.execCommand("copy"); } catch { ok = false; }
      ta.remove();
    }
    const original = btn.textContent;
    btn.textContent = ok ? "[ COPIED ]" : "[ COPY FAILED ]";
    setTimeout(() => { btn.textContent = original; }, 1200);
  }

  // ----- Unanonymize flow -----
  async function loadKeys() {
    let data;
    try {
      data = await api("/api/keys");
    } catch (exc) {
      $("key-list").textContent = `could not load keys: ${exc.message}`;
      return;
    }
    state.keys = data.keys || [];
    const names = new Set(state.keys.map((k) => k.name));
    // Keys that disappeared are dropped; newly seen current-format keys start
    // selected (IDs are unique across keys, so selecting them is always safe).
    for (const n of [...state.keysSelected]) if (!names.has(n)) state.keysSelected.delete(n);
    for (const k of state.keys) {
      if (!state.keysSeen.has(k.name)) {
        state.keysSeen.add(k.name);
        if (k.format !== "legacy") state.keysSelected.add(k.name);
      }
    }
    renderKeys();
  }

  function renderKeys() {
    const host = $("key-list");
    host.innerHTML = "";
    if (!state.keys.length) {
      host.append(create("div", { class: "empty" }, [document.createTextNode(
        "No keys yet. Anonymize a file first, or [ IMPORT KEY FILE ] from another machine.")]));
    }
    for (const k of state.keys) {
      const cb = create("input", { type: "checkbox", "data-name": k.name, "aria-label": `use key ${k.name}` });
      cb.checked = state.keysSelected.has(k.name);
      cb.addEventListener("change", () => {
        if (cb.checked) state.keysSelected.add(k.name); else state.keysSelected.delete(k.name);
        updateKeysMeta();
      });
      const info = create("div", {}, [
        create("div", { class: "file" }, [document.createTextNode(k.original_filename || k.name)]),
        create("div", { class: "meta" }, [document.createTextNode(
          `${(k.created_at || k.modified || "").replace("T", " ").replace("Z", "")} · ${k.name}`)]),
      ]);
      const count = create("div", { class: "count" }, [document.createTextNode(
        `${Number(k.ids).toLocaleString()} ids`)]);
      if (k.format === "legacy") {
        count.append(create("span", { class: "legacy" }, [document.createTextNode("[LEGACY 4-CHAR]")]));
      }
      const row = create("label", { class: "key-row" }, [cb, info, count]);
      host.append(row);
    }
    updateKeysMeta();
  }

  function updateKeysMeta() {
    const n = state.keysSelected.size;
    $("keys-meta").textContent = `${n} of ${state.keys.length} selected`;
    maybeEnableUnanon();
  }

  function maybeEnableUnanon() {
    const haveKeys = state.keysSelected.size > 0;
    $("btn-restore-text").disabled = !(haveKeys && $("restore-input").value.trim());
    $("btn-unanonymize").disabled = !(haveKeys && state.unanon.file);
  }

  function bindUnanonymize() {
    bindDropzone("dropzone-unanon", "unanon-file-input", (file) => {
      state.unanon.file = file;
      showFileLoaded("dropzone-unanon", file);
      rebindDropzones();
      maybeEnableUnanon();
    });
    $("restore-input").addEventListener("input", maybeEnableUnanon);
    $("btn-restore-text").addEventListener("click", runRestoreText);
    $("btn-unanonymize").addEventListener("click", runUnanonymize);
    $("btn-keys-all").addEventListener("click", () => {
      for (const k of state.keys) state.keysSelected.add(k.name);
      renderKeys();
    });
    $("btn-keys-none").addEventListener("click", () => {
      state.keysSelected.clear();
      renderKeys();
    });
    $("btn-key-import").addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("key-import-input").click(); }
    });
    $("key-import-input").addEventListener("change", async (e) => {
      const f = e.target.files && e.target.files[0];
      e.target.value = "";
      if (!f) return;
      const fd = new FormData();
      fd.append("key", f);
      try {
        const resp = await fetch("/api/keys/import", { method: "POST", body: fd });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || "import failed");
        state.keysSeen.add(data.imported);
        state.keysSelected.add(data.imported);   // an import is an explicit choice
        await loadKeys();
      } catch (exc) {
        showUnanonError(`Import failed: ${exc.message}`);
      }
      refreshLog();
    });
    $("btn-copy-restored").addEventListener("click", () =>
      copyText($("restored-text").value, $("btn-copy-restored")));
    $("btn-dl-restored-text").addEventListener("click", () => {
      const blob = new Blob([state.restoredText], { type: "text/plain;charset=utf-8" });
      const a = create("a", { href: URL.createObjectURL(blob), download: state.restoredName || "restored.txt" });
      document.body.append(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
    });
  }

  function showUnanonError(msg) {
    const el = $("unanon-result");
    el.textContent = `[!] ${msg}`;
    show(el);
  }

  function clearRestoreOutput() {
    hide($("unanon-result"));
    hide($("lbl-restore"));
    hide($("block-restore"));
    hide($("btn-dl-restored-file"));
    $("restore-report").innerHTML = "";
    $("restored-text").value = "";
  }

  async function runRestoreText() {
    clearRestoreOutput();
    const btn = $("btn-restore-text");
    btn.disabled = true;
    try {
      const data = await api("/api/unanonymize/text", {
        method: "POST",
        body: JSON.stringify({ text: $("restore-input").value, keys: [...state.keysSelected] }),
      });
      showRestored(data.text, data.report, "restored.txt", null);
    } catch (exc) {
      showUnanonError(exc.message);
    }
    maybeEnableUnanon();
    refreshLog();
  }

  async function runUnanonymize() {
    clearRestoreOutput();
    const fd = new FormData();
    fd.append("file", state.unanon.file);
    fd.append("keys", JSON.stringify([...state.keysSelected]));
    const btn = $("btn-unanonymize");
    btn.disabled = true;
    try {
      const resp = await fetch("/api/unanonymize", { method: "POST", body: fd });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "failed");
      const txtName = data.output_filename.replace(/\.[^.]+$/, "") + ".txt";
      showRestored(data.text, data.report, txtName, data);
    } catch (exc) {
      showUnanonError(exc.message);
    }
    maybeEnableUnanon();
    refreshLog();
  }

  function showRestored(text, report, txtName, fileData) {
    state.restoredText = text || "";
    state.restoredName = txtName;
    renderRestoreReport(report, fileData);
    setTabular($("restored-text"), fileData ? fileData.output_filename : "");
    $("restored-text").value = state.restoredText;
    $("restored-meta").textContent = `${state.restoredText.length.toLocaleString()} chars`;
    const link = $("btn-dl-restored-file");
    if (fileData) {
      link.href = fileData.download_url;
      link.setAttribute("download", fileData.output_filename);
      link.textContent = `[ DOWNLOAD ${fileData.output_filename.split(".").pop().toUpperCase()} ]`;
      show(link);
    }
    show($("lbl-restore"));
    show($("block-restore"));
    $("lbl-restore").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderRestoreReport(r, fileData) {
    const tbl = $("restore-report");
    tbl.innerHTML = "";
    const rule = "─".repeat(44);
    const lines = [];
    const W = 14, N = 8;
    lines.push({ text: rule, cls: "rule" });
    if (fileData) lines.push({ text: `FILE: ${fileData.output_filename}` });
    const clean = r.unresolved_count === 0 && r.residual === 0;
    lines.push({
      text: `RESTORED: ${r.restored.toLocaleString()} identifier(s) -> original values`,
      cls: r.restored > 0 && clean ? "verified" : "",
    });
    lines.push({ text: `KEYS USED: ${r.keys_used.length}` });
    lines.push({ text: rule, cls: "rule" });
    const tags = Object.keys(r.by_tag || {}).sort();
    for (const t of tags) {
      lines.push({ text: padRight(t, W) + String(r.by_tag[t].toLocaleString()).padStart(N) });
    }
    if (tags.length) lines.push({ text: rule, cls: "rule" });
    if (r.restored === 0 && r.unresolved_count === 0) {
      lines.push({ text: "No identifiers found in this input.", cls: "unresolved" });
    }
    if (r.relabeled) {
      lines.push({ text: `[i] ${r.relabeled} had a different label from the AI - restored by ID`, cls: "" });
    }
    if (r.untagged) {
      lines.push({ text: `[i] ${r.untagged} appeared as a bare ID - restored by ID`, cls: "" });
    }
    if (r.legacy_ambiguous) {
      lines.push({ text: `[!] ${r.legacy_ambiguous} old 4-char ID(s) pointed at more than one value;`, cls: "unresolved" });
      lines.push({ text: `    restored to the longest. Check those names.`, cls: "unresolved" });
    }
    if (r.unresolved_count) {
      lines.push({ text: `[!] ${r.unresolved_count} NOT RESTORED - not in the selected keys, left as-is:`, cls: "failed" });
      const shown = r.unresolved.slice(0, 20);
      for (const tok of shown) lines.push({ text: `    ${tok}`, cls: "failed" });
      if (r.unresolved.length > shown.length) {
        lines.push({ text: `    (+${r.unresolved.length - shown.length} more)`, cls: "failed" });
      }
      lines.push({ text: `    Select more keys, or check the AI did not alter these.`, cls: "failed" });
    }
    if (r.residual) {
      lines.push({ text: `[!] ${r.residual} identifier(s) could not be written back into the file.`, cls: "failed" });
      lines.push({ text: `    Use the text below, or paste the content instead.`, cls: "failed" });
    }
    if (r.unresolved_count || r.residual) lines.push({ text: rule, cls: "rule" });
    for (const l of lines) {
      const div = create("div", l.cls ? { class: l.cls } : {});
      div.textContent = l.text;
      tbl.append(div);
    }
  }

  // Re-attach dropzones after innerHTML rewrites.
  function rebindDropzones() {
    bindDropzone("dropzone", "file-input", (file) => {
      state.anonFile = file;
      showFileLoaded("dropzone", file);
      rebindDropzones();
      $("btn-detect").disabled = false;
    });
    bindDropzone("dropzone-unanon", "unanon-file-input", (file) => {
      state.unanon.file = file;
      showFileLoaded("dropzone-unanon", file);
      rebindDropzones();
      maybeEnableUnanon();
    });
  }

  // ----- Log -----
  async function refreshLog() {
    try {
      const data = await api("/api/log/tail?n=80");
      $("log-body").textContent = data.lines.join("\n");
      $("log-body").scrollTop = $("log-body").scrollHeight;
    } catch {}
  }

  // ----- Wire everything up -----
  document.addEventListener("DOMContentLoaded", async () => {
    initTheme();
    renderPiiGrid();
    rebindDropzones();
    bindUnanonymize();
    loadKeys();

    await loadEndpoints();
    await loadGithub();
    await refreshLog();

    // Status bar buttons
    $("btn-toggle-endpoints").addEventListener("click", () => {
      $("panel-endpoints").classList.toggle("hidden");
      hide($("panel-github"));
    });
    $("btn-toggle-github").addEventListener("click", () => {
      $("panel-github").classList.toggle("hidden");
      hide($("panel-endpoints"));
    });
    $("endpoint-select").addEventListener("change", async (e) => {
      await api(`/api/endpoints/${e.target.value}/select`, { method: "POST" });
      loadEndpoints();
    });

    // Endpoint manager form
    $("btn-add-endpoint").addEventListener("click", () => openEndpointForm(null));
    $("btn-cancel-endpoint").addEventListener("click", closeEndpointForm);
    $("btn-recheck-endpoints").addEventListener("click", healthCheck);
    $("ep-url").addEventListener("change", async () => {
      const isLocal = await checkLocalUrl($("ep-url").value);
      isLocal ? hide($("ep-warn")) : show($("ep-warn"));
    });
    $("btn-test-endpoint").addEventListener("click", async () => {
      const tmp = collectEndpointForm();
      if (!(await confirmNonlocal(tmp))) return;
      // Save/test flow: do a synthetic health check via the endpoint.
      try {
        const resp = await fetch("/api/endpoints", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...tmp, id: tmp.id || "test-temp" }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || "test failed");
        const r = await api("/api/endpoints/health", {
          method: "POST",
          body: JSON.stringify({ id: data.endpoint.id }),
        });
        const status = r.results[0];
        alert(status.status === "ok"
          ? `OK - ${status.model_count} models in ${status.elapsed_ms}ms`
          : `FAIL - ${status.error || status.http_status || "unreachable"}`);
        loadEndpoints();
      } catch (exc) {
        alert(`Test failed: ${exc.message}`);
      }
    });
    $("form-endpoint").addEventListener("submit", async (e) => {
      e.preventDefault();
      const payload = collectEndpointForm();
      if (!(await confirmNonlocal(payload))) return;
      try {
        await api("/api/endpoints", { method: "POST", body: JSON.stringify(payload) });
        closeEndpointForm();
        loadEndpoints();
      } catch (exc) { alert(`Save failed: ${exc.message}`); }
    });

    // GitHub manager form
    $("btn-add-github").addEventListener("click", () => openGithubForm(null));
    $("btn-cancel-github").addEventListener("click", closeGithubForm);
    $("btn-test-github").addEventListener("click", async () => {
      const payload = collectGithubForm();
      try {
        const saved = await api("/api/github", { method: "POST", body: JSON.stringify(payload) });
        const r = await api(`/api/github/${saved.connection.id}/test`, { method: "POST" });
        alert(r.status === "ok"
          ? `OK - ${r.repo} (default: ${r.default_branch})`
          : `FAIL - ${r.error || r.http_status}`);
        loadGithub();
      } catch (exc) { alert(`Test failed: ${exc.message}`); }
    });
    $("form-github").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await api("/api/github", { method: "POST", body: JSON.stringify(collectGithubForm()) });
        closeGithubForm();
        loadGithub();
      } catch (exc) { alert(`Save failed: ${exc.message}`); }
    });

    // SANITIZE ALL re-selects every PII type.
    $("btn-sanitize-all").addEventListener("click", () => {
      document.querySelectorAll(".pii-row input[type=checkbox]").forEach((cb) => { cb.checked = true; });
      state.sanitizeAll = true;
      updateSanitizeAllButton();
    });

    // Anonymize buttons
    $("btn-detect").addEventListener("click", () => {
      const f = state.anonFile || $("file-input")?.files?.[0];
      if (f) uploadAndDetect(f);
    });
    $("btn-confirm").addEventListener("click", confirmScrub);
    $("btn-cancel").addEventListener("click", cancelSession);
    $("btn-dl-file").addEventListener("click", () => {
      window.location.href = `/api/anonymize/${state.sessionId}/download/file`;
    });
    $("btn-dl-key").addEventListener("click", () => {
      window.location.href = `/api/anonymize/${state.sessionId}/download/key`;
    });
    $("btn-view-text").addEventListener("click", () => loadScreenText(true));
    $("btn-hide-text").addEventListener("click", () => hide($("block-screen-text")));
    $("btn-copy-text").addEventListener("click", () =>
      copyText($("screen-text").value, $("btn-copy-text")));
    $("btn-copy-ai").addEventListener("click", () =>
      copyText(`${AI_NOTE}\n\n${$("screen-text").value}`, $("btn-copy-ai")));
    $("btn-push-github").addEventListener("click", () => {
      show($("panel-push"));
      const sel = $("push-conn");
      const opt = sel.options[sel.selectedIndex];
      $("push-branch").value = opt ? (opt.textContent.split("@").pop() || "main") : "main";
    });
    $("btn-close-push").addEventListener("click", () => hide($("panel-push")));
    $("btn-do-push").addEventListener("click", async () => {
      const result = $("push-result");
      result.textContent = "pushing...";
      try {
        const r = await api("/api/github/push", {
          method: "POST",
          body: JSON.stringify({
            session_id: state.sessionId,
            connection_id: $("push-conn").value,
            branch: $("push-branch").value,
            path: $("push-path").value,
          }),
        });
        result.innerHTML = `pushed - <a href="${r.html_url}" target="_blank" rel="noopener">${escapeHtml(r.path)}</a> (${r.sha?.slice(0, 7) || ""})`;
      } catch (exc) {
        result.textContent = `failed: ${exc.message}`;
      }
      refreshLog();
    });

    // Tabs
    $("tab-anonymize").addEventListener("click", () => switchTab("anonymize"));
    $("tab-unanonymize").addEventListener("click", () => switchTab("unanonymize"));

    // Log panel buttons
    $("btn-refresh-log").addEventListener("click", refreshLog);
    $("btn-copy-log").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText($("log-body").textContent);
      } catch {
        // fallback: select the text
        const range = document.createRange();
        range.selectNodeContents($("log-body"));
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        document.execCommand("copy");
      }
    });

    // Periodic light health check.
    setInterval(healthCheck, 30000);

    // Closing or reloading the tab mid-run must not strand the upload.
    window.addEventListener("pagehide", () => cancelUnfinished(true));
  });

  function switchTab(which) {
    if (which === "unanonymize") loadKeys();   // pick up keys from recent runs
    if (which === "anonymize") {
      $("tab-anonymize").classList.add("active");
      $("tab-anonymize").setAttribute("aria-selected", "true");
      $("tab-unanonymize").classList.remove("active");
      $("tab-unanonymize").setAttribute("aria-selected", "false");
      show($("mode-anonymize"));
      hide($("mode-unanonymize"));
    } else {
      $("tab-unanonymize").classList.add("active");
      $("tab-unanonymize").setAttribute("aria-selected", "true");
      $("tab-anonymize").classList.remove("active");
      $("tab-anonymize").setAttribute("aria-selected", "false");
      show($("mode-unanonymize"));
      hide($("mode-anonymize"));
    }
  }

  function collectEndpointForm() {
    return {
      id: $("ep-id").value || undefined,
      nickname: $("ep-nick").value,
      base_url: $("ep-url").value,
      api_style: document.querySelector("input[name=api_style]:checked").value,
      model: $("ep-model").value,
      chunk_tokens: parseInt($("ep-chunk").value || "2000", 10),
    };
  }
  function collectGithubForm() {
    return {
      id: $("gh-id").value || undefined,
      nickname: $("gh-nick").value,
      repo: $("gh-repo").value,
      branch: $("gh-branch").value,
      path: $("gh-path").value,
      token: $("gh-token").value || undefined,
    };
  }
})();
