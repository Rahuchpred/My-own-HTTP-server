const MAX_TIMELINE = 120
const MAX_HISTORY_ITEMS = 80

const appState = {
  authState: "checking",
  streamState: "connecting",
  diagnostics: [],
  lastEventId: 0,
  eventSource: null,
  reconnectTimer: null,
  reconnectAttempts: 0,
  eventIndex: new Map(),
  timeline: [],
  latestRequest: null,
  latestProxy: null,
  latestRun: null,
  lastRecoveryMs: null,
  scenarios: [],
  targets: [],
  history: [],
  incident: null,
  metrics: {},
}

const els = {
  engineBadge: document.getElementById("engineBadge"),
  authBadge: document.getElementById("authBadge"),
  eventsBadge: document.getElementById("eventsBadge"),
  healthBadge: document.getElementById("healthBadge"),
  cursorBadge: document.getElementById("cursorBadge"),

  incidentMode: document.getElementById("incidentMode"),
  incidentProbability: document.getElementById("incidentProbability"),
  incidentLatency: document.getElementById("incidentLatency"),
  startIncidentBtn: document.getElementById("startIncidentBtn"),
  stopIncidentBtn: document.getElementById("stopIncidentBtn"),
  incidentState: document.getElementById("incidentState"),

  verdictCard: document.getElementById("verdictCard"),
  analysisPanel: document.getElementById("analysisPanel"),
  narrativePanel: document.getElementById("narrativePanel"),

  timelineList: document.getElementById("timelineList"),
  eventDetail: document.getElementById("eventDetail"),
  serviceMap: document.getElementById("serviceMap"),
  diagnosticsPanel: document.getElementById("diagnosticsPanel"),

  scoreReliability: document.getElementById("scoreReliability"),
  scoreLatency: document.getElementById("scoreLatency"),
  scorePassRate: document.getElementById("scorePassRate"),
  scoreBurn: document.getElementById("scoreBurn"),
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
      latency_ms: 1300,
      seed: 2026,
    },
    incidentStopAfter: true,
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

function getToken() {
  return window.localStorage.getItem("demoToken") || ""
}

function setToken(token) {
  if (!token) {
    window.localStorage.removeItem("demoToken")
    return
  }
  window.localStorage.setItem("demoToken", token)
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

function updateStatusBadges() {
  const authKind = appState.authState === "connected" ? "ok" : appState.authState === "checking" ? "warn" : "bad"
  const streamKind = appState.streamState === "connected" ? "ok" : appState.streamState === "reconnecting" ? "warn" : "bad"
  const healthKind = appState.incident && appState.incident.active ? "bad" : "ok"

  setBadge(els.authBadge, `auth: ${appState.authState}`, authKind)
  setBadge(els.eventsBadge, `events: ${appState.streamState}`, streamKind)
  setBadge(els.healthBadge, `health: ${appState.incident && appState.incident.active ? "degraded" : "healthy"}`, healthKind)
}

function setAuthState(nextState) {
  if (appState.authState === nextState) {
    return
  }
  appState.authState = nextState
  updateStatusBadges()
  if (nextState === "unauthorized") {
    addDiagnostic("Protected APIs returned 401. Token is missing or invalid.", "warn")
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
  const latest = appState.latestRun
  const reliability = latest && latest.analysis ? latest.analysis.reliability_score : "-"

  let p50 = 0
  let p95 = 0
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
  const p99 = Number(metrics.latency_p99_ms || 0)

  const runsTotal = Number(metrics.scenario_runs_total || 0)
  const runsPassed = Number(metrics.scenario_runs_passed || 0)
  const passRate = runsTotal > 0 ? (runsPassed / runsTotal) * 100 : 0

  const statusCounts = metrics.status_counts || {}
  const totalRequests = Number(metrics.total_requests || 0)
  const fiveXx = Object.keys(statusCounts)
    .filter((code) => code.startsWith("5"))
    .reduce((sum, code) => sum + Number(statusCounts[code] || 0), 0)
  const burn = totalRequests > 0 ? (fiveXx / totalRequests) * 100 : 0

  els.scoreReliability.textContent = String(reliability)
  els.scoreLatency.textContent = `${p50.toFixed(1)} / ${p95.toFixed(1)} / ${p99.toFixed(1)}`
  els.scorePassRate.textContent = `${passRate.toFixed(1)}%`
  els.scoreBurn.textContent = `${burn.toFixed(2)}%`
  els.scoreMttr.textContent = appState.lastRecoveryMs == null ? "-" : `${appState.lastRecoveryMs.toFixed(0)}ms`
}

function renderVerdict(run, missionLabel, comparison = null) {
  appState.latestRun = run
  const analysis = run.analysis || {}
  const pass = run.status === "pass"

  els.verdictCard.classList.remove("pass", "fail", "neutral")
  els.verdictCard.classList.add(pass ? "pass" : "fail")
  els.verdictCard.innerHTML = `
    <div class="verdict-title">${missionLabel}: ${pass ? "PASS" : "FAIL"}</div>
    <div class="verdict-sub">${analysis.root_cause_summary || "No root cause summary provided."}</div>
  `

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

function stopEventStream() {
  if (appState.eventSource) {
    appState.eventSource.close()
    appState.eventSource = null
  }
  if (appState.reconnectTimer) {
    clearTimeout(appState.reconnectTimer)
    appState.reconnectTimer = null
  }
}

function startEventStream() {
  stopEventStream()

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
    appState.reconnectAttempts = 0
    updateStatusBadges()
  }

  source.onerror = async () => {
    appState.reconnectAttempts += 1
    appState.streamState = appState.reconnectAttempts > 4 ? "degraded" : "reconnecting"
    updateStatusBadges()
    addDiagnostic(`event stream interrupted (attempt ${appState.reconnectAttempts})`, "warn")
    stopEventStream()
    await fetchEventSnapshot()

    appState.reconnectTimer = window.setTimeout(
      () => {
        startEventStream()
      },
      appState.reconnectAttempts > 4 ? 4000 : 1200,
    )
  }
}

async function fetchState() {
  const [metricsRes, stateRes, targetsRes, scenariosRes, incidentRes] = await Promise.all([
    fetchJson("/_metrics"),
    fetchJson("/api/playground/state"),
    fetchJson("/api/targets"),
    fetchJson("/api/scenarios"),
    fetchJson("/api/incidents/state"),
  ])

  if (stateRes.status === 401 || targetsRes.status === 401 || scenariosRes.status === 401 || incidentRes.status === 401) {
    setAuthState("unauthorized")
  } else {
    setAuthState("connected")
  }

  if (metricsRes.ok) {
    appState.metrics = metricsRes.data || {}
    const engine = appState.metrics.engine || "-"
    setBadge(els.engineBadge, `engine: ${engine}`, "ok")
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

  els.verdictCard.classList.remove("pass", "fail")
  els.verdictCard.classList.add("neutral")
  els.verdictCard.innerHTML = `<div class="verdict-title">Running ${mission.label}</div><div class="verdict-sub">Preparing mocks, scenario, and incident profile...</div>`

  const startedAt = performance.now()
  try {
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
      await stopIncident()
      const afterRun = await runScenario(scenario.id, Number(mission.seed) + 1)
      renderVerdict(afterRun, mission.label, "fail -> pass recovery complete")
      renderComparison(beforeRun, afterRun)
      addDiagnostic(`${mission.label} completed with recovery rerun`, "info")
    } else {
      renderVerdict(beforeRun, mission.label)
      if (mission.incidentStopAfter) {
        await stopIncident()
      }
      addDiagnostic(`${mission.label} completed`, "info")
    }
  } catch (error) {
    els.verdictCard.classList.remove("pass", "neutral")
    els.verdictCard.classList.add("fail")
    els.verdictCard.innerHTML = `<div class="verdict-title">${mission.label}: execution failed</div><div class="verdict-sub">${String(error)}</div>`
    addDiagnostic(`mission ${mission.label} failed: ${String(error)}`, "error")
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
    button.addEventListener("click", () => runMission(button.getAttribute("data-mission")))
  })

  els.saveTokenBtn.addEventListener("click", async () => {
    const token = els.tokenInput.value.trim()
    setToken(token)
    showTokenModal(false)
    setAuthState("checking")
    await fetchState()
    await fetchEventSnapshot()
    startEventStream()
  })

  els.clearTokenBtn.addEventListener("click", async () => {
    setToken("")
    setAuthState("unauthorized")
    await fetchState()
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
      await fetchState()
    } catch (error) {
      addDiagnostic(`incident start failed: ${String(error)}`, "error")
    }
  })

  els.stopIncidentBtn.addEventListener("click", async () => {
    try {
      await stopIncident()
      addDiagnostic("incident stopped", "info")
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

async function boot() {
  bindEvents()
  updateStatusBadges()
  await fetchState()
  await fetchEventSnapshot()
  startEventStream()

  window.setInterval(fetchState, 7000)
}

boot()
