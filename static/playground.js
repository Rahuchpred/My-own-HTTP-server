const reqMethod = document.getElementById("reqMethod");
const reqPath = document.getElementById("reqPath");
const reqHeaders = document.getElementById("reqHeaders");
const reqBody = document.getElementById("reqBody");
const respStats = document.getElementById("respStats");
const respHeaders = document.getElementById("respHeaders");
const respBody = document.getElementById("respBody");
const engineBadge = document.getElementById("engineBadge");
const traceBadge = document.getElementById("traceBadge");
const mocksList = document.getElementById("mocksList");
const historyList = document.getElementById("historyList");

const mockMethod = document.getElementById("mockMethod");
const mockPath = document.getElementById("mockPath");
const mockStatus = document.getElementById("mockStatus");
const mockBody = document.getElementById("mockBody");

const historyFilter = document.getElementById("historyFilter");

function parseHeaders() {
  try {
    const parsed = JSON.parse(reqHeaders.value || "{}");
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed;
    }
  } catch (_err) {}
  return {};
}

async function fetchState() {
  const [stateRes, metricsRes] = await Promise.all([
    fetch("/api/playground/state"),
    fetch("/_metrics"),
  ]);
  const state = await stateRes.json();
  const metrics = await metricsRes.json();
  renderMocks(state.mocks || []);
  renderHistory((state.history || []).slice().reverse());
  engineBadge.textContent = `engine: ${metrics.engine || "selectors/threadpool"}`;
  traceBadge.textContent = `trace: ${metrics.request_trace_id || "-"}`;
}

function renderMocks(mocks) {
  mocksList.innerHTML = "";
  for (const mock of mocks) {
    const li = document.createElement("li");
    li.innerHTML = `
      <div class="mono">${mock.method} ${mock.path_pattern} -> ${mock.status}</div>
      <small>id: ${mock.id}</small>
      <div style="margin-top:8px;display:flex;gap:8px;">
        <button data-delete="${mock.id}">Delete</button>
      </div>
    `;
    mocksList.appendChild(li);
  }
}

function renderHistory(history) {
  const filter = historyFilter.value.trim();
  historyList.innerHTML = "";
  for (const item of history) {
    if (filter && !String(item.path || "").includes(filter)) {
      continue;
    }
    const li = document.createElement("li");
    li.innerHTML = `
      <div class="mono">${item.status} ${item.method} ${item.path}</div>
      <small>${item.ts} | latency ${item.latency_ms}ms | trace ${item.trace_id}</small>
      <div style="margin-top:8px;display:flex;gap:8px;">
        <button data-replay="${item.request_id}">Replay</button>
      </div>
    `;
    historyList.appendChild(li);
  }
}

async function sendPlaygroundRequest() {
  const payload = {
    method: reqMethod.value,
    path: reqPath.value || "/",
    headers: parseHeaders(),
    body: reqBody.value || "",
  };
  const started = performance.now();
  const res = await fetch("/api/playground/request", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const latency = (performance.now() - started).toFixed(2);
  const data = await res.json();
  const result = data.response || data.error || {};
  respStats.innerHTML = `<span>Status: ${result.status || res.status}</span><span>Latency: ${latency}ms</span><span>Bytes: ${JSON.stringify(result).length}</span>`;
  respHeaders.textContent = JSON.stringify(result.headers || {}, null, 2);
  respBody.textContent = typeof result.body === "string" ? result.body : JSON.stringify(result, null, 2);
  await fetchState();
}

async function createMock() {
  let bodyText = mockBody.value || "{}";
  try {
    JSON.parse(bodyText);
  } catch (_err) {
    bodyText = JSON.stringify({ raw: bodyText });
  }

  const payload = {
    method: mockMethod.value,
    path_pattern: mockPath.value.trim(),
    status: Number(mockStatus.value || 201),
    headers: { "X-Mock-Server": "custom" },
    body: bodyText,
    content_type: "application/json",
  };

  await fetch("/api/mocks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  await fetchState();
}

async function replayRequest(requestId) {
  await fetch(`/api/replay/${requestId}`, { method: "POST" });
  await fetchState();
}

document.getElementById("sendBtn").addEventListener("click", sendPlaygroundRequest);
document.getElementById("createMockBtn").addEventListener("click", createMock);
document.getElementById("refreshBtn").addEventListener("click", fetchState);
document.getElementById("filterBtn").addEventListener("click", fetchState);

mocksList.addEventListener("click", async (event) => {
  const id = event.target.getAttribute("data-delete");
  if (!id) return;
  await fetch(`/api/mocks/${id}`, { method: "DELETE" });
  await fetchState();
});

historyList.addEventListener("click", async (event) => {
  const id = event.target.getAttribute("data-replay");
  if (!id) return;
  await replayRequest(id);
});

for (const button of document.querySelectorAll("[data-action]")) {
  button.addEventListener("click", async () => {
    const action = button.getAttribute("data-action");
    if (action === "ping") {
      reqMethod.value = "GET";
      reqPath.value = "/";
      reqBody.value = "";
      await sendPlaygroundRequest();
    } else if (action === "stream") {
      reqMethod.value = "GET";
      reqPath.value = "/stream";
      await sendPlaygroundRequest();
    } else if (action === "create-user-mock") {
      mockMethod.value = "POST";
      mockPath.value = "/api/users";
      mockStatus.value = "201";
      mockBody.value = '{"id": 101, "name": "Demo User", "source": "mock"}';
      await createMock();
    }
  });
}

fetchState();
setInterval(fetchState, 3000);
