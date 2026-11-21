// DMM Downloader page: shows the tool's download queue and DMM library, and sends what the user does (see web.py).
"use strict";
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  let state = null;
  let lastLine = 0;
  let libVersion = -1;
  let library = null; // [{cid, title, vr, download, bought, saved}] or null
  let stopped = false;
  const open = new Set();
  const seen = new Map(); // job id -> last state (a video that fails opens by itself)
  const selected = new Set(); // library content ids ticked
  let libFilter = "all";
  let tab = "download";

  // -- Dark or light: the system's choice until the user picks one (kept in this browser only) -----------------

  const THEME_KEY = "dmm-downloader-theme";
  const systemTheme = () => (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  const shownTheme = () => document.documentElement.dataset.theme || systemTheme();
  function applyTheme(theme) {
    if (theme === "dark" || theme === "light") document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
    const btn = $("#themeBtn");
    if (btn) {
      const label = `Switch to ${shownTheme() === "dark" ? "light" : "dark"}`;
      btn.title = label;
      btn.setAttribute("aria-label", label);
    }
  }
  try { applyTheme(localStorage.getItem(THEME_KEY)); } catch { applyTheme(null); }
  $("#themeBtn")?.addEventListener("click", () => {
    const next = shownTheme() === "dark" ? "light" : "dark";
    applyTheme(next);
    try { localStorage.setItem(THEME_KEY, next); } catch { /* not kept: fine */ }
  });

  // -- talking to the tool --------------------------------------------------------------------

  // Sent with every request to the tool: only its own page can add it (web.py).
  const OURS = { "X-DMM-Downloader": "1" };

  async function api(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...OURS },
      body: JSON.stringify(body || {}),
      credentials: "same-origin",
    });
    let data = {};
    try { data = await res.json(); } catch { /* empty answer */ }
    if (!res.ok) throw new Error(data.error || `The downloader answered ${res.status}.`);
    return data;
  }

  async function act(path, body, okText) {
    try {
      const data = await api(path, body);
      if (okText) toast(typeof okText === "function" ? okText(data) : okText, true);
      await refresh();
      return data;
    } catch (err) {
      toast(err.message);
      await refresh();
      return null;
    }
  }

  let toastTimer = 0;
  function toast(text, ok = false) {
    const el = $("#toast");
    el.textContent = text;
    el.classList.toggle("ok", ok);
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, ok ? Math.max(3000, text.length * 70) : 8000);
  }

  let pollTimer = 0;
  let failures = 0;
  async function refresh() {
    clearTimeout(pollTimer);
    if (stopped) return;
    try {
      const res = await fetch(`/api/state?after=${lastLine}&lib=${libVersion}`, { headers: OURS, credentials: "same-origin" });
      if (res.status === 403) { stopped = true; document.body.textContent = "Open the downloader with the link shown in its window."; return; }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      state = await res.json();
      if ("library" in state) { library = state.library; libVersion = state.library_version; }
      failures = 0;
      $("#goodbye").hidden = true;
      render();
    } catch {
      failures++;
      if (failures >= 3) $("#goodbye").hidden = false;
    }
    pollTimer = setTimeout(refresh, failures ? 3000 : state && state.running ? 700 : 1200);
  }

  // -- formatting ---------------------------------------------------------------------------------

  function size(bytes) {
    if (bytes == null) return "";
    const gb = bytes / 1e9;
    return gb >= 1 ? `${gb.toFixed(2)} GB` : `${Math.round(bytes / 1e6)} MB`;
  }
  function minutes(seconds) {
    if (!seconds || !isFinite(seconds)) return "";
    const m = Math.round(seconds / 60);
    return m < 1 ? "under a minute left" : `${m} min left`;
  }
  const cap = s => s && !/^[a-z0-9_]+[0-9]/.test(s.split(/[\s:]/)[0]) ? s[0].toUpperCase() + s.slice(1) : s;

  // Redraws only on a change: replacing a control every second could swallow a click on it.
  const shown = new WeakMap();
  function setHtml(el, html) {
    if (shown.get(el) === html) return;
    el.innerHTML = html;
    shown.set(el, html);
  }
  // Fills a field from the tool's state unless the user is typing in it.
  function fill(id, value) {
    const el = $("#" + id);
    if (el && document.activeElement !== el && !el.dataset.dirty) el.value = value ?? "";
  }
  function check(id, value) {
    const el = $("#" + id);
    if (el) el.checked = !!value;
  }

  // -- the queue ----------------------------------------------------------------------------------

  const PILLS = {
    checking: ["grey", "Looking up"], ready: ["ready", "Ready"], waiting: ["ready", "Waiting"], working: ["run", "Working"],
    done: ["ok", "Saved"], vrdone: ["vr", "VR saved (encrypted)"], there: ["grey", "Already saved"],
    bad: ["bad", "Not available"], failed: ["bad", "Failed"], limit: ["ask", "DMM limit: later"],
  };
  const shownState = job => job.state === "done" && job.encrypted ? "vrdone" : job.state;
  // Where a video comes from: the row's chip and the "From" line.
  const SOURCE_CHIP = { premium: "Premium", vrchannel: "VR Channel", library: "Library" };
  const SOURCE_FROM = { premium: "DMM Premium", vrchannel: "DMM VR Channel (monthly VR plan)", library: "DMM library (bought)" };

  function hint(message) {
    // The VR Channel's reasons first: a VR video there needs no "check the id".
    if (/no VR Channel subscription/.test(message)) {
      return "This VR video is in DMM's VR Channel, but this DMM account has no VR Channel subscription. Subscribe to the VR Channel on DMM (then “Look up again”), or buy the video and use “My library only”.";
    }
    if (/in DMM's VR Channel from /.test(message)) return "DMM's VR Channel offers it from that day on: look it up again then.";
    if (/gives its downloads only in Japan|not available in your region/.test(message)) {
      return "Enter a proxy in Japan in Settings, Downloads (“Check” tells whether DMM counts it as Japan), then “Look up again”.";
    }
    if (/through the Japan proxy|Japan route/.test(message)) return "Settings, Downloads: is the Japan proxy running and connected? “Check” tells, then “Look up again”.";
    if (/not in your DMM library/.test(message)) {
      return `Check the id: content ids are exact, <span class="mono">sone096</span> and <span class="mono">sone00096</span> can be different videos. The My library tab lists what this account bought.`;
    }
    if (/log in again/.test(message)) return "Settings tab, DMM account: “Log in and save”, then “Look up again” here.";
    if (/streaming only/.test(message)) return "DMM offers no download of a streaming-only (or rental) purchase.";
    if (/not in DMM's VR Channel/.test(message) && !/library/.test(message)) {
      return "“VR Channel only” finds the VR videos in DMM's VR Channel list. Choose “Premium, VR Channel, then my library” to look on Premium and in your library too.";
    }
    if (/not on DMM Premium/.test(message) && !/library/.test(message)) return "Choose “Premium, VR Channel, then my library” or “My library only” if you bought it.";
    return "";
  }

  function sideOf(job) {
    if (job.state === "working") {
      const step = job.steps.length ? job.steps[job.steps.length - 1] : "starting";
      const p = job.progress;
      if (p && p.unit === "bytes") {
        const pct = p.total ? Math.min(100, (p.done / p.total) * 100) : 0;
        const bits = [p.total ? `${size(p.done)} / ${size(p.total)}` : size(p.done), p.speed ? `${(p.speed / 1e6).toFixed(1)} MB/s` : "", minutes(p.eta)]
          .filter(Boolean).join(" · ");
        return `${esc(step)}<br><span class="mono">${esc(bits)}</span><progress max="100" value="${pct.toFixed(1)}"></progress>`;
      }
      if (p && p.unit === "fraction") return `${esc(step)}<progress max="1" value="${p.done}"></progress>`;
      return esc(step);
    }
    if (job.parts) return `${job.parts} part${job.parts === 1 ? "" : "s"}`;
    return "";
  }

  // VR files saved encrypted earlier: decrypted instead of downloaded ("all" saved, or some of the parts).
  function savedNote(job) {
    const saved = job.saved_encrypted || [];
    if (!saved.length) return "";
    return saved.length >= job.parts ? "decrypts the saved files" : `${saved.length} of ${job.parts} parts saved`;
  }

  function detailOf(job) {
    const out = state.options.output_dir;
    const st = shownState(job);
    let html = "";
    if (job.found) {
      const pick = state.options.quality === "pick" && job.state === "ready";
      const quality = pick
        ? `<select data-pick="${job.id}" aria-label="Quality of ${esc(job.cid)}">${job.qualities.map(q => `<option value="${esc(q)}" ${q === job.label ? "selected" : ""}>${esc(q)}${q === job.best ? " (best)" : ""}</option>`).join("")}</select>`
        : `${esc(job.label)} <span class="sub">(${job.label === job.best ? "best of" : "offered:"} ${esc(job.qualities.join(", "))})</span>`;
      const names = (job.files && job.files.length ? job.files : (job.names || []).map(n => `${out}${out.includes("/") ? "/" : "\\"}${n}`));
      html += `<dl class="kv">
        <dt>From</dt><dd>${esc(SOURCE_FROM[job.found] || job.found)}${job.vr ? " · VR" : ""}${job.japan ? ` <span class="sub">· ${state.japan.route === "proxy" ? "through the Japan proxy" : "through this connection (Japan)"}</span>` : ""}${job.page_cid ? ` · DMM shows it as ${esc(job.page_cid)}` : ""}</dd>
        <dt>Quality</dt><dd>${quality}</dd>
        <dt>Parts</dt><dd>${job.parts}</dd>
        <dt>${job.files && job.files.length ? "Saved" : "Saved as"}</dt><dd class="files">${names.map(n => `<span>${esc(n)}</span>`).join("")}</dd>
      </dl>`;
    }
    if (st === "bad" || st === "failed") {
      const h = hint(job.message || "");
      html += `<div class="box bad"><b>${esc(cap(job.message || "Failed."))}</b>${h ? `<span>${h}</span>` : ""}</div>`;
    }
    if (st === "limit") html += `<div class="box ask"><b>DMM refuses more downloads of this VR video for now.</b><span>DMM limits how often one VR video can be downloaded (other videos still work). The parts already downloaded are kept: start it again later (it seems to reset each day) and only the missing parts are downloaded. A VPN on DMM-Downloader gets the same refusal: keep it off the VPN (only DMM-Fetch may use it).</span></div>`;
    // The tool's message says where: "already in <folder>: abc00123.mkv" (or "already downloaded" after a run).
    if (st === "there") html += `<div class="note">${esc(cap(job.message || "already saved").replace(/\.?$/, "."))} Tick “Download again videos already saved” to replace it.</div>`;
    const saved = job.saved_encrypted || [];
    if (saved.length && ["ready", "waiting"].includes(job.state)) {
      html += `<div class="box vr"><b>${saved.length >= job.parts ? "The encrypted files saved earlier are decrypted, nothing is downloaded." : `The encrypted files saved earlier are decrypted; only the other ${job.parts - saved.length} part${job.parts - saved.length === 1 ? " is" : "s are"} downloaded.`}</b><span class="files">${saved.map(n => `<span>${esc(n)}</span>`).join("")}</span><span>They are removed after a checked decrypt unless “Keep the encrypted downloads” is ticked.</span></div>`;
    }
    if (st === "done") {
      html += job.vr
        ? `<div class="box ok">Decrypted with jav-it and checked (size, MediaInfo: damage and length).</div>`
        : `<div class="box ok">Saved and checked (MediaInfo: length, parts, DMM's video id).</div>`;
    }
    if (st === "vrdone") html += `<div class="box vr">Saved encrypted (.wsdcf), as DMM sends VR: watch it in DMM VR Player. With VR.MOD next to jav-it, VR videos are decrypted (the note below says what was missing).</div>`;
    const warnings = job.warnings || [];
    if (warnings.length) html += warnings.map(w => `<div class="warn">⚠ ${esc(w)}</div>`).join("");
    if (job.steps.length && ["working", "done", "failed", "limit"].includes(job.state)) { // limit: the waits stay listed
      html += `<ul class="steps">${job.steps.map((s, i) => `<li class="${job.state === "working" && i === job.steps.length - 1 ? "now" : ""}">${esc(s)}</li>`).join("")}</ul>`;
    }
    const buttons = [];
    if (["bad", "failed", "limit", "there"].includes(st)) buttons.push(`<button type="button" class="btn small" data-retry="${job.id}">Look up again</button>`);
    if (!["waiting", "working"].includes(job.state)) buttons.push(`<button type="button" class="btn small" data-remove="${job.id}">Remove from the list</button>`);
    if (buttons.length) html += `<div class="actions">${buttons.join("")}</div>`;
    return `<div class="detail">${html}</div>`;
  }

  function renderQueue() {
    const q = $("#queue");
    const busy = q.contains(document.activeElement) && document.activeElement.tagName === "SELECT";
    for (const job of state.jobs) {
      const before = seen.get(job.id);
      if (before !== job.state && ["bad", "failed", "limit"].includes(job.state)) open.add(job.id);
      // "I pick it for each video": the quality menu is inside the row, so a row opens once it is looked up.
      if (before !== job.state && job.state === "ready" && state.options.quality === "pick") open.add(job.id);
      seen.set(job.id, job.state);
    }
    if (!busy) {
      setHtml(q, state.jobs.length ? state.jobs.map(job => {
        const [cls, label] = PILLS[shownState(job)] || ["grey", job.state];
        const chips = [
          job.found ? `<span class="chip${job.found === "vrchannel" ? " vr" : ""}">${esc(SOURCE_CHIP[job.found] || job.found)}</span>` : "",
          job.label ? `<span class="chip">${esc(job.label)}</span>` : "",
          job.vr ? `<span class="chip vr">VR</span>` : "",
          job.parts > 1 ? `<span class="chip">${job.parts} parts</span>` : "",
          savedNote(job) && ["ready", "waiting"].includes(job.state) ? `<span class="chip vr">${savedNote(job)}</span>` : "",
        ].join("");
        return `<div class="item">
          <div class="itemrow" data-open="${job.id}" role="button" tabindex="0" aria-expanded="${open.has(job.id)}">
            <div class="pillcell"><span class="pill ${cls}">${label}</span></div>
            <div class="main"><div class="cid">${esc(job.cid)}</div><div class="chips">${chips}</div></div>
            <div class="side">${sideOf(job)}</div>
          </div>
          ${open.has(job.id) ? detailOf(job) : ""}
        </div>`;
      }).join("") : `<div class="empty">No videos yet. Paste content ids or DMM links above, or pick them in My library.</div>`);
    }
    const count = states => state.jobs.filter(j => states.includes(shownState(j))).length;
    setHtml($("#counts"), [[count(["checking"]), "looking up"], [count(["ready", "waiting"]), "ready"], [count(["working"]), "working"],
      [count(["done", "vrdone"]), "saved"], [count(["there"]), "already saved"], [count(["bad", "failed", "limit"]), "not now"]]
      .filter(([n]) => n).map(([n, l]) => `<span class="pill grey">${n} ${l}</span>`).join("") || "<span>Empty list</span>");
    const ready = state.jobs.filter(j => j.state === "ready").length;
    const start = $("#startBtn");
    start.disabled = !ready || state.running;
    start.textContent = state.running ? (state.stopping ? "Stopping…" : "Working…") : `Download ${ready} video${ready === 1 ? "" : "s"}`;
    $("#stopBtn").hidden = !state.running || state.stopping;
  }

  // -- the library --------------------------------------------------------------------------------

  function libShown() {
    const find = $("#libSearch").value.trim().toLowerCase();
    return (library || []).filter(l =>
      (libFilter === "all" || (libFilter === "2d" && !l.vr) || (libFilter === "vr" && l.vr) || (libFilter === "new" && !l.saved))
      && (!find || l.cid.includes(find) || l.title.toLowerCase().includes(find)));
  }

  function renderLibrary() {
    $("#libCount").textContent = library ? library.length : "";
    if (!library) {
      setHtml($("#libRows"), `<tr><td colspan="7">${esc(state.dmm.logged_in ? (state.library_error || "Reading the library from DMM…") : "Log in to DMM first (Settings).")}</td></tr>`);
      $("#libSum").textContent = "";
      $("#libAdd").disabled = true;
      return;
    }
    const rows = libShown();
    setHtml($("#libRows"), rows.map(l => `<tr class="${l.download ? "" : "off"}">
      <td><input type="checkbox" data-lib="${esc(l.cid)}" ${selected.has(l.cid) ? "checked" : ""} ${l.download ? "" : "disabled"} aria-label="Select ${esc(l.cid)}"></td>
      <td class="c">${esc(l.cid)}</td><td class="t" title="${esc(l.title)}">${esc(l.title)}</td>
      <td>${l.vr ? `<span class="pill vr">VR</span>` : "2D"}</td><td>${esc(l.bought)}</td>
      <td>${l.download ? (l.vr && !state.tools.vr ? "Download (encrypted)" : "Download") : "Streaming only"}</td>
      <td>${l.saved ? `<span class="pill ok">Saved</span>` : "–"}</td></tr>`).join("") || `<tr><td colspan="7">Nothing matches.</td></tr>`);
    const downloadable = library.filter(l => l.download).length;
    const vr = library.filter(l => l.vr).length;
    const saved = library.filter(l => l.saved).length;
    $("#libSum").textContent = `${library.length} video${library.length === 1 ? "" : "s"} bought on this DMM account: ${downloadable} can be downloaded`
      + ` (${vr} VR${state.tools.vr ? "" : ", saved encrypted"}), ${library.length - downloadable} streaming only. ${saved} already in ${state.options.output_dir}.`;
    for (const cid of [...selected]) if (!library.some(l => l.cid === cid && l.download)) selected.delete(cid);
    $("#libAdd").textContent = selected.size ? `Add ${selected.size} selected to the queue` : "Add selected to the queue";
    $("#libAdd").disabled = !selected.size;
    const ticks = rows.filter(l => l.download);
    $("#libAll").checked = ticks.length > 0 && ticks.every(l => selected.has(l.cid));
  }

  // -- header, options, settings, log ------------------------------------------------------------

  function setDot(id, text, cls) {
    const el = $("#" + id);
    el.textContent = text;
    el.className = `dot ${cls || ""}`;
  }

  function renderStatus() {
    $("#version").textContent = `v${state.version}`;
    const d = state.dmm;
    if (state.starting) setDot("dmmStatus", "Starting…", "off");
    else if (d.logged_in) setDot("dmmStatus", `DMM: logged in${d.library_count != null ? ` · library ${d.library_count} videos` : ""}`, "");
    else setDot("dmmStatus", `DMM: ${d.error || "not logged in"}`, "bad");
    const t = state.tools;
    // jav-it with DECRYPT.MOD decrypts 2D videos, with VR.MOD VR videos (without it they are saved encrypted).
    const twoD = t.javit && !t.errors.decrypt_mod;
    const vrMod = t.javit && !t.errors.vr_mod;
    const javitText = !t.javit ? "jav-it missing" : twoD && vrMod ? "jav-it: 2D, VR" : twoD ? "jav-it: 2D (no VR.MOD)"
      : vrMod ? "jav-it: VR (no DECRYPT.MOD)" : "jav-it: no DECRYPT.MOD, VR.MOD";
    setDot("javitStatus", javitText, twoD ? "" : "bad");
    const progs = [["MediaInfo", t.mediainfo], ["FFmpeg", t.ffmpeg]];
    const missing = progs.filter(([, p]) => !p).map(([n]) => n);
    setDot("toolStatus", missing.length ? `${missing.join(", ")} missing` : "MediaInfo, FFmpeg", missing.length ? "bad" : "");
    setDot("fetchStatus", t.fetch_helper ? `Files: ${t.fetch_program}` : "", t.fetch_helper ? "" : "off");
    // The Japan route (VR Channel downloads): shown when it reaches Japan, or when it doesn't and "VR Channel only"
    // depends on it (with the default, the rows say so when a VR Channel video needs it).
    const j = state.japan;
    const route = j.route === "proxy" ? "via Japan proxy" : "via this connection";
    if (j.state === "ok") setDot("jpStatus", `VR Channel: ${route}`, "");
    else if (["region", "error"].includes(j.state) && state.options.source === "vrchannel") {
      setDot("jpStatus", j.state === "region" ? "VR Channel: not in Japan" : "VR Channel: route problem", "bad");
    } else setDot("jpStatus", "", "off");
  }

  function japanState(j) {
    const when = j.checked ? ` (checked ${esc(j.checked)})` : "";
    if (j.state === "ok") {
      return `<span class="ok-t">reaches Japan</span> <span class="sub">${j.route === "proxy" ? `through ${esc(j.shown)}` : "this program's own connection (your VPN app?)"}; DMM shows the VR Channel${when}</span>`;
    }
    if (j.state === "region") {
      return `<span class="bad-t">not in Japan</span> <span class="sub">DMM answered “not available in your region”${when}: use a proxy in Japan</span>`;
    }
    if (j.state === "error") return `<span class="bad-t">couldn't be checked</span> <span class="sub">${esc(j.message)}${when}</span>`;
    return `<span class="sub">not checked yet: “Check” asks DMM whether ${j.route === "proxy" ? `the proxy ${esc(j.shown)}` : "this program's own connection"} counts as Japan</span>`;
  }

  function renderOptions() {
    const o = state.options;
    check(`src-${o.source}`, true);
    check(`q-${o.quality}`, true);
    check("opt-merge", o.merge);
    check("opt-keep", o.keep_dcv);
    check("opt-again", o.overwrite);
    fill("opt-out", o.output_dir);
  }

  function toolRow(name, path, error, extra) {
    return `<span>${name}</span><span>${path ? `<span class="ok-t">found</span> <span class="path">${esc(path)}</span>` : `<span class="bad-t">${esc(error || "not found")}</span>`}${extra || ""}</span>`;
  }

  function renderSettings() {
    const d = state.dmm;
    const s = state.settings;
    const t = state.tools;
    $("#dmmState").textContent = d.logged_in
      ? `Logged in as ${d.account}${d.library_count != null ? ` · library ${d.library_count} videos` : ""}. jav-it decrypts with this same login. The DMM session is saved, so DMM doesn't see a new login every time.`
      : (d.error || "Not logged in.");
    $("#storeNote").textContent = d.login_source === "environment"
      ? "This login comes from DMM_USERNAME and DMM_PASSWORD in the environment: change or remove them there (“Log in and save” and “Log out” don't change them)."
      : d.store
        ? "Kept in the system's password store (Windows Credential Manager, macOS Keychain, Linux Secret Service), never in a file or the log. jav-it gets it from this program each time it decrypts, so you set it only here: “Log out” removes it, logging in with another account replaces it."
        : "No password store on this system: the login works until this program closes; set DMM_USERNAME and DMM_PASSWORD in the environment to keep it.";
    fill("set-parallel", s.parallel);
    fill("set-retries", s.retries);
    fill("set-fetch", s.fetch_helper);
    fill("set-jp", s.vrchannel_route);
    setHtml($("#jpState"), japanState(state.japan));
    fill("set-patreon", s.javit_email);
    fill("set-javit", s.jav_it);
    fill("set-mediainfo", s.mediainfo);
    fill("set-ffmpeg", s.ffmpeg);
    fill("set-ffprobe", s.ffprobe);
    $("#fetchName").textContent = t.fetch_program;
    setHtml($("#fetchState"), t.fetch_helper
      ? `<span class="ok-t">in use</span> <span class="path">${esc(t.fetch_helper)}</span>`
      : t.errors.fetch_helper ? `<span class="bad-t">${esc(t.errors.fetch_helper)}</span>` : `<span class="sub">not used: files download in this program (${esc(t.fetch_program)} isn't next to it)</span>`);
    const mod = (name, error) => `<span>${name}</span><span>${t.javit && !error ? `<span class="ok-t">found</span>` : `<span class="bad-t">${esc(error || "needs jav-it first")}</span>`}</span>`;
    setHtml($("#javitTools"), toolRow("jav-it", t.javit, t.errors.javit) + mod("DECRYPT.MOD", t.errors.decrypt_mod)
      + mod("VR.MOD", t.errors.vr_mod && `${t.errors.vr_mod}: VR videos are saved encrypted`));
    $("#vrNote").textContent = t.vr
      ? "VR videos (from the VR Channel or your library) are decrypted with jav-it (VR.MOD) into .mp4 and checked; their parts are never joined (-pt1.mp4, -pt2.mp4, ...). Encrypted VR files saved earlier (.wsdcf) are decrypted instead of downloaded again."
      : `VR videos (from the VR Channel or your library) are saved encrypted (.wsdcf), as DMM sends them: ${t.no_vr}. With jav-it and VR.MOD next to this program they are decrypted into .mp4.`;
    setHtml($("#progTools"), toolRow("MediaInfo", t.mediainfo, t.errors.mediainfo) + toolRow("FFmpeg", t.ffmpeg, t.errors.ffmpeg) + toolRow("ffprobe", t.ffprobe, t.errors.ffmpeg));
    $("#footConfig").textContent = s.config_path;
    $("#footLog").textContent = s.log_dir;
    $("#footAddr").textContent = `Runs on your computer only, at ${location.host}; closing the window stops the downloader.`;
  }

  function renderLog() {
    if (!state.log.length) return;
    const el = $("#log");
    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
    el.insertAdjacentHTML("beforeend", state.log.map(([n, time, text]) => `<span class="t">${esc(time)}</span>  ${esc(text)}\n`).join(""));
    lastLine = state.log[state.log.length - 1][0];
    if (atBottom) el.scrollTop = el.scrollHeight;
  }

  function render() {
    renderStatus();
    renderOptions();
    renderQueue();
    renderLibrary();
    renderSettings();
    renderLog();
  }

  // -- browsing folders and files ----------------------------------------------------------------

  let browseMode = "folder"; // folder (where videos go), txt (a list to import)
  let browsePath = "";
  let browseFile = "";

  async function browseTo(path) {
    try {
      const files = browseMode === "folder" ? "" : "txt";
      const res = await fetch(`/api/browse?path=${encodeURIComponent(path)}&files=${files}`, { headers: OURS, credentials: "same-origin" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);
      browsePath = data.path;
      browseFile = "";
      $("#browsePath").textContent = data.path || "This computer";
      $("#browseUp").disabled = data.parent == null;
      $("#browseUp").dataset.path = data.parent ?? "";
      const list = $("#browseList");
      list.innerHTML = data.entries.map((e, i) => e.dir
        ? `<div class="entry"><button type="button" class="open" data-go="${i}">📁 ${esc(e.name)}</button></div>`
        : `<label class="entry"><input type="radio" name="browsefile" value="${i}"> ${esc(e.name)}<span class="sz">${size(e.size)}</span></label>`).join("")
        || `<div class="entry">No folders${browseMode === "folder" ? "" : " or .txt files"} here.</div>`;
      list.entries = data.entries;
      $("#browseOk").disabled = browseMode === "folder" ? !data.path : true;
    } catch (err) {
      toast(err.message);
    }
  }

  function openBrowse(mode) {
    browseMode = mode;
    const titles = { folder: "Choose the folder for the videos", txt: "Choose a list of content ids (.txt)" };
    $("#browseTitle").textContent = titles[mode];
    $("#browseOk").textContent = mode === "folder" ? "Use this folder" : "Import";
    $("#browseHint").textContent = mode === "folder" ? "Open the folder, then use it." : "Open folders, then pick the file.";
    $("#browse").hidden = false;
    $("#browseCancel").focus();
    browseTo(mode === "folder" ? state.options.output_dir : "");
  }

  // -- events -------------------------------------------------------------------------------------

  $$(".tabs button").forEach(b => b.addEventListener("click", () => {
    tab = b.dataset.tab;
    $$(".tabs button").forEach(x => x.setAttribute("aria-selected", String(x === b)));
    ["download", "library", "settings", "log"].forEach(t => { $("#tab-" + t).hidden = t !== tab; });
  }));

  $$("input[name=src]").forEach(r => r.addEventListener("change", () => act("/api/options", { source: r.value })));
  $$("input[name=q]").forEach(r => r.addEventListener("change", () => {
    // Switching to "I pick it for each video" opens the rows already looked up, where the quality menus are.
    if (r.value === "pick") state.jobs.filter(j => j.state === "ready").forEach(j => open.add(j.id));
    act("/api/options", { quality: r.value });
  }));
  [["opt-merge", "merge"], ["opt-keep", "keep_dcv"], ["opt-again", "overwrite"]].forEach(([id, name]) =>
    $("#" + id).addEventListener("change", e => act("/api/options", { [name]: e.target.checked })));
  const out = $("#opt-out");
  out.addEventListener("input", () => { out.dataset.dirty = "1"; });
  const saveOut = () => { if (!out.dataset.dirty) return; delete out.dataset.dirty; act("/api/options", { output_dir: out.value }, "Folder saved."); };
  out.addEventListener("change", saveOut);
  out.addEventListener("keydown", e => { if (e.key === "Enter") saveOut(); });

  $("#addBtn").addEventListener("click", async () => {
    const text = $("#addIds").value;
    if (!text.trim()) { toast("Paste content ids or DMM links first."); return; }
    const data = await act("/api/add", { text }, d => `Added ${d.added} video${d.added === 1 ? "" : "s"}.${d.invalid.length ? ` Not content ids: ${d.invalid.slice(0, 3).join(", ")}.` : ""}`);
    if (data) $("#addIds").value = "";
  });
  $("#fromLib").addEventListener("click", () => $$(".tabs button")[1].click());
  $("#startBtn").addEventListener("click", () => act("/api/start", {}));
  $("#stopBtn").addEventListener("click", () => act("/api/stop", {}, "Stops after the videos that are running."));
  $("#clearBtn").addEventListener("click", () => act("/api/clear", {}));

  $("#queue").addEventListener("change", e => {
    const sel = e.target.closest("[data-pick]");
    if (sel) act("/api/pick", { id: +sel.dataset.pick, rate: sel.value }).then(() => sel.blur());
  });
  $("#queue").addEventListener("click", e => {
    const retry = e.target.closest("[data-retry]");
    if (retry) { act("/api/retry", { id: +retry.dataset.retry }); return; }
    const remove = e.target.closest("[data-remove]");
    if (remove) { open.delete(+remove.dataset.remove); act("/api/remove", { id: +remove.dataset.remove }); return; }
    if (e.target.closest("input, button, select, a")) return;
    const row = e.target.closest("[data-open]");
    if (row) { const id = +row.dataset.open; open.has(id) ? open.delete(id) : open.add(id); render(); }
  });
  $("#queue").addEventListener("keydown", e => {
    const row = e.target.closest("[data-open]");
    if (row && e.target === row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); row.click(); }
  });

  $("#libFilter").addEventListener("click", e => {
    const b = e.target.closest("button");
    if (!b) return;
    libFilter = b.dataset.f;
    $$("#libFilter button").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
    renderLibrary();
  });
  $("#libSearch").addEventListener("input", renderLibrary);
  $("#libRows").addEventListener("change", e => {
    const c = e.target.closest("[data-lib]");
    if (c) { c.checked ? selected.add(c.dataset.lib) : selected.delete(c.dataset.lib); renderLibrary(); }
  });
  $("#libAll").addEventListener("change", e => {
    libShown().filter(l => l.download).forEach(l => e.target.checked ? selected.add(l.cid) : selected.delete(l.cid));
    renderLibrary();
  });
  $("#libAdd").addEventListener("click", async () => {
    const cids = [...selected];
    const data = await act("/api/library/add", { cids }, d => `Added ${d.added} video${d.added === 1 ? "" : "s"} from your library.`);
    if (data) { selected.clear(); $$(".tabs button")[0].click(); }
  });
  $("#libRefresh").addEventListener("click", () => act("/api/library/refresh", {}, "Reading the library from DMM…"));

  $("#loginBtn").addEventListener("click", async () => {
    const data = await act("/api/login", { email: $("#set-mail").value, password: $("#set-pass").value }, "Logged in to DMM.");
    if (data) { $("#set-pass").value = ""; $("#set-mail").value = ""; }
  });
  $("#logoutBtn").addEventListener("click", () => act("/api/logout", {}, "Logged out."));
  const settingsBody = ids => Object.fromEntries(ids.map(([id, name]) => [name, $("#" + id).value]));
  const saveSettings = async (ids, text) => {
    const data = await act("/api/settings", settingsBody(ids), text);
    if (data) ids.forEach(([id]) => delete $("#" + id).dataset.dirty);
  };
  $$("#tab-settings input").forEach(el => el.addEventListener("input", () => { el.dataset.dirty = "1"; }));
  $("#dlSave").addEventListener("click", () => saveSettings([["set-parallel", "parallel"], ["set-retries", "retries"], ["set-fetch", "fetch_helper"], ["set-jp", "vrchannel_route"]], "Saved."));
  $("#jpCheck").addEventListener("click", async () => {
    // A changed route is saved first, so "Check" checks what is in the field.
    if ($("#set-jp").dataset.dirty) await saveSettings([["set-jp", "vrchannel_route"]]);
    toast("Asking DMM through the Japan route…", true);
    await act("/api/japan/check", {}, d => d.state === "ok" ? "The Japan route reaches Japan." : d.state === "region" ? "Not in Japan for DMM." : `Couldn't be checked: ${d.message}`);
  });
  $("#javitSave").addEventListener("click", () => saveSettings([["set-patreon", "javit_email"], ["set-javit", "jav_it"]], "Saved."));
  $("#progSave").addEventListener("click", () => saveSettings([["set-mediainfo", "mediainfo"], ["set-ffmpeg", "ffmpeg"], ["set-ffprobe", "ffprobe"]], "Saved."));
  $("#doctorBtn").addEventListener("click", () => act("/api/doctor", {}, "Setup checked (see the Log)."));
  $("#quitBtn").addEventListener("click", async () => {
    const ok = await act("/api/quit", {});
    if (ok) { stopped = true; $("#goodbye").hidden = false; $("#goodbye").textContent = "The downloader has stopped. You can close this page."; }
  });

  $$("[data-browse]").forEach(b => b.addEventListener("click", () => openBrowse(b.dataset.browse)));
  $("#browseList").addEventListener("click", e => {
    const go = e.target.closest("[data-go]");
    if (go) browseTo($("#browseList").entries[+go.dataset.go].path);
  });
  $("#browseList").addEventListener("change", e => {
    const pick = e.target.closest("input[name=browsefile]");
    if (pick) { browseFile = $("#browseList").entries[+pick.value].path; $("#browseOk").disabled = false; }
  });
  $("#browseUp").addEventListener("click", e => browseTo(e.currentTarget.dataset.path));
  $("#browseCancel").addEventListener("click", () => { $("#browse").hidden = true; });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("#browse").hidden) $("#browse").hidden = true; });
  $("#browseOk").addEventListener("click", () => {
    if (browseMode === "folder") act("/api/options", { output_dir: browsePath }, "Folder saved.");
    else act("/api/import", { path: browseFile }, d => `Imported: ${d.added} video${d.added === 1 ? "" : "s"} added.`);
    $("#browse").hidden = true;
  });

  refresh();
})();
