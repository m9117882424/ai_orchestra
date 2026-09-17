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

const executionLabels = { preparing: "Подготовка каталога", queued: "В очереди", running: "Выполняется", completed: "Готов к приемке", failed: "Ошибка", cancelled: "Остановлен" };
const repositoryStatusLabels = {
  pending_validation: "Ожидает проверки",
  validating: "Проверяется",
  ready: "Готов",
  unavailable: "Недоступен",
  invalid: "Отклонён",
};
const repositoryProviderLabels = { github: "GitHub", gitlab: "GitLab", bitbucket: "Bitbucket", generic: "Generic Git" };
const assuranceLabels = {
  "general-standard": "Standard",
  "general-high-assurance": "High assurance",
  "regulated-critical": "Regulated critical",
};
const workspaceStatusLabels = {
  pending: "Ожидает подготовки",
  preparing: "Подготавливается",
  unavailable: "Повтор ожидается",
  ready: "Готов",
  inspection_pending: "Ожидает проверки",
  inspecting: "Проверяется",
  retained: "Сохранён для ревью",
  cleanup_pending: "Ожидает удаления",
  cleaning: "Удаляется",
  removed: "Удалён",
  invalid: "Заблокирован",
};
let progressExecutionId = null;
let executionsByTask = new Map();
let repositoriesById = new Map();
let tasksById = new Map();

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
    try {
      const detail = (await response.json()).detail;
      if (typeof detail === "string") message = detail;
      else if (Array.isArray(detail)) message = detail.map((item) => item.msg || "Некорректные данные").join("; ");
    } catch (_) { /* noop */ }
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
  const costMetric = document.getElementById("metric-cost");
  if (data.month_cost_status === "unknown") {
    costMetric.textContent = "нет данных";
  } else if (data.month_cost_status === "partial") {
    costMetric.textContent = `≥ ${formatNumber(data.month_cost)}`;
  } else {
    costMetric.textContent = formatNumber(data.month_cost);
  }
  document.getElementById("metric-done").textContent = data.tasks.done || 0;
}

async function loadExecutions() {
  const runs = await api("/api/executions?limit=100");
  executionsByTask = new Map();
  runs.forEach((run) => {
    if (!executionsByTask.has(run.task_id)) executionsByTask.set(run.task_id, run);
  });
}

async function setRepositoryEnabled(repository) {
  try {
    await api(`/api/repositories/${repository.id}`, {
      method: "PATCH",
      body: JSON.stringify({ expected_version: repository.version, enabled: !repository.enabled }),
    });
    toast(repository.enabled ? "Репозиторий отключён" : "Репозиторий включён");
    await refreshAll();
  } catch (error) { toast(error.message, true); }
}

async function validateRepository(repository) {
  try {
    await api(`/api/repositories/${repository.id}/validate`, {
      method: "POST",
      body: JSON.stringify({ expected_version: repository.version }),
    });
    toast("Проверка репозитория поставлена в надёжную очередь");
    await refreshAll();
  } catch (error) { toast(error.message, true); }
}

async function loadRepositories() {
  const repositories = await api("/api/repositories?limit=100");
  repositoriesById = new Map(repositories.map((repository) => [repository.id, repository]));
  const taskRepository = document.getElementById("task-repository");
  taskRepository.replaceChildren(new Option("Выберите готовый репозиторий", ""));
  repositories.forEach((repository) => {
    const option = new Option(
      `${repository.name} · ${repositoryStatusLabels[repository.status] || repository.status}`,
      repository.id,
    );
    option.disabled = !repository.enabled || repository.status !== "ready";
    taskRepository.append(option);
  });
  const body = document.getElementById("repositories-body");
  body.replaceChildren();
  if (!repositories.length) {
    const row = node("tr");
    const cell = node("td", "empty", "Репозитории ещё не зарегистрированы.");
    cell.colSpan = 5;
    row.append(cell);
    body.append(row);
    return;
  }
  repositories.forEach((repository) => {
    const row = node("tr");
    const nameCell = node("td");
    nameCell.append(node("span", "task-title", repository.name));
    nameCell.append(node("span", "task-meta", repository.remote_url));
    const providerCell = node("td");
    providerCell.append(node("span", "pill", repositoryProviderLabels[repository.provider] || repository.provider));
    const statusCell = node("td");
    statusCell.append(node("span", `pill ${repository.status}`, repositoryStatusLabels[repository.status] || repository.status));
    if (repository.default_branch && repository.last_known_commit) {
      statusCell.append(node("span", "task-meta", `${repository.default_branch} · ${repository.last_known_commit.slice(0, 12)}`));
    }
    if (repository.last_sync_error_code) {
      statusCell.append(node("span", "task-meta", `Код: ${repository.last_sync_error_code}`));
    }
    const assuranceCell = node("td");
    assuranceCell.append(node("span", "pill", assuranceLabels[repository.assurance_tier] || repository.assurance_tier));
    if (repository.assurance_profile) assuranceCell.append(node("span", "task-meta", ` · ${repository.assurance_profile}`));
    const accessCell = node("td");
    accessCell.append(node("span", `pill ${repository.enabled ? "done" : "failed"}`, repository.enabled ? "Включён" : "Отключён"));
    const toggle = node("button", "text-button", repository.enabled ? "Отключить" : "Включить");
    toggle.addEventListener("click", () => setRepositoryEnabled(repository));
    accessCell.append(toggle);
    if (repository.enabled) {
      const validate = node("button", "text-button", "Проверить");
      validate.disabled = repository.status === "validating";
      validate.addEventListener("click", () => validateRepository(repository));
      accessCell.append(validate);
    }
    row.append(nameCell, providerCell, statusCell, assuranceCell, accessCell);
    body.append(row);
  });
}

async function startExecution(taskId) {
  try {
    await api(`/api/tasks/${taskId}/execute`, { method: "POST" });
    toast("AI Orchestra начал безопасную подготовку рабочего каталога");
    await refreshAll();
  } catch (error) { toast(error.message, true); }
}

async function abortExecution(id) {
  if (!window.confirm("Остановить выполнение этой задачи?")) return;
  try {
    await api(`/api/executions/${id}/abort`, { method: "POST" });
    toast("Запрос на остановку сохранен");
    await refreshAll();
  } catch (error) { toast(error.message, true); }
}

function formatElapsed(seconds) {
  const total = Number(seconds || 0);
  const min = Math.floor(total / 60);
  const sec = total % 60;
  return min ? `${min} мин ${sec} сек` : `${sec} сек`;
}

async function showExecutionProgress(id) {
  progressExecutionId = id;
  document.getElementById("progress-modal").classList.remove("hidden");
  await loadExecutionProgress(id);
}

async function loadExecutionProgress(id) {
  const progress = await api(`/api/executions/${id}/progress`);
  const summary = document.getElementById("progress-summary");
  const activityLabel = progress.cancel_requested_at
    ? "● остановка запрошена"
    : progress.status === "running"
      ? "● активен"
      : progress.status === "queued"
        ? "● в очереди"
        : progress.status === "preparing"
          ? "● подготовка каталога"
        : executionLabels[progress.status] || progress.status;
  const summaryItems = [
    node("span", "", activityLabel),
    node("span", "", `Этап: ${progress.stage}`),
    node("span", "", `OpenCode: ${progress.session_state}`),
    node("span", "", `Время: ${formatElapsed(progress.elapsed_seconds)}`),
    node("span", "", `Lease generation: ${progress.lease_generation}`),
  ];
  if (progress.heartbeat_at) summaryItems.push(node("span", "", `Heartbeat: ${formatDate(progress.heartbeat_at)}`));
  if (progress.deadline_at) summaryItems.push(node("span", "", `Deadline: ${formatDate(progress.deadline_at)}`));
  if (progress.error) summaryItems.push(node("span", "", `Ошибка: ${progress.error}`));
  summary.replaceChildren(...summaryItems);
  const feed = document.getElementById("progress-feed");
  feed.replaceChildren();
  if (!progress.items.length) {
    const emptyText = progress.error
      || (progress.status === "preparing"
        ? "Workspace Manager проверяет commit и создаёт изолированный каталог."
        : progress.status === "queued"
          ? "Каталог проверен; запуск ожидает dispatch worker."
          : "Текстовых сообщений пока нет.");
    feed.append(node("p", "empty", emptyText));
    return;
  }
  progress.items.forEach((item) => {
    const card = node("article", "progress-item");
    const header = node("header");
    header.append(node("strong", "", item.role || "assistant"));
    header.append(node("span", "", item.model || ""));
    card.append(header, node("pre", "", item.text));
    feed.append(card);
  });
}

function showExecutionResult(text) {
  document.getElementById("execution-result").textContent = text;
  document.getElementById("result-modal").classList.remove("hidden");
}

async function loadTasks() {
  const tasks = await api("/api/tasks?limit=50");
  tasksById = new Map(tasks.map((task) => [task.id, task]));
  const body = document.getElementById("tasks-body");
  body.replaceChildren();
  if (!tasks.length) {
    const row = node("tr");
    const cell = node("td", "empty", "Пока нет задач — создайте первую.");
    cell.colSpan = 6;
    row.append(cell);
    body.append(row);
    return;
  }
  tasks.forEach((task) => {
    const row = node("tr");
    const titleCell = node("td");
    titleCell.append(node("span", "task-title", task.title));
    titleCell.append(node("span", "task-meta", `${task.project} · ${formatDate(task.created_at)}`));
    const repositoryCell = node("td");
    const repositorySelect = node("select", "status-select");
    repositorySelect.append(new Option("Не назначен", ""));
    repositoriesById.forEach((repository) => {
      repositorySelect.append(new Option(repository.name, repository.id));
    });
    repositorySelect.value = task.repository_id || "";
    const run = executionsByTask.get(task.id);
    repositorySelect.disabled = Boolean(run && ["preparing", "queued", "running"].includes(run.status));
    repositorySelect.addEventListener("change", async () => {
      repositorySelect.disabled = true;
      try {
        await api(`/api/tasks/${task.id}/repository`, {
          method: "PATCH",
          body: JSON.stringify({ repository_id: repositorySelect.value || null }),
        });
        toast("Репозиторий задачи обновлён");
        await refreshAll();
      } catch (error) {
        repositorySelect.value = task.repository_id || "";
        repositorySelect.disabled = false;
        toast(error.message, true);
      }
    });
    repositoryCell.append(repositorySelect);
    if (task.repository_id) {
      const repository = repositoriesById.get(task.repository_id);
      repositoryCell.append(node("span", "task-meta", repository?.status === "ready" ? "commit будет зафиксирован при запуске" : "репозиторий пока не готов"));
    }
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
    const executionCell = node("td");
    if (!run && task.domain === "development" && task.status !== "done") {
      const repository = repositoriesById.get(task.repository_id);
      if (repository?.enabled && repository.status === "ready") {
        const start = node("button", "button button-small button-secondary", "Запустить");
        start.addEventListener("click", () => startExecution(task.id));
        executionCell.append(start);
      } else {
        executionCell.append(node("span", "task-meta", "Нужен ready-репозиторий"));
      }
    } else if (run) {
      executionCell.append(node("span", `pill ${run.status}`, executionLabels[run.status] || run.status));
      if (["preparing", "queued", "running"].includes(run.status)) {
        const actions = node("div", "stack-actions");
        const progress = node("button", "text-button", "Ход работы");
        const refresh = node("button", "text-button", "Обновить");
        refresh.addEventListener("click", refreshAll);
        progress.addEventListener("click", () => showExecutionProgress(run.id));
        actions.append(progress, refresh);
        if (!run.cancel_requested_at) {
          const stop = node("button", "text-button", "Остановить");
          stop.addEventListener("click", () => abortExecution(run.id));
          actions.append(stop);
        }
        executionCell.append(actions);
      }
      if (run.result) {
        const result = node("button", "text-button", "Результат");
        result.addEventListener("click", () => showExecutionResult(run.result));
        executionCell.append(result);
      }
      if (!["preparing", "queued", "running"].includes(run.status) && task.status !== "done") {
        const repository = repositoriesById.get(task.repository_id);
        if (repository?.enabled && repository.status === "ready") {
          const restart = node("button", "text-button", "Запустить снова");
          restart.addEventListener("click", () => startExecution(task.id));
          executionCell.append(restart);
        }
      }
    } else {
      executionCell.append(node("span", "task-meta", "V1: development"));
    }
    row.append(titleCell, repositoryCell, domainCell, riskCell, statusCell, executionCell);
    body.append(row);
  });
}

async function requestWorkspaceCleanup(workspace) {
  if (!window.confirm("Удалить доказанно чистый рабочий каталог?")) return;
  try {
    await api(`/api/workspaces/${workspace.id}/cleanup`, {
      method: "POST",
      body: JSON.stringify({ expected_version: workspace.version }),
    });
    toast("Безопасная очистка поставлена в очередь");
    await refreshAll();
  } catch (error) { toast(error.message, true); }
}

async function loadWorkspaces() {
  const workspaces = await api("/api/workspaces?limit=100");
  const body = document.getElementById("workspaces-body");
  body.replaceChildren();
  if (!workspaces.length) {
    const row = node("tr");
    const cell = node("td", "empty", "Рабочие каталоги появятся после запуска задач.");
    cell.colSpan = 7;
    row.append(cell);
    body.append(row);
    return;
  }
  workspaces.forEach((workspace) => {
    const row = node("tr");
    const taskCell = node("td");
    taskCell.append(node("span", "task-title", tasksById.get(workspace.task_id)?.title || workspace.task_id));
    taskCell.append(node("span", "task-meta mono", workspace.id));
    const repositoryCell = node("td", "", repositoriesById.get(workspace.repository_id)?.name || workspace.repository_id);
    const commitCell = node("td", "mono", workspace.base_commit.slice(0, 12));
    const pathCell = node("td", "mono", workspace.opencode_path);
    const statusCell = node("td");
    statusCell.append(node("span", `pill ${workspace.status}`, workspaceStatusLabels[workspace.status] || workspace.status));
    if (workspace.last_error_code) statusCell.append(node("span", "task-meta mono", workspace.last_error_code));
    const changesCell = node("td", "", workspace.has_changes === null ? "—" : workspace.has_changes ? `${workspace.changed_file_count} файл(ов)` : "Чисто");
    const actionCell = node("td");
    const clean = workspace.status === "retained"
      && workspace.has_changes === false
      && workspace.current_head_commit === workspace.base_commit
      && workspace.current_tree === workspace.initial_tree;
    if (clean) {
      const remove = node("button", "text-button", "Очистить");
      remove.addEventListener("click", () => requestWorkspaceCleanup(workspace));
      actionCell.append(remove);
    } else {
      actionCell.append(node("span", "task-meta", workspace.status === "retained" ? "Требуется ревью" : "—"));
    }
    row.append(taskCell, repositoryCell, commitCell, pathCell, statusCell, changesCell, actionCell);
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
    await loadExecutions();
    await loadRepositories();
    await Promise.all([loadSummary(), loadTasks(), loadCapabilityGuard(), loadApprovals(), loadBudgets(), loadAudit()]);
    await loadWorkspaces();
  } catch (error) { toast(error.message, true); }
}

document.getElementById("close-result").addEventListener("click", () => document.getElementById("result-modal").classList.add("hidden"));
document.getElementById("close-progress").addEventListener("click", () => { progressExecutionId = null; document.getElementById("progress-modal").classList.add("hidden"); });

document.getElementById("show-task-form").addEventListener("click", () => document.getElementById("task-form-panel").classList.remove("hidden"));
document.getElementById("hide-task-form").addEventListener("click", () => document.getElementById("task-form-panel").classList.add("hidden"));
document.getElementById("show-repository-form").addEventListener("click", () => document.getElementById("repository-form-panel").classList.remove("hidden"));
document.getElementById("hide-repository-form").addEventListener("click", () => document.getElementById("repository-form-panel").classList.add("hidden"));
document.querySelectorAll("[data-refresh]").forEach((button) => button.addEventListener("click", refreshAll));
document.getElementById("repository-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const status = document.getElementById("repository-form-status");
  const values = Object.fromEntries(new FormData(form).entries());
  const payload = {
    name: values.name,
    remote_url: values.remote_url,
    enabled: form.elements.enabled.checked,
    execution_profile: "development",
    assurance_tier: values.assurance_tier,
  };
  if (values.auth_profile_ref?.trim()) payload.auth_profile_ref = values.auth_profile_ref.trim();
  if (values.assurance_profile?.trim()) payload.assurance_profile = values.assurance_profile.trim();
  try {
    status.textContent = "";
    await api("/api/repositories", { method: "POST", body: JSON.stringify(payload) });
    form.reset();
    document.getElementById("repository-form-panel").classList.add("hidden");
    toast("Репозиторий зарегистрирован и ожидает проверки");
    await refreshAll();
  } catch (error) { status.textContent = error.message; }
});
document.getElementById("task-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const status = document.getElementById("task-form-status");
  const payload = Object.fromEntries(new FormData(form).entries());
  if (!payload.repository_id) delete payload.repository_id;
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

setInterval(async () => {
  const active = [...executionsByTask.values()].filter((run) => ["preparing", "queued", "running"].includes(run.status));
  if (!active.length) return;
  await refreshAll();
}, 10000);

setInterval(async () => {
  if (progressExecutionId) {
    try { await loadExecutionProgress(progressExecutionId); } catch (_) { /* transient */ }
  }
}, 5000);
