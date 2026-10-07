/* Repository and model output is always rendered as text, never as markup. */
"use strict";

(() => {
  const POLL_LIMIT_MS = 15 * 60 * 1000;
  const POLL_INTERVAL_MS = 2000;
  const MAX_POLL_ERRORS = 3;
  const TERMINAL = new Set(["improved", "reviewed", "stopped", "failed"]);
  const STATUS_LABELS = {
    queued: "Queued", running: "Reviewing", improved: "Verified improvement",
    reviewed: "Reviewed", stopped: "Stopped at a guardrail", failed: "Review failed",
    applied: "Applied", accepted: "Applied", resolved: "Applied", fixed: "Applied",
    rejected: "Rejected", unresolved: "Unresolved", skipped: "Skipped",
    needs_review: "Unresolved", not_attempted: "Unresolved",
    pending: "Unresolved", attempted: "Attempted", passed: "Passed", pass: "Passed",
    failed_check: "Failed", fail: "Failed", disabled: "Disabled", unavailable: "Unavailable",
    timeout: "Timed out", high: "High", medium: "Medium", low: "Low",
  };
  const $ = (id) => document.getElementById(id);
  const state = {
    source: "github", file: null, health: null, submitting: false, selectedId: null,
    currentRun: null, generation: 0, pollTimer: null, pollController: null,
    deadline: 0, pollErrors: 0, recentController: null, reportKey: "",
  };

  function element(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined && content !== null) node.textContent = String(content);
    return node;
  }

  function humanize(value) {
    const text = String(value ?? "").replace(/[_-]/g, " ");
    return text ? text[0].toUpperCase() + text.slice(1) : "Unknown";
  }

  function stringify(value) {
    if (value === undefined || value === null) return "";
    if (typeof value === "string") return value;
    if (typeof value === "object") return JSON.stringify(value, null, 2);
    return String(value);
  }

  function asArray(value) {
    if (Array.isArray(value)) return value;
    if (value === undefined || value === null || value === "") return [];
    if (typeof value === "object") {
      return Object.entries(value).map(([name, item]) => (
        typeof item === "object" && item !== null ? { name, ...item } : { name, detail: item }
      ));
    }
    return [value];
  }

  function tone(value) {
    const status = String(value ?? "").toLowerCase();
    if (["improved", "applied", "accepted", "resolved", "fixed", "passed", "pass"].includes(status)) return "positive";
    if (["failed", "failed_check", "fail", "rejected", "high", "error"].includes(status)) return "danger";
    if (["stopped", "unresolved", "needs_review", "not_attempted", "pending", "attempted", "medium", "timeout"].includes(status)) return "warning";
    if (["queued", "running"].includes(status)) return "active";
    return "neutral";
  }

  function badge(status, label) {
    return element("span", `pill ${tone(status)}`, label ?? STATUS_LABELS[status] ?? humanize(status));
  }

  function notice(id, message) {
    $(id).textContent = message || "";
    $(id).hidden = !message;
  }

  class ApiError extends Error {
    constructor(message, status = 0, fieldId = null) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.fieldId = fieldId;
    }
  }

  function errorMessage(error) {
    if (error.status === 401) return "This server requires a CodeProof access token. Enter it in Connection settings and try again.";
    if (error.status === 403) return "The server refused this request. Use CodeProof from its local address and check your access settings.";
    if (error instanceof ApiError) return error.message;
    return error.message === "Request timed out" ? "The request timed out. Check the service connection and try again." : "Could not reach the review service. Check that it is running and try again.";
  }

  async function request(path, options = {}) {
    const { signal, timeout = 20000, responseType = "json", ...fetchOptions } = options;
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (signal?.aborted) controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    let timedOut = false;
    const timer = window.setTimeout(() => { timedOut = true; controller.abort(); }, timeout);
    const headers = new Headers(fetchOptions.headers || {});
    const token = $("api-token").value.trim();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    try {
      const response = await fetch(path, { ...fetchOptions, headers, signal: controller.signal, credentials: "same-origin" });
      if (!response.ok) {
        let message = `The service returned an error (${response.status}).`;
        try {
          const body = await response.json();
          if (typeof body.detail === "string") message = body.detail;
          else if (Array.isArray(body.detail)) message = body.detail.map((item) => item.msg || "Invalid input").join(". ");
        } catch { /* Keep the useful HTTP status when the server has no JSON body. */ }
        throw new ApiError(message, response.status);
      }
      return responseType === "blob" ? await response.blob() : await response.json();
    } catch (error) {
      if (timedOut) throw new Error("Request timed out");
      throw error;
    } finally {
      window.clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
  }

  function sourceName(run) {
    const source = run?.source;
    if (typeof source === "string") return source;
    return source?.repository || source?.name || source?.repo || source?.url || `Review ${String(run?.id || "").slice(0, 8)}`;
  }

  function displayDate(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(date);
  }

  function setSource(source, focus = false) {
    if (state.submitting) return;
    state.source = source;
    for (const name of ["github", "zip"]) {
      const active = name === source;
      const tab = $(`${name}-tab`);
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      tab.classList.toggle("active", active);
      $(`${name}-panel`).hidden = !active;
    }
    notice("form-error", "");
    if (focus) $(`${source}-tab`).focus();
  }

  function setFile(file) {
    if (!file) return;
    state.file = file;
    $("file-label").textContent = file.name;
    $("file-help").textContent = `${formatBytes(file.size)} · Click to choose another file`;
    notice("form-error", "");
  }

  function formatBytes(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function providerLabel(provider) {
    return { openai: "OpenAI", gemini: "Gemini" }[provider] || humanize(provider);
  }

  function providerInfo(provider) {
    return Array.isArray(state.health?.providers) ? state.health.providers.find((item) => item.id === provider) : undefined;
  }

  function updateModelControls() {
    const provider = $("model-provider").value;
    const explicit = provider === "openai" || provider === "gemini";
    const info = providerInfo(provider);
    $("model-provider").disabled = state.submitting;
    $("model-name").disabled = state.submitting || !explicit;
    $("model-api-key").disabled = state.submitting || !explicit;
    $("model-reasoning").disabled = state.submitting || provider !== "openai";
    $("reasoning-field").hidden = provider !== "openai";
    $("model-fields").classList.toggle("no-reasoning", provider !== "openai");
    $("model-mode-label").textContent = explicit ? `${providerLabel(provider)} · This review` : "Server default";
    $("model-name").placeholder = explicit ? info?.model || (provider === "openai" ? "gpt-6-luna" : "Provider model name") : "Choose a provider first";
    $("model-name-help").textContent = explicit && info?.model ? `Leave blank to use ${info.model}.` : "Leave blank to use the selected provider’s default model.";
    $("model-api-key").placeholder = explicit ? "API key for this review" : "Choose a provider first";
    $("model-key-help").textContent = !explicit ? "Choose a provider to use your own key. Leave blank to use its server key when configured." : info?.configured ? `Leave blank to use the server’s configured ${providerLabel(provider)} key.` : info ? `No server key is configured for ${providerLabel(provider)}. Add your key to enable model proposals for this review.` : "Leave blank to use this provider’s server key when configured.";
    const selected = state.health?.selected_provider;
    const model = state.health?.selected_model;
    $("model-config-summary").textContent = selected ? `Server default: ${providerLabel(selected)}${model ? ` · ${model}` : ""}. Choose a provider to override it for this review.` : "Use the server’s defaults, or choose a provider for this review.";
  }

  function captureModelSettings(form) {
    const provider = $("model-provider").value;
    const model = $("model-name").value.trim();
    const key = $("model-api-key").value.trim();
    try {
      if (!provider && (model || key)) throw new ApiError("Choose a provider before entering a model name or provider API key.", 0, "model-provider");
      if (provider && !["openai", "gemini"].includes(provider)) throw new ApiError("Choose OpenAI, Gemini, or the server default.", 0, "model-provider");
      if (!provider) return;
      if (model && !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(model)) throw new ApiError("Use a model name of up to 128 letters, numbers, dots, underscores, or hyphens, starting with a letter or number.", 0, "model-name");
      if (key.length > 4096 || /[^\x20-\x7E]/.test(key)) throw new ApiError("The provider API key must be no more than 4096 printable ASCII characters. Remove line breaks or unsupported characters.", 0, "model-api-key");
      form.append("model_provider", provider);
      if (model) form.append("model_name", model);
      if (key) form.append("model_api_key", key);
      if (provider === "openai") {
        const effort = $("model-reasoning").value;
        if (!["none", "low", "medium", "high", "xhigh", "max"].includes(effort)) throw new ApiError("Choose a supported OpenAI reasoning effort.", 0, "model-reasoning");
        form.append("model_reasoning_effort", effort);
      }
    } finally {
      // Keep the captured key only in this request body, never in UI state or browser storage.
      $("model-api-key").value = "";
    }
  }

  function setSubmitting(submitting) {
    state.submitting = submitting;
    for (const id of ["repository", "source-file", "github-tab", "zip-tab", "start-review"]) $(id).disabled = submitting;
    $("refresh-detail").disabled = submitting || Boolean(state.pollController);
    for (const button of $("recent-runs").querySelectorAll("button")) button.disabled = submitting;
    $("review-form").setAttribute("aria-busy", String(submitting));
    $("start-review").replaceChildren(document.createTextNode(submitting ? "Preparing source…" : "Start review"));
    if (!submitting) $("start-review").append(element("span", "", "→"));
    updateModelControls();
  }

  async function loadHealth() {
    try {
      const health = await request("/api/health");
      state.health = health;
      const ready = health.database === "ready";
      const parts = [ready ? "Review service ready" : "Database unavailable"];
      parts.push(health.provider_configured ? `${providerLabel(health.selected_provider || "AI")} configured${health.selected_model ? ` · ${health.selected_model}` : ""}` : "Local checks available · Server model key not configured");
      parts.push(health.sandbox_enabled ? "Behavioral sandbox enabled" : "Static verification only");
      $("service-status").textContent = parts.join(" / ");
      $("service-status").classList.toggle("unavailable", !ready);
      if (health.max_upload_bytes) $("zip-help").textContent = `ZIP files up to ${formatBytes(health.max_upload_bytes)}. Archive and repository limits are enforced by the server.`;
      notice("provider-notice", typeof health.provider_notice === "string" ? health.provider_notice : "");
      updateModelControls();
    } catch {
      $("service-status").textContent = "Review service unavailable. Start the local service or check its connection.";
      $("service-status").classList.add("unavailable");
    }
  }

  async function loadRecent() {
    state.recentController?.abort();
    const controller = new AbortController();
    state.recentController = controller;
    $("refresh-runs").disabled = true;
    try {
      const result = await request("/api/runs", { signal: controller.signal });
      if (controller.signal.aborted) return;
      const runs = Array.isArray(result) ? result : result.runs || [];
      $("recent-runs").replaceChildren();
      $("recent-message").textContent = runs.length ? "" : "Your reviews will appear here.";
      $("recent-message").hidden = Boolean(runs.length);
      for (const run of runs.slice(0, 30)) {
        if (!run.id) continue;
        const item = element("li");
        const button = element("button", `recent-run${run.id === state.selectedId ? " selected" : ""}`);
        button.type = "button";
        button.dataset.runId = run.id;
        button.disabled = state.submitting;
        if (run.id === state.selectedId) button.setAttribute("aria-current", "true");
        button.append(element("span", "recent-name", sourceName(run)));
        const meta = element("span", "recent-meta");
        meta.append(element("span", `recent-status-dot ${run.status || ""}`));
        meta.append(document.createTextNode(`${STATUS_LABELS[run.status] || humanize(run.status)}${run.created_at ? ` · ${displayDate(run.created_at)}` : ""}`));
        button.append(meta);
        button.addEventListener("click", () => selectRun(run.id));
        item.append(button);
        $("recent-runs").append(item);
      }
    } catch (error) {
      if (controller.signal.aborted) return;
      $("recent-message").hidden = false;
      $("recent-message").textContent = errorMessage(error);
    } finally {
      if (state.recentController === controller) $("refresh-runs").disabled = false;
    }
  }

  function stopPolling() {
    state.generation += 1;
    window.clearTimeout(state.pollTimer);
    state.pollTimer = null;
    state.pollController?.abort();
    state.pollController = null;
    $("activity-dot").classList.remove("busy");
  }

  function selectRun(id, initialRun = null) {
    if (state.submitting) return;
    const sameRun = id === state.selectedId;
    stopPolling();
    state.selectedId = id;
    state.deadline = Date.now() + POLL_LIMIT_MS;
    state.pollErrors = 0;
    $("empty-state").hidden = true;
    $("run-progress").hidden = false;
    $("refresh-detail").textContent = "Refresh report";
    notice("polling-notice", "");
    notice("download-error", "");
    if (!sameRun) {
      state.currentRun = null;
      state.reportKey = "";
      $("report-content").hidden = true;
      $("run-name").textContent = "Loading review…";
      $("run-meta").textContent = `Run ${id}`;
      $("run-detail").textContent = "Retrieving the latest review status…";
      $("run-status").textContent = "Loading";
      $("run-status").className = "pill neutral";
      updateStages("", false);
    }
    for (const button of $("recent-runs").querySelectorAll("button")) {
      const selected = button.dataset.runId === id;
      button.classList.toggle("selected", selected);
      if (selected) button.setAttribute("aria-current", "true");
      else button.removeAttribute("aria-current");
    }
    if (initialRun) renderRun(initialRun);
    fetchRun(state.generation);
  }

  async function fetchRun(generation) {
    if (generation !== state.generation || !state.selectedId) return;
    if (Date.now() >= state.deadline) {
      pausePolling("Live updates paused after 15 minutes. The server may still be working. Resume updates to check the latest status.");
      return;
    }
    const controller = new AbortController();
    state.pollController = controller;
    $("refresh-detail").disabled = true;
    try {
      const run = await request(`/api/runs/${encodeURIComponent(state.selectedId)}`, { signal: controller.signal });
      if (generation !== state.generation) return;
      if (!run || run.id !== state.selectedId) throw new ApiError("The service returned an unexpected review. Refresh the report to try again.");
      state.pollErrors = 0;
      notice("polling-notice", "");
      renderRun(run);
      if (TERMINAL.has(run.status)) {
        $("activity-dot").classList.remove("busy");
        loadRecent();
        return;
      }
      state.pollTimer = window.setTimeout(() => fetchRun(generation), POLL_INTERVAL_MS);
    } catch (error) {
      if (generation !== state.generation || controller.signal.aborted) return;
      state.pollErrors += 1;
      const permanent = error.status >= 400 && error.status < 500;
      if (permanent || state.pollErrors >= MAX_POLL_ERRORS) {
        pausePolling(`${errorMessage(error)} Live updates are paused; resume them when the service is available.`);
      } else {
        notice("polling-notice", `${errorMessage(error)} Retrying updates (${state.pollErrors}/${MAX_POLL_ERRORS}).`);
        state.pollTimer = window.setTimeout(() => fetchRun(generation), POLL_INTERVAL_MS * (state.pollErrors + 1));
      }
    } finally {
      if (generation === state.generation) {
        state.pollController = null;
        $("refresh-detail").disabled = state.submitting;
      }
    }
  }

  function pausePolling(message) {
    window.clearTimeout(state.pollTimer);
    state.pollTimer = null;
    $("activity-dot").classList.remove("busy");
    $("refresh-detail").textContent = "Resume updates";
    notice("polling-notice", message);
  }

  function updateStages(stage, complete) {
    const normalized = String(stage || "").toLowerCase();
    let index = 0;
    if (/analy|baseline|inventory|retriev|index/.test(normalized)) index = 1;
    if (/repair|fix|propos|remediat|plan|reason/.test(normalized)) index = 2;
    if (/valid|verif|test|check/.test(normalized)) index = 3;
    if (/report|finish|complete/.test(normalized)) index = 4;
    const active = Boolean(stage);
    $("run-progress").querySelectorAll(".pipeline li").forEach((item, position) => {
      item.classList.toggle("complete", complete || (active && position < index));
      item.classList.toggle("current", !complete && active && position === index);
      if (!complete && active && position === index) item.setAttribute("aria-current", "step");
      else item.removeAttribute("aria-current");
    });
  }

  function renderRun(run) {
    state.currentRun = run;
    $("run-name").textContent = sourceName(run);
    $("run-meta").textContent = `Run ${String(run.id).slice(0, 8)}${run.created_at ? ` · ${displayDate(run.created_at)}` : ""}`;
    $("run-status").textContent = STATUS_LABELS[run.status] || humanize(run.status);
    $("run-status").className = `pill ${tone(run.status)}`;
    const finished = TERMINAL.has(run.status);
    $("activity-dot").classList.toggle("busy", !finished);
    $("run-detail").textContent = stringify(run.detail) || (finished ? "Review finished. Inspect the findings and verification evidence below." : `${humanize(run.stage || run.status)}…`);
    updateStages(run.stage || run.status, finished && ["improved", "reviewed"].includes(run.status));
    if (run.report && typeof run.report === "object") {
      const key = JSON.stringify(run.report);
      if (key !== state.reportKey) {
        renderReport(run.report, run);
        state.reportKey = key;
      }
    } else if (finished) {
      $("report-content").hidden = true;
      notice("polling-notice", "This review ended before a report was produced. The status above explains the outcome.");
    }
  }

  function numberMetric(metrics, key, fallback) {
    const value = metrics?.[key];
    return typeof value === "number" || typeof value === "string" ? value : fallback;
  }

  function renderMetrics(report, findings) {
    const metrics = report.metrics || {};
    const count = (statuses) => findings.filter((finding) => statuses.includes(finding.status)).length;
    const applied = count(["applied", "accepted", "resolved", "fixed"]);
    const attempted = findings.reduce((sum, finding) => sum + (Number(finding.attempts) || 0), 0);
    const unresolved = count(["unresolved", "needs_review", "not_attempted", "pending", "skipped", "rejected", "attempted"]);
    const items = [
      ["Findings", numberMetric(metrics, "findings", findings.length), "Prioritized review items"],
      ["Applied fixes", numberMetric(metrics, "accepted_changes", numberMetric(metrics, "applied", numberMetric(metrics, "accepted", applied))), "Accepted after verification"],
      ["Attempts", numberMetric(metrics, "attempts", attempted), "Bounded remediation attempts"],
      ["Unresolved", numberMetric(metrics, "unresolved", unresolved), "Require further review"],
    ];
    $("report-metrics").replaceChildren();
    for (const [label, value, detail] of items) {
      const card = element("dl", "metric-card");
      card.append(element("dt", "", label), element("dd", "", value), element("p", "", detail));
      $("report-metrics").append(card);
    }
  }

  function renderFinding(finding, actions) {
    const item = element("article", "finding");
    item.append(element("span", "finding-icon", ["applied", "resolved", "accepted", "fixed"].includes(finding.status) ? "✓" : "!"));
    const content = element("div");
    const heading = element("div", "finding-heading");
    heading.append(element("h4", "", finding.message || finding.title || finding.rule || "Review finding"));
    const tags = element("div", "finding-tags");
    if (finding.severity) tags.append(badge(finding.severity, `${humanize(finding.severity)} severity`));
    const status = finding.status || "unresolved";
    const disposition = badge(status);
    if (["needs_review", "not_attempted"].includes(status)) disposition.title = humanize(status);
    tags.append(disposition);
    if (Number(finding.attempts) > 0) tags.append(badge("attempted", `${finding.attempts} attempt${Number(finding.attempts) === 1 ? "" : "s"}`));
    heading.append(tags);
    content.append(heading);
    const location = [finding.path || finding.file, finding.line ? `line ${finding.line}` : null, finding.rule].filter(Boolean).join(" · ");
    if (location) content.append(element("p", "finding-location", location));
    if (finding.rationale) {
      const rationale = element("p", "finding-body");
      rationale.append(element("strong", "", "Rationale: "), document.createTextNode(stringify(finding.rationale)));
      content.append(rationale);
    }
    const related = actions.filter((action) => typeof action === "object" && action !== null && [action.finding_id, action.id, action.finding?.id].includes(finding.id));
    const evidence = finding.evidence || finding.validation || finding.details;
    if (evidence) content.append(detailBlock("Finding evidence", evidence));
    if (related.length) content.append(detailBlock("Action history", related));
    item.append(content);
    return item;
  }

  function detailBlock(title, value) {
    const detail = element("details", "finding-detail");
    detail.append(element("summary", "", title), element("pre", "evidence-block", stringify(value)));
    return detail;
  }

  function validationStatus(value) {
    if (value === true) return "passed";
    if (value === false) return "failed_check";
    if (typeof value === "string") return value.toLowerCase();
    if (!value || typeof value !== "object") return "unavailable";
    if (value.status) return validationStatus(value.status);
    if (value.passed !== undefined) return validationStatus(value.passed);
    if (value.success !== undefined) return validationStatus(value.success);
    if (value.ok !== undefined) return validationStatus(value.ok);
    if (value.returncode !== undefined) return value.returncode === 0 ? "passed" : "failed_check";
    if (value.skipped === true) return "skipped";
    return "recorded";
  }

  function renderValidation(check, index) {
    const value = typeof check === "object" && check !== null ? check : { detail: check };
    const name = value.name || value.check || value.tool || value.engine || value.kind || value.type || `Validation ${index + 1}`;
    const category = value.category || value.kind || value.coverage;
    const behavioral = /behav|test|sandbox|unittest/i.test(`${name} ${stringify(category)}`);
    const card = element("article", "validation-card");
    card.append(element("span", "check-label", behavioral ? "Behavioral evidence" : /static|syntax|lint|ruff|ast/i.test(`${name} ${stringify(category)}`) ? "Static evidence" : "Verification record"));
    const header = element("div", "validation-card-header");
    header.append(element("h4", "", humanize(name)));
    const status = validationStatus(value);
    const label = name === "baseline_lint" && status === "passed" ? "Baseline established" : status === "recorded" ? "Recorded" : STATUS_LABELS[status] || humanize(status);
    header.append(badge(name === "baseline_lint" && status === "passed" ? "neutral" : status, label));
    card.append(header);
    const description = value.summary || value.detail || value.reason || value.message || (typeof value.details === "string" ? value.details : "");
    if (description) card.append(element("p", "", stringify(description)));
    const counts = [];
    if (typeof value.files === "number") counts.push(`${value.files} source files`);
    if (typeof value.findings === "number") counts.push(`${value.findings} existing findings`);
    if (typeof value.skipped === "number" && value.skipped > 0) counts.push(`${value.skipped} skipped tests`);
    if (counts.length) card.append(element("p", "", counts.join(" · ")));
    if (value.phase) card.append(element("p", "", `Phase: ${humanize(value.phase)}${typeof value.tests === "number" ? ` · ${value.tests} tests` : ""}`));
    if (value.before !== undefined || value.after !== undefined || value.baseline !== undefined || value.candidate !== undefined) {
      const phases = element("div", "validation-phases");
      for (const [label, result] of [["Before", value.before ?? value.baseline], ["After", value.after ?? value.candidate]]) {
        if (result === undefined) continue;
        phases.append(element("span", "phase-label", label), badge(validationStatus(result)));
      }
      card.append(phases);
    }
    card.append(detailBlock("Inspect evidence", value));
    return card;
  }

  function renderProse(id, panelId, value) {
    const values = asArray(value);
    $(id).replaceChildren();
    $(panelId).hidden = !values.length;
    if (!values.length) return;
    for (const item of values) $(id).append(typeof item === "object" ? detailBlock(item.name || item.action || item.type || "Record", item) : element("p", "", item));
  }

  function renderActions(actions, metrics) {
    $("report-actions").replaceChildren();
    $("actions-panel").hidden = !actions.length && !Object.keys(metrics).length;
    for (const [index, action] of actions.entries()) {
      if (typeof action !== "object" || action === null) {
        $("report-actions").append(element("p", "", stringify(action)));
        continue;
      }
      const card = element("article", "action-record");
      const heading = element("div", "action-heading");
      heading.append(element("h4", "", `${action.path || `Action ${index + 1}`}${action.attempt ? ` · Attempt ${action.attempt}` : ""}`));
      const badges = element("div", "action-badges");
      if (typeof action.accepted === "boolean") badges.append(badge(action.accepted ? "applied" : "rejected"));
      if (action.method) badges.append(badge("neutral", humanize(action.method)));
      heading.append(badges);
      card.append(heading);
      if (action.rationale) card.append(element("p", "", stringify(action.rationale)));
      if (action.rolled_back) card.append(element("p", "action-rollback", "The candidate change was rolled back."));
      card.append(detailBlock("Inspect action record", action));
      $("report-actions").append(card);
    }
    if (Object.keys(metrics).length) $("report-actions").append(detailBlock("Run limits and metrics", metrics));
  }

  function renderProviderMetadata(run) {
    const metadata = run.source?.provider;
    $("report-provider").replaceChildren();
    $("report-provider").hidden = !metadata || typeof metadata !== "object";
    if (!metadata || typeof metadata !== "object") return;
    const credentials = { byok: "Own key · This review", environment: "Server environment", none: "No provider key" };
    const items = [
      ["Provider", metadata.provider ? providerLabel(metadata.provider) : "Not recorded"],
      ["Model", metadata.model || "Not recorded"],
      ["Reasoning effort", metadata.reasoning_effort ? humanize(metadata.reasoning_effort) : "Not applicable"],
      ["Credential source", credentials[metadata.credential_source] || "Not recorded"],
    ];
    for (const [label, value] of items) {
      const entry = element("dl");
      entry.append(element("dt", "", label), element("dd", "", value));
      $("report-provider").append(entry);
    }
  }

  function renderReport(report, run) {
    $("report-content").hidden = false;
    const titles = { improved: "Verified improvements, ready for review.", reviewed: "Review complete.", stopped: "Review stopped at a guardrail.", failed: "Review ended with an error." };
    $("report-title").textContent = titles[report.status || run.status] || "Review evidence";
    $("report-summary").textContent = stringify(report.summary) || "Inspect the findings and validation evidence for this review.";
    renderProviderMetadata(run);
    const findings = asArray(report.findings).map((value) => typeof value === "object" && value !== null ? value : { message: value });
    const actions = asArray(report.actions);
    renderMetrics(report, findings);
    notice("stop-reason", report.stop_reason ? `Stop reason: ${stringify(report.stop_reason)}` : "");
    $("findings-count").textContent = `${findings.length} item${findings.length === 1 ? "" : "s"}`;
    $("findings").replaceChildren();
    if (!findings.length) $("findings").append(element("p", "empty-panel", "No findings were recorded within this review’s supported checks. See the coverage and limitations below."));
    for (const finding of findings) $("findings").append(renderFinding(finding, actions));
    renderActions(actions, report.metrics || {});
    const validations = asArray(report.validation || report.validations);
    $("validations").replaceChildren();
    if (!validations.length) $("validations").append(element("p", "empty-panel", "No validation evidence was recorded. Behavioral correctness has not been established."));
    for (const [index, check] of validations.entries()) $("validations").append(renderValidation(check, index));
    renderProse("report-rationale", "rationale-panel", report.rationale || report.reasoning);
    renderProse("report-limitations", "limitations-panel", report.limitations);
    $("download-patch").disabled = !report.patch;
    $("download-patch").title = report.patch ? "Download the changes accepted during this review" : "No accepted changes are available in this review";
  }

  async function submitReview(event) {
    event.preventDefault();
    if (state.submitting) return;
    notice("form-error", "");
    const form = new FormData();
    try {
      captureModelSettings(form);
    } catch (error) {
      notice("form-error", errorMessage(error));
      $("model-settings").open = true;
      if (error.fieldId) $(error.fieldId).focus();
      return;
    }
    if (state.source === "github") {
      const repository = $("repository").value.trim();
      if (!repository) { notice("form-error", "Enter a public GitHub repository URL or owner/repository name."); $("repository").focus(); return; }
      if (repository.length > 300) { notice("form-error", "The repository reference is too long. Use its GitHub URL or owner/repository name."); $("repository").focus(); return; }
      form.append("repository", repository);
    } else {
      if (!state.file) { notice("form-error", "Choose a source code ZIP to review."); $("source-file").focus(); return; }
      if (!state.file.name.toLowerCase().endsWith(".zip")) { notice("form-error", "Choose a .zip archive containing the source code."); $("source-file").focus(); return; }
      if (state.health?.max_upload_bytes && state.file.size > state.health.max_upload_bytes) { notice("form-error", `The ZIP exceeds this server’s ${formatBytes(state.health.max_upload_bytes)} upload limit.`); $("source-file").focus(); return; }
      form.append("file", state.file, state.file.name);
    }
    setSubmitting(true);
    try {
      const run = await request("/api/runs", { method: "POST", body: form, timeout: 120000 });
      if (!run?.id) throw new ApiError("The service did not return a review ID. Refresh recent reviews before submitting again.");
      setSubmitting(false);
      selectRun(run.id, run);
      loadRecent();
      $("review-title").setAttribute("tabindex", "-1");
      $("review-title").focus({ preventScroll: true });
      $("review-section").scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "start" });
    } catch (error) {
      const message = errorMessage(error);
      notice("form-error", `${message}${error.message === "Request timed out" ? " A review may already have been created; refresh recent reviews before submitting again." : ""}`);
    } finally {
      $("model-api-key").value = "";
      setSubmitting(false);
    }
  }

  async function download(kind, button) {
    const runId = state.selectedId;
    if (!runId) return;
    const suffix = kind === "patch" ? "/patch" : `/report?format=${kind}`;
    const extension = kind === "markdown" ? "md" : kind;
    button.disabled = true;
    notice("download-error", "");
    try {
      const blob = await request(`/api/runs/${encodeURIComponent(runId)}${suffix}`, { responseType: "blob" });
      const url = URL.createObjectURL(blob);
      const anchor = element("a");
      anchor.href = url;
      anchor.download = `codeproof-${runId}.${extension}`;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      if (state.selectedId === runId) notice("download-error", errorMessage(error));
    } finally {
      button.disabled = kind === "patch" && !state.currentRun?.report?.patch;
    }
  }

  for (const source of ["github", "zip"]) {
    $(`${source}-tab`).addEventListener("click", () => setSource(source));
    $(`${source}-tab`).addEventListener("keydown", (event) => {
      if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        setSource(event.key === "Home" ? "github" : event.key === "End" ? "zip" : state.source === "github" ? "zip" : "github", true);
      }
    });
  }
  $("source-file").addEventListener("change", (event) => {
    const file = event.target.files[0];
    if (file) setFile(file);
  });
  $("upload-zone").addEventListener("dragover", (event) => { event.preventDefault(); if (!state.submitting) $("upload-zone").classList.add("dragover"); });
  $("upload-zone").addEventListener("dragleave", () => $("upload-zone").classList.remove("dragover"));
  $("upload-zone").addEventListener("drop", (event) => {
    event.preventDefault();
    $("upload-zone").classList.remove("dragover");
    if (state.submitting) return;
    const files = event.dataTransfer.files;
    if (files.length !== 1) { notice("form-error", "Upload one source code ZIP at a time."); return; }
    setFile(files[0]);
  });
  $("review-form").addEventListener("submit", submitReview);
  $("model-provider").addEventListener("change", () => {
    $("model-api-key").value = "";
    $("model-name").value = "";
    $("model-reasoning").value = "medium";
    updateModelControls();
    notice("form-error", "");
  });
  $("refresh-runs").addEventListener("click", loadRecent);
  $("refresh-detail").addEventListener("click", () => { if (state.selectedId) selectRun(state.selectedId); });
  $("api-token").addEventListener("change", () => { loadRecent(); if (state.selectedId && !state.submitting) selectRun(state.selectedId); });
  $("download-patch").addEventListener("click", (event) => download("patch", event.currentTarget));
  $("download-markdown").addEventListener("click", (event) => download("markdown", event.currentTarget));
  $("download-html").addEventListener("click", (event) => download("html", event.currentTarget));
  $("download-json").addEventListener("click", (event) => download("json", event.currentTarget));
  window.addEventListener("pagehide", () => { $("model-api-key").value = ""; stopPolling(); state.recentController?.abort(); });
  updateModelControls();
  loadHealth();
  loadRecent();
})();
