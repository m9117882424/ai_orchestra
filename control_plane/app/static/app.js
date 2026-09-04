const statusLabels = {
  backlog: "Бэклог",
  planned: "Запланировано",
  in_progress: "В работе",
  waiting_approval: "Ждет решения",
  qa: "Проверка",
  done: "Готово",
  failed: "Ошибка",
};

const domainLabels = {
  development: "Разработка",
  analytics: "Аналитика",
  trading: "Торговые исследования",
};

const approvalLabels = {
  code_change: "Изменение кода",
  git_push: "Публикация ветки",
  deploy: "Production deploy",
  secret_access: "Доступ к секретам",
  external_write: "Запись во внешнюю систему",
  financial_execution: "Финансовое исполнение",
};

const transitions = {
  backlog: ["planned", "in_progress"],
  planned: ["backlog", "in_progress"],
  in_progress: ["waiting_approval", "qa", "failed"],
  waiting_approval: ["in_progress", "qa", "failed"],
  qa: ["done", "in_progress", "failed"],
  failed: ["planned", "in_progress"],
  done: [],
};

async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = { ...(options.headers || {}) };
  if (method !== "GET" && method !== "HEAD") headers["X-Control-Request"] = "ai-orchestra";
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let message = `Ошибка ${response.status}`;
    try { message = (await response.json()).detail || message; } catch (_) { /* noop */ }
    throw new Error(message);
  }
  return response.json();
}

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

let toastTimer;
function toast(message, error = false) {
  const element = document.getElementById("toast");
  element.textContent = message;
  element.className = `toast visible${error ? " error" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.className = "toast"; }, 3500);
}

function formatNumber(value) {
  return new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(Number(value));
}

function formatDate(value) {
  return new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "short" }).format(new Date(value));
}

async function loadSummary() {
  const data = await api("/api/summary");
  document.getElementById("metric-progress").textContent = (data.tasks.in_progress || 0) + (data.tasks.qa || 0);
  document.getElementById("metric-approvals").textContent = data.pending_approvals;
  document.getElementById("metric-cost").textContent = formatNumber(data.month_cost);
  document.getElementById("metric-done").textContent = data.tasks.done || 0;
}

async function loadTasks() {
  const tasks = await api("/api/tasks?limit=50");
  const body = document.getElementById("tasks-body");
  body.replaceChildren();
  if (!tasks.length) {
    const row = node("tr");
    const cell = node("td", "empty", "Пока нет задач — создайте первую.");
    cell.colSpan = 4;
    row.append(cell);
    body.append(row);
    return;
  }
  tasks.forEach((task) => {
    const row = node("tr");
    const titleCell = node("td");
    titleCell.append(node("span", "task-title", task.title));
    titleCell.append(node("span", "task-meta", `${task.project} · ${formatDate(task.created_at)}`));
    const domainCell = node("td");
    domainCell.append(node("span", "pill", domainLabels[task.domain] || task.domain));
    const riskCell = node("td");
    riskCell.append(node("span", `pill ${task.risk_level}`, task.risk_level));
    const statusCell = node("td");
    if (transitions[task.status]?.length) {
      const select = node("select", "status-select");
      select.append(new Option(statusLabels[task.status], task.status, true, true));
      transitions[task.status].forEach((status) => select.append(new Option(`→ ${statusLabels[status]}`, status)));
      select.addEventListener("change", async () => {
        if (select.value === task.status) return;
        select.disabled = true;
        try {
          await api(`/api/tasks/${task.id}/status`, { method: "PATCH", body: JSON.stringify({ status: select.value }) });
          toast("Статус задачи обновлен");
          await refreshAll();
        } catch (error) {
          toast(error.message, true);
          select.value = task.status;
          select.disabled = false;
        }
      });
      statusCell.append(select);
    } else {
      statusCell.append(node("span", `pill ${task.status}`, statusLabels[task.status] || task.status));
    }
    row.append(titleCell, domainCell, riskCell, statusCell);
    body.append(row);
  });
}

function deniedLabel(value) {
  return value ? "РАЗРЕШЕНО" : "ЗАПРЕЩЕНО";
}

async function loadCapabilityGuard() {
  const guard = await api("/api/capabilities/guard");
  document.getElementById("guard-deploy").textContent = deniedLabel(guard.production_deploy_allowed);
  document.getElementById("guard-write").textContent = deniedLabel(guard.external_write_allowed);
  document.getElementById("guard-finance").textContent = deniedLabel(guard.financial_execution_allowed);
  document.getElementById("guard-secrets").textContent = deniedLabel(guard.secret_access_allowed);
}

async function decideApproval(id, decision) {
  const comment = window.prompt(decision === "approved" ? "Комментарий к одобрению (необязательно)" : "Причина отказа");
  if (comment === null) return;
  try {
    await api(`/api/approvals/${id}/decision`, { method: "POST", body: JSON.stringify({ decision, comment }) });
    toast(decision === "approved" ? "Решение одобрено" : "Решение отклонено");
    await refreshAll();
  } catch (error) { toast(error.message, true); }
}

async function loadApprovals() {
  const approvals = await api("/api/approvals?limit=20");
  const list = document.getElementById("approvals-list");
  list.replaceChildren();
  if (!approvals.length) {
    list.append(node("p", "empty", "Нет запросов на согласование."));
    return;
  }
  approvals.forEach((approval) => {
    const item = node("article", "stack-item");
    const header = node("header");
    header.append(node("strong", "", approvalLabels[approval.kind] || approval.kind));
    header.append(node("span", `pill ${approval.status}`, approval.status));
    item.append(header, node("p", "", approval.reason));
    if (approval.status === "pending") {
      const actions = node("div", "stack-actions");
      const approve = node("button", "button button-small button-secondary", "Одобрить");
      const reject = node("button", "button button-small button-danger", "Отклонить");
      approve.addEventListener("click", () => decideApproval(approval.id, "approved"));
      reject.addEventListener("click", () => decideApproval(approval.id, "rejected"));
      actions.append(approve, reject);
      item.append(actions);
    }
    list.append(item);
  });
}

async function loadBudgets() {
  const budgets = await api("/api/budgets");
  const list = document.getElementById("budgets-list");
  list.replaceChildren();
  budgets.forEach((budget) => {
    const row = node("div", "stack-item budget-row");
    const label = node("div");
    label.append(node("strong", "", budget.scope));
    label.append(node("small", "", `Предупреждение ${budget.warning_pct}% · hard stop ${budget.hard_stop ? "да" : "нет"}`));
    const input = node("input");
    input.type = "number";
    input.min = "0";
    input.step = "0.01";
    input.value = budget.monthly_limit;
    const save = node("button", "button button-small button-secondary", "Сохранить");
    save.addEventListener("click", async () => {
      try {
        await api(`/api/budgets/${encodeURIComponent(budget.scope)}`, {
          method: "PUT",
          body: JSON.stringify({ monthly_limit: input.value, warning_pct: budget.warning_pct, hard_stop: budget.hard_stop, enabled: budget.enabled }),
        });
        toast("Бюджет обновлен");
        await refreshAll();
      } catch (error) { toast(error.message, true); }
    });
    row.append(label, input, save);
    list.append(row);
  });
}

async function loadAudit() {
  const events = await api("/api/audit?limit=50");
  const list = document.getElementById("audit-list");
  list.replaceChildren();
  if (!events.length) {
    list.append(node("p", "empty", "Журнал пока пуст."));
    return;
  }
  events.forEach((event) => {
    const row = node("div", "audit-item");
    row.append(node("time", "", formatDate(event.created_at)));
    row.append(node("strong", "", event.action));
    row.append(node("span", "", `${event.actor} · ${event.entity_type}:${event.entity_id}`));
    list.append(row);
  });
}

async function refreshAll() {
  try {
    await Promise.all([loadSummary(), loadTasks(), loadCapabilityGuard(), loadApprovals(), loadBudgets(), loadAudit()]);
  } catch (error) { toast(error.message, true); }
}

document.getElementById("show-task-form").addEventListener("click", () => document.getElementById("task-form-panel").classList.remove("hidden"));
document.getElementById("hide-task-form").addEventListener("click", () => document.getElementById("task-form-panel").classList.add("hidden"));
document.querySelectorAll("[data-refresh]").forEach((button) => button.addEventListener("click", refreshAll));
document.getElementById("task-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const status = document.getElementById("task-form-status");
  const payload = Object.fromEntries(new FormData(form).entries());
  try {
    status.textContent = "";
    await api("/api/tasks", { method: "POST", body: JSON.stringify(payload) });
    form.reset();
    form.elements.project.value = "general";
    document.getElementById("task-form-panel").classList.add("hidden");
    toast("Задача добавлена в бэклог");
    await refreshAll();
  } catch (error) { status.textContent = error.message; }
});

refreshAll();
