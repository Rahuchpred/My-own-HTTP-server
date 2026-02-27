const MAX_TIMELINE = 120
const MAX_HISTORY_ITEMS = 80

const appState = {
  authState: "checking",
  authRole: "",
  streamState: "connecting",
  streamTransport: "sse",
  diagnostics: [],
  lastEventId: 0,
  eventSource: null,
  reconnectTimer: null,
  fallbackTimer: null,
  reconnectAttempts: 0,
  eventIndex: new Map(),
  timeline: [],
  latestRequest: null,
  latestProxy: null,
  latestRun: null,
  latestMissionLatency: null,
  lastRecoveryMs: null,
  missionLifecycle: "idle",
  liveMetricsUpdatedAt: 0,
  scenarios: [],
  targets: [],
  history: [],
  incident: null,
  metrics: {},
  trendsSummary: {},
  activePreset: null,
  liveMode: null,
}

const els = {
  engineBadge: document.getElementById("engineBadge"),
  authBadge: document.getElementById("authBadge"),
  eventsBadge: document.getElementById("eventsBadge"),
  healthBadge: document.getElementById("healthBadge"),
  cursorBadge: document.getElementById("cursorBadge"),
  currentRoleText: document.getElementById("currentRoleText"),
  switchRoleBtn: document.getElementById("switchRoleBtn"),
  enableLiveModeBtn: document.getElementById("enableLiveModeBtn"),
  liveModeHint: document.getElementById("liveModeHint"),

  incidentMode: document.getElementById("incidentMode"),
  incidentProbability: document.getElementById("incidentProbability"),
  incidentLatency: document.getElementById("incidentLatency"),
  startIncidentBtn: document.getElementById("startIncidentBtn"),
  stopIncidentBtn: document.getElementById("stopIncidentBtn"),
  incidentState: document.getElementById("incidentState"),
  presetHint: document.getElementById("presetHint"),
  runPresetBtn: document.getElementById("runPresetBtn"),

  verdictCard: document.getElementById("verdictCard"),
  analysisPanel: document.getElementById("analysisPanel"),
  narrativePanel: document.getElementById("narrativePanel"),

  timelineList: document.getElementById("timelineList"),
  eventDetail: document.getElementById("eventDetail"),
  serviceMap: document.getElementById("serviceMap"),
  diagnosticsPanel: document.getElementById("diagnosticsPanel"),

  liveScoreLatency: document.getElementById("liveScoreLatency"),
  liveScoreBurn: document.getElementById("liveScoreBurn"),
  liveScoreUpdated: document.getElementById("liveScoreUpdated"),
  missionScoreStatus: document.getElementById("missionScoreStatus"),
  missionScoreReliability: document.getElementById("missionScoreReliability"),
  missionScoreLatency: document.getElementById("missionScoreLatency"),
  scorePassRate: document.getElementById("scorePassRate"),
  scoreMttr: document.getElementById("scoreMttr"),
  scoreIncident: document.getElementById("scoreIncident"),
  comparisonPanel: document.getElementById("comparisonPanel"),

  reqMethod: document.getElementById("reqMethod"),
  reqTarget: document.getElementById("reqTarget"),
  reqPath: document.getElementById("reqPath"),
  reqHeaders: document.getElementById("reqHeaders"),
  reqBody: document.getElementById("reqBody"),
  sendBtn: document.getElementById("sendBtn"),

  respStats: document.getElementById("respStats"),
  respHeaders: document.getElementById("respHeaders"),
  respBody: document.getElementById("respBody"),

  scenarioSelect: document.getElementById("scenarioSelect"),
  scenarioSeed: document.getElementById("scenarioSeed"),
  runScenarioBtn: document.getElementById("runScenarioBtn"),
  scenarioRunResult: document.getElementById("scenarioRunResult"),

  mockMethod: document.getElementById("mockMethod"),
  mockPath: document.getElementById("mockPath"),
  mockStatus: document.getElementById("mockStatus"),
  mockBody: document.getElementById("mockBody"),
  createMockBtn: document.getElementById("createMockBtn"),
  refreshBtn: document.getElementById("refreshBtn"),
  mocksList: document.getElementById("mocksList"),

  targetName: document.getElementById("targetName"),
  targetBaseUrl: document.getElementById("targetBaseUrl"),
  targetTimeout: document.getElementById("targetTimeout"),
  createTargetBtn: document.getElementById("createTargetBtn"),
  refreshTargetsBtn: document.getElementById("refreshTargetsBtn"),
  targetsList: document.getElementById("targetsList"),

  historyFilter: document.getElementById("historyFilter"),
  filterBtn: document.getElementById("filterBtn"),
  historyList: document.getElementById("historyList"),

  tokenModal: document.getElementById("tokenModal"),
  usernameInput: document.getElementById("usernameInput"),
  passwordInput: document.getElementById("passwordInput"),
  loginBtn: document.getElementById("loginBtn"),
  tokenInput: document.getElementById("tokenInput"),
  saveTokenBtn: document.getElementById("saveTokenBtn"),
  clearTokenBtn: document.getElementById("clearTokenBtn"),
}

const MISSION_PRESETS = {
  signup_chaos: {
    label: "User Signup Under Chaos",
    scenarioName: "Mission - User Signup Chaos",
    seed: 1337,
    mocks: [
      {
        method: "POST",
        path_pattern: "/api/users",
        status: 201,
        headers: { "X-Mock-Server": "mission" },
        body: '{"id":101,"name":"Demo User","result":"created"}',
        content_type: "application/json",
      },
    ],
    scenario: {
      description: "Validates signup flow with deterministic chaos enabled.",
      defaults: { stop_on_fail: true },
      steps: [
        {
          id: "create-user",
          method: "POST",
          path: "/api/users",
          body: '{"name":"Neo"}',
          chaos: {
            enabled: true,
            mode: "status_flip",
            probability: 0.25,
            seed_offset: 11,
          },
          expect: {
            status: [200, 201],
            body_contains: ["id"],
          },
        },
        {
          id: "health-check",
          method: "GET",
          path: "/",
          expect: {
            status: 200,
            body_contains: ["Hello World"],
          },
        },
      ],
    },
  },
  checkout_latency: {
    label: "Checkout Latency Investigation",
    scenarioName: "Mission - Checkout Latency",
    seed: 2026,
    incidentStart: {
      mode: "fixed_latency",
      probability: 1.0,
      latency_ms: 900,
      seed: 2026,
    },
    dualRunRecovery: true,
    mocks: [
      {
        method: "GET",
        path_pattern: "/checkout",
        status: 200,
        headers: { "X-Mock-Server": "mission" },
        body: '{"checkout":"ok","items":3}',
        content_type: "application/json",
      },
    ],
    scenario: {
      description: "Investigates slow checkout route and surfaces bottleneck diagnostics.",
      defaults: { stop_on_fail: true },
      steps: [
        {
          id: "checkout-route",
          method: "GET",
          path: "/checkout",
          expect: {
            status: 200,
            body_contains: ["checkout"],
            max_latency_ms: 450,
          },
        },
      ],
    },
  },
  upstream_outage: {
    label: "Upstream Outage Simulation",
    scenarioName: "Mission - Upstream Outage",
    seed: 42,
    incidentStart: {
      mode: "status_spike_503",
      probability: 1.0,
      latency_ms: 0,
      seed: 42,
    },
    dualRunRecovery: true,
    scenario: {
      description: "Forces outage, demonstrates failure impact, then verifies recovery.",
      defaults: { stop_on_fail: true },
      steps: [
        {
          id: "home-availability",
          method: "GET",
          path: "/",
          expect: {
            status: 200,
            body_contains: ["Hello World"],
          },
        },
      ],
    },
  },
}

const DEMO_PRESETS = {
  beginner: {
    label: "Beginner",
    mission: "signup_chaos",
    incident: { mode: "fixed_latency", probability: 0.0, latency_ms: 120 },
    hint: "Low-chaos mode for clean walkthroughs and stable outputs.",
  },
  ops: {
    label: "Ops",
    mission: "checkout_latency",
    incident: { mode: "fixed_latency", probability: 0.25, latency_ms: 600 },
    hint: "Balanced observability mode with moderate latency and clear diagnostics.",
  },
  chaos: {
    label: "Chaos",
    mission: "upstream_outage",
    incident: { mode: "status_spike_503", probability: 0.45, latency_ms: 0 },
    hint: "Stress mode for outage and recovery storytelling.",
  },
}

function getToken() {
  return window.localStorage.getItem("authToken") || window.localStorage.getItem("demoToken") || ""
}

function setToken(token) {
  if (!token) {
    window.localStorage.removeItem("authToken")
    window.localStorage.removeItem("demoToken")
    return
  }
  window.localStorage.setItem("authToken", token)
}

function withAuthHeaders(headers = {}) {
  const token = getToken()
  if (!token) {
    return { ...headers }
  }
  return {
    ...headers,
    Authorization: `Bearer ${token}`,
  }
}

function addDiagnostic(message, level = "info") {
  const entry = `${new Date().toISOString()} [${level}] ${message}`
  appState.diagnostics.unshift(entry)
  appState.diagnostics = appState.diagnostics.slice(0, 20)
  els.diagnosticsPanel.textContent = appState.diagnostics.join("\n")
}

function setBadge(element, text, kind = "warn") {
  element.textContent = text
  element.classList.remove("ok", "warn", "bad")
  element.classList.add(kind)
}

function showTokenModal(show = true) {
  els.tokenModal.classList.toggle("hidden", !show)
  if (show) {
    els.tokenInput.value = getToken()
  }
}

function setLiveModeHint(text) {
  if (!els.liveModeHint) return
  els.liveModeHint.textContent = text
}

function roleRank(role) {
  if (role === "admin") return 3
  if (role === "operator") return 2
  if (role === "viewer") return 1
  return 0
}

function setVerdictCard(tone, title, subtitle) {
  const toneClass = tone === "pass" || tone === "fail" ? tone : "neutral"
  els.verdictCard.classList.remove("pass", "fail", "neutral")
  els.verdictCard.classList.add(toneClass)
  els.verdictCard.innerHTML = `<div class="verdict-title">${title}</div><div class="verdict-sub">${subtitle}</div>`
}

function missionStatusLabel() {
  const state = appState.missionLifecycle
  if (state === "running") return "RUNNING"
  if (state === "blocked_role") return "BLOCKED"
  if (state === "setup_failed") return "SETUP_FAILED"
  if (state === "completed_pass") return "PASS"
  if (state === "completed_fail") return "FAIL"
  return "No mission run yet"
}

function percentileFromSorted(values, percentile) {
  if (!values.length) {
    return 0
  }
  if (values.length === 1) {
    return Number(values[0]) || 0
  }
  const rank = (percentile / 100) * (values.length - 1)
  const lower = Math.floor(rank)
  const upper = Math.ceil(rank)
  if (lower === upper) {
    return Number(values[lower]) || 0
  }
  const weight = rank - lower
  return (Number(values[lower]) || 0) * (1 - weight) + (Number(values[upper]) || 0) * weight
}

function summarizeMissionLatency(run) {
  const latencies = (run.step_results || [])
    .map((step) => Number(step.latency_ms || 0))
    .filter((value) => Number.isFinite(value) && value >= 0)
    .sort((a, b) => a - b)
  if (!latencies.length) {
    return null
  }
  return {
    p50: percentileFromSorted(latencies, 50),
    p95: percentileFromSorted(latencies, 95),
    p99: percentileFromSorted(latencies, 99),
  }
}

function missionRequiresAdminBootstrap(mission) {
  return Boolean((mission.mocks && mission.mocks.length > 0) || mission.scenario)
}

function formatMissionError(error) {
  const raw = String(error || "mission execution failed")
  if (raw.includes("(403)")) {
    return "403 forbidden (role restriction). Switch Role -> login as admin -> rerun preset."
  }
  if (raw.includes("(401)")) {
    return "401 unauthorized. Login again and rerun mission."
  }
  return raw
}

function setMissionLifecycle(state, options = {}) {
  appState.missionLifecycle = state

  if (state === "idle") {
    setVerdictCard(
      "neutral",
      "No mission run yet.",
      options.subtitle || "Run a mission to generate pass/fail conclusion and root cause.",
    )
    if (!options.preservePanels) {
      els.analysisPanel.textContent = "analysis: -"
      els.narrativePanel.textContent = "No report yet."
    }
  } else if (state === "running") {
    appState.latestRun = null
    appState.latestMissionLatency = null
    setVerdictCard(
      "neutral",
      `Running ${options.missionLabel || "mission"}`,
      options.subtitle || "Preparing mocks, scenario, and incident profile...",
    )
    els.analysisPanel.textContent = "analysis: mission running..."
    els.narrativePanel.textContent = "Mission in progress..."
  } else if (state === "blocked_role") {
    appState.latestRun = null
    appState.latestMissionLatency = null
    setVerdictCard(
      "fail",
      "Mission blocked",
      options.subtitle || "This mission needs admin role to prepare mocks/scenarios. Click Switch Role.",
    )
    els.analysisPanel.textContent = "analysis: state=blocked_role"
    els.narrativePanel.textContent = "Guided action: Switch Role -> login as admin -> rerun preset."
  } else if (state === "setup_failed") {
    appState.latestRun = null
    appState.latestMissionLatency = null
    setVerdictCard("fail", options.title || "Mission setup failed", options.subtitle || "Mission setup failed.")
    els.analysisPanel.textContent = "analysis: state=setup_failed"
    els.narrativePanel.textContent = options.subtitle || "Mission setup failed."
  }

  renderScoreboard()
}

function noteFaultChangedIfNoMission() {
  if (appState.missionLifecycle !== "idle") {
    return
  }
  setMissionLifecycle("idle", {
    subtitle: "Fault changed. Run a mission to evaluate impact.",
  })
}

function updateControlPermissions() {
  const rank = roleRank(appState.authRole)
  const canOperate = rank >= 2
  const canAdmin = rank >= 3

  document.querySelectorAll("[data-mission]").forEach((button) => {
    button.disabled = !canOperate
  })
  document.querySelectorAll("[data-preset]").forEach((button) => {
    button.disabled = !canOperate
  })

  if (els.startIncidentBtn) els.startIncidentBtn.disabled = !canOperate
  if (els.stopIncidentBtn) els.stopIncidentBtn.disabled = !canOperate
  if (els.sendBtn) els.sendBtn.disabled = !canOperate
  if (els.runScenarioBtn) els.runScenarioBtn.disabled = !canOperate
  if (els.runPresetBtn) els.runPresetBtn.disabled = !canOperate

  if (els.createMockBtn) els.createMockBtn.disabled = !canAdmin
  if (els.createTargetBtn) els.createTargetBtn.disabled = !canAdmin
}

function updateStatusBadges() {
  const authKind = appState.authState === "connected" ? "ok" : appState.authState === "checking" ? "warn" : "bad"
  const streamKind =
    appState.streamState === "connected"
      ? "ok"
      : appState.streamState === "reconnecting" || appState.streamState === "fallback"
        ? "warn"
        : "bad"
  const healthKind = appState.incident && appState.incident.active ? "bad" : "ok"

  const authLabel = appState.authRole ? `${appState.authState}(${appState.authRole})` : appState.authState
  setBadge(els.authBadge, `auth: ${authLabel}`, authKind)
  setBadge(els.eventsBadge, `events: ${appState.streamState}`, streamKind)
  setBadge(els.healthBadge, `health: ${appState.incident && appState.incident.active ? "degraded" : "healthy"}`, healthKind)
  if (els.currentRoleText) {
    els.currentRoleText.textContent = `Current role: ${appState.authRole || "guest"}`
  }
}

function setAuthState(nextState) {
  if (appState.authState === nextState) {
    return
  }
  appState.authState = nextState
  updateStatusBadges()
  updateControlPermissions()
  if (nextState === "unauthorized") {
    appState.authRole = ""
    addDiagnostic("Protected APIs returned 401. Login or token is required.", "warn")
    showTokenModal(true)
  }
}

async function fetchJson(path, options = {}) {
  const merged = { ...options }
  merged.headers = withAuthHeaders(options.headers || {})

  let response
  try {
    response = await fetch(path, merged)
  } catch (error) {
    addDiagnostic(`Network error for ${path}: ${String(error)}`, "error")
    return { ok: false, status: 0, data: { error: { code: "network_error", message: String(error) } } }
  }

  const text = await response.text()
  let data = {}
  if (text) {
    try {
      data = JSON.parse(text)
    } catch (_err) {
      data = { raw: text }
    }
  }

  if (response.status === 401) {
    setAuthState("unauthorized")
  }

  return { ok: response.ok, status: response.status, data }
}

function parseJsonSafe(text, fallback = {}) {
  try {
    return JSON.parse(text || "{}")
  } catch (_err) {
    return fallback
  }
}

function renderResponse(result, statusCode, latencyMs) {
  const bytes = JSON.stringify(result || {}).length
  els.respStats.innerHTML = `<span>Status:${statusCode}</span><span>Latency:${latencyMs}ms</span><span>Bytes:${bytes}</span>`
  els.respHeaders.textContent = JSON.stringify(result.headers || {}, null, 2)
  const body = result.body
  els.respBody.textContent = typeof body === "string" ? body : JSON.stringify(result, null, 2)
}

function renderMocks(mocks) {
  els.mocksList.innerHTML = ""
  for (const mock of mocks) {
    const item = document.createElement("li")
    item.innerHTML = `<div>${mock.method} ${mock.path_pattern} -> ${mock.status}</div><small>id:${mock.id}</small><div class="row"><button data-delete-mock="${mock.id}" class="muted">Delete</button></div>`
    els.mocksList.appendChild(item)
  }
}

function renderTargets(targets) {
  appState.targets = targets
  els.targetsList.innerHTML = ""
  const options = ['<option value="local">local</option>']
  const selected = els.reqTarget.value || "local"

  for (const target of targets) {
    options.push(`<option value="${target.id}">${target.name}</option>`)
    const item = document.createElement("li")
    item.innerHTML = `<div>${target.name}</div><small>${target.base_url}</small><small>timeout:${target.timeout_ms}ms</small><div class="row"><button data-delete-target="${target.id}" class="muted">Delete</button></div>`
    els.targetsList.appendChild(item)
  }

  els.reqTarget.innerHTML = options.join("")
  const stillExists = selected === "local" || targets.some((target) => String(target.id) === selected)
  els.reqTarget.value = stillExists ? selected : "local"
}

function renderScenarios(scenarios) {
  appState.scenarios = scenarios
  const selected = els.scenarioSelect.value
  const options = scenarios.map((scenario) => `<option value="${scenario.id}">${scenario.name} (${(scenario.steps || []).length})</option>`)
  els.scenarioSelect.innerHTML = options.length ? options.join("") : '<option value="">No scenarios yet</option>'
  if (scenarios.some((item) => String(item.id) === selected)) {
    els.scenarioSelect.value = selected
  }
}

function renderHistory(history) {
  appState.history = history
  const filter = els.historyFilter.value.trim()
  els.historyList.innerHTML = ""

  for (const item of history.slice().reverse().slice(0, MAX_HISTORY_ITEMS)) {
    if (filter && !String(item.path || "").includes(filter)) {
      continue
    }
    const node = document.createElement("li")
    node.innerHTML = `<div>${item.status} ${item.method} ${item.path}</div><small>${item.ts} | ${item.latency_ms}ms | trace ${item.trace_id}</small><div class="row"><button data-replay="${item.request_id}" class="muted">Replay</button></div>`
    els.historyList.appendChild(node)
  }
}

function updateCursor(eventId) {
  appState.lastEventId = Math.max(appState.lastEventId, Number(eventId || 0))
  els.cursorBadge.textContent = `cursor: ${appState.lastEventId}`
}

function updateServiceMap() {
  const lines = []
  lines.push("client -> custom-http-server")
  if (appState.latestProxy) {
    lines.push(`custom-http-server -> target:${appState.latestProxy.target_id} (${appState.latestProxy.status})`)
  } else {
    lines.push("custom-http-server -> local-route")
  }
  if (appState.latestRequest) {
    lines.push(`route: ${appState.latestRequest.method} ${appState.latestRequest.path} (${appState.latestRequest.status})`)
  }
  lines.push(`state: ${appState.incident && appState.incident.active ? "degraded" : "healthy"}`)
  els.serviceMap.textContent = lines.join("\n")
}

function pushTimeline(event, cssClass = "") {
  const item = {
    id: Number(event.id || 0),
    type: String(event.type || "unknown"),
    ts: String(event.ts || ""),
    trace_id: String(event.trace_id || ""),
    payload: event.payload || {},
    cssClass,
  }
  appState.timeline.unshift(item)
  appState.timeline = appState.timeline.slice(0, MAX_TIMELINE)
  appState.eventIndex.set(item.id, item)
  renderTimeline()
}

function renderTimeline() {
  els.timelineList.innerHTML = ""
  for (const item of appState.timeline) {
    const li = document.createElement("li")
    li.dataset.eventId = String(item.id)
    if (item.cssClass) {
      li.classList.add(item.cssClass)
    }
    const step = item.payload.step_id ? ` | step=${item.payload.step_id}` : ""
    const status = item.payload.status ? ` | status=${item.payload.status}` : ""
    li.innerHTML = `<div>${item.type}</div><small>${item.ts}${step}${status}</small>`
    els.timelineList.appendChild(li)
  }
}

function showEventDetail(eventId) {
  const record = appState.eventIndex.get(Number(eventId))
  if (!record) {
    return
  }
  const summary = []
  summary.push(`event_id: ${record.id}`)
  summary.push(`type: ${record.type}`)
  if (record.trace_id) {
    summary.push(`trace_id: ${record.trace_id}`)
  }
  if (record.payload && record.payload.failure_reason) {
    summary.push(`failure_reason: ${record.payload.failure_reason}`)
  }
  if (record.type === "scenario.step.completed" && record.payload && record.payload.failure_reason) {
    const reason = String(record.payload.failure_reason)
    if (reason.includes("expected") && reason.includes("got")) {
      summary.push("diff_hint: expectation mismatch detected; inspect expected vs actual values")
    }
  }
  summary.push("payload:")
  summary.push(JSON.stringify(record.payload || {}, null, 2))
  els.eventDetail.textContent = summary.join("\n")
}

function renderIncidentState() {
  const incident = appState.incident || { active: false, mode: "none", affected_requests: 0 }
  els.incidentState.textContent = JSON.stringify(incident, null, 2)
  els.scoreIncident.textContent = incident.active ? `${incident.mode} (active)` : "inactive"
}

function renderScoreboard() {
  const metrics = appState.metrics || {}
  const trends = appState.trendsSummary || {}

  let p50 = Number(trends.p50 || 0)
  let p95 = Number(trends.p95 || 0)
  let p99 = Number(trends.p99 || 0)
  if (Number(trends.request_count || 0) <= 0) {
    let routeCount = 0
    const latencyByRoute = metrics.latency_by_route_ms || {}
    for (const key of Object.keys(latencyByRoute)) {
      p50 += Number(latencyByRoute[key].p50 || 0)
      p95 += Number(latencyByRoute[key].p95 || 0)
      routeCount += 1
    }
    if (routeCount > 0) {
      p50 = p50 / routeCount
      p95 = p95 / routeCount
    }
    p99 = Number(metrics.latency_p99_ms || 0)
  }

  const runsTotal = Number(metrics.scenario_runs_total || 0)
  const runsPassed = Number(metrics.scenario_runs_passed || 0)
  const passRate = runsTotal > 0 ? (runsPassed / runsTotal) * 100 : 0

  let totalRequests = Number(metrics.total_requests || 0)
  let fiveXx = 0
  if (Number(trends.request_count || 0) > 0) {
    totalRequests = Number(trends.request_count || 0)
    fiveXx = Number(trends.status_5xx || 0)
  } else {
    const statusCounts = metrics.status_counts || {}
    fiveXx = Object.keys(statusCounts)
      .filter((code) => code.startsWith("5"))
      .reduce((sum, code) => sum + Number(statusCounts[code] || 0), 0)
  }
  const burn = totalRequests > 0 ? (fiveXx / totalRequests) * 100 : 0

  const missionState = appState.missionLifecycle
  const missionHasRun = missionState === "completed_pass" || missionState === "completed_fail"
  let missionReliability = "No mission run yet"
  let missionLatency = "No mission run yet"
  if (missionState === "running") {
    missionReliability = "running..."
    missionLatency = "running..."
  } else if (missionState === "blocked_role") {
    missionReliability = "blocked (switch role)"
    missionLatency = "blocked (switch role)"
  } else if (missionState === "setup_failed") {
    missionReliability = "setup failed"
    missionLatency = "setup failed"
  } else if (missionHasRun) {
    const reliabilityValue =
      appState.latestRun && appState.latestRun.analysis
        ? Number(appState.latestRun.analysis.reliability_score || 0)
        : 0
    missionReliability = String(reliabilityValue)
    if (appState.latestMissionLatency) {
      missionLatency = `${appState.latestMissionLatency.p50.toFixed(1)} / ${appState.latestMissionLatency.p95.toFixed(
        1,
      )} / ${appState.latestMissionLatency.p99.toFixed(1)}`
    } else {
      missionLatency = "-"
    }
  }

  els.liveScoreLatency.textContent = `${p50.toFixed(1)} / ${p95.toFixed(1)} / ${p99.toFixed(1)}`
  els.liveScoreBurn.textContent = `${burn.toFixed(2)}%`
  els.liveScoreUpdated.textContent =
    appState.liveMetricsUpdatedAt > 0 ? new Date(appState.liveMetricsUpdatedAt).toLocaleTimeString() : "-"
  els.missionScoreStatus.textContent = missionStatusLabel()
  els.missionScoreReliability.textContent = missionReliability
  els.missionScoreLatency.textContent = missionLatency
  els.scorePassRate.textContent = `${passRate.toFixed(1)}%`
  els.scoreMttr.textContent = appState.lastRecoveryMs == null ? "-" : `${appState.lastRecoveryMs.toFixed(0)}ms`
}

function renderVerdict(run, missionLabel, comparison = null) {
  appState.latestRun = run
  appState.latestMissionLatency = summarizeMissionLatency(run)
  const analysis = run.analysis || {}
  const pass = run.status === "pass"
  appState.missionLifecycle = pass ? "completed_pass" : "completed_fail"

  setVerdictCard(
    pass ? "pass" : "fail",
    `${missionLabel}: ${pass ? "PASS" : "FAIL"}`,
    analysis.root_cause_summary || "No root cause summary provided.",
  )

  els.analysisPanel.textContent = JSON.stringify(
    {
      run_id: run.run_id,
      status: run.status,
      analysis,
      summary: run.summary,
    },
    null,
    2,
  )

  const report = []
  report.push(`Mission: ${missionLabel}`)
  report.push(`Run: ${run.run_id}`)
  report.push(`Status: ${run.status.toUpperCase()}`)
  report.push(`What failed: ${analysis.failing_step_reason || "none"}`)
  report.push(`Bottleneck: ${analysis.latency_bottleneck_route || "n/a"} (${analysis.latency_bottleneck_ms || 0}ms)`)
  report.push(`Suggested action: ${analysis.suggested_next_action || "n/a"}`)
  if (comparison) {
    report.push(`Recovery delta: ${comparison}`)
  }
  const traces = (run.step_results || []).map((item) => item.trace_id).filter(Boolean)
  report.push(`Evidence traces: ${traces.join(", ") || "none"}`)
  els.narrativePanel.textContent = report.join("\n")

  renderScoreboard()
}

function renderComparison(beforeRun, afterRun) {
  const beforeScore = Number((beforeRun.analysis && beforeRun.analysis.reliability_score) || 0)
  const afterScore = Number((afterRun.analysis && afterRun.analysis.reliability_score) || 0)
  const beforeP99 = Number(beforeRun.summary ? beforeRun.summary.duration_ms : 0)
  const afterP99 = Number(afterRun.summary ? afterRun.summary.duration_ms : 0)

  appState.lastRecoveryMs = Math.max(0, afterP99)
  els.comparisonPanel.textContent = [
    `before_status: ${beforeRun.status}`,
    `after_status: ${afterRun.status}`,
    `score_delta: ${afterScore - beforeScore}`,
    `duration_delta_ms: ${(afterP99 - beforeP99).toFixed(3)}`,
  ].join("\n")
}

function handleLiveEvent(event) {
  if (!event || typeof event !== "object") {
    return
  }
  updateCursor(event.id)

  let cssClass = ""
  if (event.type.includes("completed") && event.payload && event.payload.status) {
    cssClass = String(event.payload.status) === "fail" || Number(event.payload.status) >= 400 ? "fail" : "pass"
  }

  if (event.type === "request.completed") {
    appState.latestRequest = {
      method: event.payload.method,
      path: event.payload.path,
      status: event.payload.status,
      trace_id: event.trace_id,
    }
    if (Number(event.payload.status) >= 400) {
      addDiagnostic(`Request failure detected: ${event.payload.method} ${event.payload.path} -> ${event.payload.status}`, "warn")
    }
  }

  if (event.type === "proxy.request.completed") {
    appState.latestProxy = {
      target_id: event.payload.target_id,
      method: event.payload.method,
      path: event.payload.path,
      status: event.payload.status,
    }
  }

  if (event.type === "incident.started" || event.type === "incident.stopped") {
    appState.incident = event.payload
    renderIncidentState()
  }

  pushTimeline(event, cssClass)
  updateServiceMap()
}

function applyDemoPreset(presetId) {
  const preset = DEMO_PRESETS[presetId]
  if (!preset) {
    return
  }
  appState.activePreset = presetId
  if (els.incidentMode) els.incidentMode.value = preset.incident.mode
  if (els.incidentProbability) els.incidentProbability.value = String(preset.incident.probability)
  if (els.incidentLatency) els.incidentLatency.value = String(preset.incident.latency_ms)
  if (els.presetHint) {
    els.presetHint.textContent = [
      `preset: ${preset.label}`,
      `recommended_mission: ${preset.mission}`,
      `incident: mode=${preset.incident.mode}, probability=${preset.incident.probability}, latency_ms=${preset.incident.latency_ms}`,
      `hint: ${preset.hint}`,
    ].join("\n")
  }
  addDiagnostic(`preset applied: ${preset.label}`, "info")
}

async function runActivePresetMission() {
  if (!appState.activePreset) {
    addDiagnostic("choose a preset first (Beginner, Ops, or Chaos)", "warn")
    return
  }
  const preset = DEMO_PRESETS[appState.activePreset]
  if (!preset) {
    return
  }
  await runMission(preset.mission)
}

async function fetchLiveModeStatus() {
  const response = await fetchJson("/api/events/live-mode")
  if (!response.ok) {
    return {
      available: false,
      reason: `status_${response.status}`,
      fallback: { mode: "snapshot_polling", refresh_ms: 1500 },
      actions: { retry: true, recommended_command: "python3 server.py --engine selectors" },
    }
  }
  return response.data
}

async function fetchEventSnapshot() {
  const token = getToken()
  const query = new URLSearchParams({
    since_id: String(appState.lastEventId),
    limit: "250",
    topics: [
      "request.completed",
      "scenario.run.started",
      "scenario.step.started",
      "scenario.step.completed",
      "scenario.run.completed",
      "proxy.request.completed",
      "system.health",
      "incident.started",
      "incident.stopped",
      "incident.affected",
    ].join(","),
  })
  if (token) {
    query.set("token", token)
  }

  const response = await fetchJson(`/api/events/snapshot?${query.toString()}`)
  if (response.status === 401) {
    setAuthState("unauthorized")
    return
  }
  if (!response.ok) {
    addDiagnostic(`events snapshot failed with status ${response.status}`, "error")
    if (response.status === 409) {
      appState.streamState = "fallback"
      updateStatusBadges()
      setLiveModeHint("Snapshot fallback active. Selectors engine is required for SSE.")
    }
    return
  }

  const events = response.data.events || []
  for (const event of events) {
    handleLiveEvent(event)
  }
  if (response.data.latest_id != null) {
    updateCursor(response.data.latest_id)
  }
}

function stopSnapshotFallback() {
  if (appState.fallbackTimer) {
    clearTimeout(appState.fallbackTimer)
    appState.fallbackTimer = null
  }
}

function startSnapshotFallback(refreshMs, reasonText) {
  stopSnapshotFallback()
  appState.streamState = "fallback"
  appState.streamTransport = "snapshot_polling"
  updateStatusBadges()
  if (reasonText) {
    setLiveModeHint(reasonText)
  }

  const runTick = async () => {
    await fetchEventSnapshot()
    appState.fallbackTimer = window.setTimeout(runTick, Math.max(750, Number(refreshMs || 1500)))
  }
  appState.fallbackTimer = window.setTimeout(runTick, 0)
}

function stopEventStream() {
  if (appState.eventSource) {
    appState.eventSource.close()
    appState.eventSource = null
  }
  if (appState.reconnectTimer) {
    clearTimeout(appState.reconnectTimer)
    appState.reconnectTimer = null
  }
  stopSnapshotFallback()
}

async function startEventStream(forceRetry = false) {
  stopEventStream()
  const liveMode = await fetchLiveModeStatus()
  appState.liveMode = liveMode
  const fallbackMs = Number((liveMode.fallback && liveMode.fallback.refresh_ms) || 1500)
  if (!liveMode.available) {
    const reason =
      liveMode.reason === "use_selectors_engine"
        ? `Selectors engine is required. Run: ${(liveMode.actions && liveMode.actions.recommended_command) || "python3 server.py --engine selectors"}`
        : `Live stream unavailable (${liveMode.reason || "unknown"})`
    addDiagnostic(`${reason}. Switched to snapshot fallback.`, "warn")
    setLiveModeHint(`${reason}. Fallback mode is active.`)
    startSnapshotFallback(fallbackMs, `${reason}. Fallback mode is active.`)
    return
  }

  if (forceRetry) {
    setLiveModeHint("Retrying live SSE mode...")
  } else {
    setLiveModeHint("Starting live SSE mode...")
  }

  const token = getToken()
  const query = new URLSearchParams({
    since_id: String(appState.lastEventId),
    topics: [
      "request.completed",
      "scenario.run.started",
      "scenario.step.started",
      "scenario.step.completed",
      "scenario.run.completed",
      "proxy.request.completed",
      "system.health",
      "incident.started",
      "incident.stopped",
      "incident.affected",
    ].join(","),
  })
  if (token) {
    query.set("token", token)
  }

  appState.streamState = "connecting"
  appState.streamTransport = "sse"
  updateStatusBadges()

  const source = new EventSource(`/api/events/stream?${query.toString()}`)
  appState.eventSource = source

  const commonListener = (raw) => {
    try {
      const parsed = JSON.parse(raw.data)
      handleLiveEvent(parsed)
    } catch (_err) {
      return
    }
  }

  const topics = [
    "request.completed",
    "scenario.run.started",
    "scenario.step.started",
    "scenario.step.completed",
    "scenario.run.completed",
    "proxy.request.completed",
    "system.health",
    "incident.started",
    "incident.stopped",
    "incident.affected",
  ]
  for (const topic of topics) {
    source.addEventListener(topic, commonListener)
  }

  source.onopen = () => {
    appState.streamState = "connected"
    appState.streamTransport = "sse"
    appState.reconnectAttempts = 0
    stopSnapshotFallback()
    setLiveModeHint("Live mode connected (SSE).")
    updateStatusBadges()
  }

  source.onerror = async () => {
    appState.reconnectAttempts += 1
    appState.streamState = appState.reconnectAttempts > 4 ? "fallback" : "reconnecting"
    updateStatusBadges()
    addDiagnostic(`event stream interrupted (attempt ${appState.reconnectAttempts})`, "warn")
    stopEventStream()
    await fetchEventSnapshot()

    if (appState.reconnectAttempts > 4) {
      const reason = "SSE degraded, switched to snapshot fallback."
      setLiveModeHint(`${reason} Click Enable Live Mode to retry.`)
      startSnapshotFallback(fallbackMs, `${reason} Click Enable Live Mode to retry.`)
      return
    }

    appState.reconnectTimer = window.setTimeout(
      () => {
        void startEventStream(true)
      },
      1200,
    )
  }
}

async function fetchState() {
  const [metricsRes, trendsRes, authRes, stateRes, targetsRes, scenariosRes, incidentRes] = await Promise.all([
    fetchJson("/_metrics"),
    fetchJson("/api/metrics/trends?window=15m&route=__all__"),
    fetchJson("/api/auth/me"),
    fetchJson("/api/playground/state"),
    fetchJson("/api/targets"),
    fetchJson("/api/scenarios"),
    fetchJson("/api/incidents/state"),
  ])

  if (authRes.ok) {
    appState.authRole = String((authRes.data.auth && authRes.data.auth.role) || "")
    setAuthState("connected")
    showTokenModal(false)
  } else if (
    stateRes.status === 401 ||
    targetsRes.status === 401 ||
    scenariosRes.status === 401 ||
    incidentRes.status === 401 ||
    authRes.status === 401
  ) {
    setAuthState("unauthorized")
  } else {
    setAuthState("connected")
  }

  if (metricsRes.ok) {
    appState.metrics = metricsRes.data || {}
    const engine = appState.metrics.engine || "-"
    setBadge(els.engineBadge, `engine: ${engine}`, "ok")
    appState.liveMetricsUpdatedAt = Date.now()
  }
  if (trendsRes.ok) {
    appState.trendsSummary = trendsRes.data.summary || {}
    appState.liveMetricsUpdatedAt = Date.now()
  }

  if (stateRes.ok) {
    renderMocks(stateRes.data.mocks || [])
    renderHistory(stateRes.data.history || [])
  }
  if (targetsRes.ok) {
    renderTargets(targetsRes.data.targets || [])
  }
  if (scenariosRes.ok) {
    renderScenarios(scenariosRes.data.scenarios || [])
  }
  if (incidentRes.ok) {
    appState.incident = incidentRes.data.incident || { active: false, mode: "none" }
    renderIncidentState()
  }

  renderScoreboard()
  updateServiceMap()
  updateStatusBadges()
  updateControlPermissions()
}

function renderManualRun(run) {
  els.scenarioRunResult.textContent = JSON.stringify(
    {
      run_id: run.run_id,
      status: run.status,
      seed: run.seed,
      summary: run.summary,
      analysis: run.analysis || {},
    },
    null,
    2,
  )
}

async function ensureMock(mockDef) {
  const listRes = await fetchJson("/api/mocks")
  if (!listRes.ok) {
    throw new Error(`unable to list mocks (${listRes.status})`)
  }

  const mocks = listRes.data.mocks || []
  const existing = mocks.find(
    (item) => item.method === mockDef.method && item.path_pattern === mockDef.path_pattern,
  )

  if (existing) {
    const updateRes = await fetchJson(`/api/mocks/${existing.id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(mockDef),
    })
    if (!updateRes.ok) {
      throw new Error(`unable to update mock (${updateRes.status})`)
    }
    return
  }

  const createRes = await fetchJson("/api/mocks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(mockDef),
  })
  if (!createRes.ok && createRes.status !== 409) {
    throw new Error(`unable to create mock (${createRes.status})`)
  }
}

async function ensureScenarioByName(name, scenarioTemplate) {
  const listRes = await fetchJson("/api/scenarios")
  if (!listRes.ok) {
    throw new Error(`unable to list scenarios (${listRes.status})`)
  }

  const existing = (listRes.data.scenarios || []).find((scenario) => scenario.name === name)
  const payload = {
    name,
    description: scenarioTemplate.description,
    defaults: scenarioTemplate.defaults,
    steps: scenarioTemplate.steps,
  }

  if (existing) {
    const updateRes = await fetchJson(`/api/scenarios/${existing.id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
    if (!updateRes.ok) {
      throw new Error(`unable to update scenario (${updateRes.status})`)
    }
    return updateRes.data.scenario || existing
  }

  const createRes = await fetchJson("/api/scenarios", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
  if (!createRes.ok) {
    throw new Error(`unable to create scenario (${createRes.status})`)
  }
  return createRes.data.scenario
}

async function runScenario(scenarioId, seed) {
  const payload = {}
  if (seed != null) {
    payload.seed = Number(seed)
  }
  const response = await fetchJson(`/api/scenarios/${scenarioId}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
  if (!response.ok) {
    const message = (response.data && response.data.error && response.data.error.message) || `scenario run failed (${response.status})`
    throw new Error(message)
  }
  return response.data.run
}

async function startIncident(profile) {
  const response = await fetchJson("/api/incidents/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(profile),
  })
  if (!response.ok) {
    throw new Error(`unable to start incident (${response.status})`)
  }
  appState.incident = response.data.incident || null
  renderIncidentState()
}

async function stopIncident() {
  const response = await fetchJson("/api/incidents/stop", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  })
  if (!response.ok) {
    throw new Error(`unable to stop incident (${response.status})`)
  }
  appState.incident = response.data.incident || null
  renderIncidentState()
}

async function runMission(missionId) {
  const mission = MISSION_PRESETS[missionId]
  if (!mission) {
    return
  }

  setMissionLifecycle("running", {
    missionLabel: mission.label,
    subtitle: "Preparing mocks, scenario, and incident profile...",
  })
  const startedAt = performance.now()
  try {
    if (missionRequiresAdminBootstrap(mission) && roleRank(appState.authRole) < roleRank("admin")) {
      const blockedReason = "This mission needs admin role to prepare mocks/scenarios. Click Switch Role."
      setMissionLifecycle("blocked_role", { subtitle: blockedReason })
      addDiagnostic(`mission ${mission.label} blocked: ${blockedReason}`, "warn")
      showTokenModal(true)
      return
    }

    for (const mock of mission.mocks || []) {
      await ensureMock(mock)
    }

    const scenario = await ensureScenarioByName(mission.scenarioName, mission.scenario)

    if (mission.incidentStart) {
      await startIncident(mission.incidentStart)
    }

    const beforeRun = await runScenario(scenario.id, mission.seed)
    renderManualRun(beforeRun)

    if (mission.dualRunRecovery) {
      addDiagnostic(`${mission.label}: initial run status=${beforeRun.status}; starting recovery rerun`, "warn")
      await stopIncident()
      const afterRun = await runScenario(scenario.id, Number(mission.seed) + 1)
      renderVerdict(afterRun, mission.label, "fail -> pass recovery complete")
      renderComparison(beforeRun, afterRun)
      addDiagnostic(
        `${mission.label} completed with recovery rerun (before=${beforeRun.status}, after=${afterRun.status})`,
        "info",
      )
    } else {
      renderVerdict(beforeRun, mission.label)
      if (mission.incidentStopAfter) {
        await stopIncident()
      }
      addDiagnostic(`${mission.label} completed`, "info")
    }
  } catch (error) {
    const friendlyError = formatMissionError(error)
    setMissionLifecycle("setup_failed", {
      title: `${mission.label}: setup failed`,
      subtitle: friendlyError,
    })
    addDiagnostic(`mission ${mission.label} failed: ${friendlyError}`, "error")
    if (friendlyError.includes("role restriction")) {
      showTokenModal(true)
    }
  } finally {
    const elapsedMs = performance.now() - startedAt
    els.comparisonPanel.textContent = `${els.comparisonPanel.textContent}\nlast_mission_elapsed_ms: ${elapsedMs.toFixed(2)}`
    await fetchState()
    await fetchEventSnapshot()
  }
}

async function sendManualRequest() {
  const headers = parseJsonSafe(els.reqHeaders.value, {})
  const payload = {
    method: els.reqMethod.value,
    path: els.reqPath.value || "/",
    headers,
    body: els.reqBody.value || "",
  }

  const targetId = els.reqTarget.value || "local"
  const endpoint = targetId === "local" ? "/api/playground/request" : "/api/proxy/request"
  const body =
    targetId === "local"
      ? payload
      : {
          target_id: targetId,
          method: payload.method,
          path: payload.path,
          headers: payload.headers,
          body: payload.body,
        }

  const startedAt = performance.now()
  const response = await fetchJson(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
  const latencyMs = (performance.now() - startedAt).toFixed(2)

  if (!response.ok) {
    renderResponse(response.data.error || response.data, response.status, latencyMs)
    return
  }

  const result = response.data.response || response.data.result || response.data
  renderResponse(result, result.status || response.status, latencyMs)
  await fetchState()
}

async function createOrUpdateMock() {
  let bodyText = els.mockBody.value || "{}"
  try {
    JSON.parse(bodyText)
  } catch (_err) {
    bodyText = JSON.stringify({ raw: bodyText })
  }

  await ensureMock({
    method: els.mockMethod.value,
    path_pattern: els.mockPath.value.trim(),
    status: Number(els.mockStatus.value || 201),
    headers: { "X-Mock-Server": "manual" },
    body: bodyText,
    content_type: "application/json",
  })
  await fetchState()
}

async function createTarget() {
  const payload = {
    name: els.targetName.value.trim(),
    base_url: els.targetBaseUrl.value.trim(),
    enabled: true,
    timeout_ms: Number(els.targetTimeout.value || 5000),
  }
  const response = await fetchJson("/api/targets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
  if (!response.ok) {
    addDiagnostic(`target create failed (${response.status})`, "error")
  }
  await fetchState()
}

async function runSelectedScenario() {
  const scenarioId = els.scenarioSelect.value
  if (!scenarioId) {
    els.scenarioRunResult.textContent = "Create a scenario first."
    return
  }
  try {
    const run = await runScenario(scenarioId, els.scenarioSeed.value ? Number(els.scenarioSeed.value) : null)
    renderManualRun(run)
    renderVerdict(run, "Manual Scenario")
    await fetchState()
  } catch (error) {
    els.scenarioRunResult.textContent = String(error)
  }
}

function bindEvents() {
  document.querySelectorAll("[data-mission]").forEach((button) => {
    button.addEventListener("click", () => {
      void runMission(button.getAttribute("data-mission"))
    })
  })

  document.querySelectorAll("[data-preset]").forEach((button) => {
    button.addEventListener("click", () => {
      applyDemoPreset(button.getAttribute("data-preset"))
    })
  })

  if (els.runPresetBtn) {
    els.runPresetBtn.addEventListener("click", () => {
      void runActivePresetMission()
    })
  }

  if (els.enableLiveModeBtn) {
    els.enableLiveModeBtn.addEventListener("click", () => {
      appState.reconnectAttempts = 0
      void startEventStream(true)
    })
  }

  if (els.switchRoleBtn) {
    els.switchRoleBtn.addEventListener("click", () => {
      showTokenModal(true)
    })
  }

  if (els.loginBtn) {
    els.loginBtn.addEventListener("click", async () => {
      const username = (els.usernameInput && els.usernameInput.value ? els.usernameInput.value : "").trim()
      const password = els.passwordInput && els.passwordInput.value ? els.passwordInput.value : ""
      const response = await fetchJson("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      })
      if (!response.ok) {
        addDiagnostic(
          `login failed (${response.status}): ${(response.data.error && response.data.error.message) || "unauthorized"}`,
          "error",
        )
        return
      }
      const token = (response.data.session && response.data.session.token) || ""
      if (!token) {
        addDiagnostic("login succeeded but no token returned", "error")
        return
      }
      setToken(token)
      showTokenModal(false)
      setAuthState("checking")
      await fetchState()
      await fetchEventSnapshot()
      await startEventStream(true)
    })
  }

  els.saveTokenBtn.addEventListener("click", async () => {
    const token = els.tokenInput.value.trim()
    setToken(token)
    showTokenModal(false)
    setAuthState("checking")
    await fetchState()
    await fetchEventSnapshot()
    await startEventStream(true)
  })

  els.clearTokenBtn.addEventListener("click", async () => {
    setToken("")
    setAuthState("unauthorized")
    await fetchState()
    await fetchEventSnapshot()
  })

  els.startIncidentBtn.addEventListener("click", async () => {
    try {
      await startIncident({
        mode: els.incidentMode.value,
        probability: Number(els.incidentProbability.value || 0),
        latency_ms: Number(els.incidentLatency.value || 0),
        seed: 1337,
      })
      addDiagnostic(`incident started: ${els.incidentMode.value}`, "warn")
      noteFaultChangedIfNoMission()
      await fetchState()
    } catch (error) {
      addDiagnostic(`incident start failed: ${String(error)}`, "error")
    }
  })

  els.stopIncidentBtn.addEventListener("click", async () => {
    try {
      await stopIncident()
      addDiagnostic("incident stopped", "info")
      noteFaultChangedIfNoMission()
      await fetchState()
    } catch (error) {
      addDiagnostic(`incident stop failed: ${String(error)}`, "error")
    }
  })

  els.sendBtn.addEventListener("click", sendManualRequest)
  els.createMockBtn.addEventListener("click", createOrUpdateMock)
  els.refreshBtn.addEventListener("click", fetchState)
  els.createTargetBtn.addEventListener("click", createTarget)
  els.refreshTargetsBtn.addEventListener("click", fetchState)
  els.filterBtn.addEventListener("click", fetchState)
  els.runScenarioBtn.addEventListener("click", runSelectedScenario)

  els.mocksList.addEventListener("click", async (event) => {
    const id = event.target.getAttribute("data-delete-mock")
    if (!id) {
      return
    }
    await fetchJson(`/api/mocks/${id}`, { method: "DELETE" })
    await fetchState()
  })

  els.targetsList.addEventListener("click", async (event) => {
    const id = event.target.getAttribute("data-delete-target")
    if (!id) {
      return
    }
    await fetchJson(`/api/targets/${id}`, { method: "DELETE" })
    await fetchState()
  })

  els.historyList.addEventListener("click", async (event) => {
    const requestId = event.target.getAttribute("data-replay")
    if (!requestId) {
      return
    }
    await fetchJson(`/api/replay/${requestId}`, { method: "POST" })
    await fetchState()
  })

  els.timelineList.addEventListener("click", (event) => {
    const node = event.target.closest("li")
    if (!node) {
      return
    }
    showEventDetail(node.dataset.eventId)
  })
}

function getMissionFromUrl() {
  const params = new URLSearchParams(window.location.search)
  const mission = params.get("mission")
  if (!mission || !MISSION_PRESETS[mission]) return null
  return mission
}

async function boot() {
  bindEvents()
  updateStatusBadges()
  updateControlPermissions()
  setMissionLifecycle("idle")
  applyDemoPreset("beginner")
  await fetchState()
  await fetchEventSnapshot()
  await startEventStream()

  window.setInterval(fetchState, 7000)

  const autoMission = getMissionFromUrl()
  if (autoMission) {
    setTimeout(() => {
      void runMission(autoMission)
    }, 2000)
  }
}

boot()
