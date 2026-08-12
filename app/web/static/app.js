/* Небольшой помощник для вызовов API из панели. */

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let data = null;
  const text = await res.text();
  try { data = text ? JSON.parse(text) : null; } catch (_) { data = { raw: text }; }
  if (!res.ok) {
    const detail = (data && (data.detail || data.message)) || res.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

function flash(message, kind = "ok") {
  let box = document.getElementById("flash");
  if (!box) {
    box = document.createElement("div");
    box.id = "flash";
    const main = document.querySelector("main");
    main.insertBefore(box, main.firstChild);
  }
  box.className = `notice ${kind}`;
  box.textContent = message;
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function act(path, body, successMessage, reload = true) {
  try {
    const result = await api(path, {
      method: "POST",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    flash(successMessage || "Готово", "ok");
    if (reload) setTimeout(() => window.location.reload(), 700);
    return result;
  } catch (err) {
    flash(err.message, "err");
    throw err;
  }
}

// --- одобрение заявок оператором -------------------------------------------
// В боевом режиме без этого ордер не уйдёт на биржу, поэтому подтверждаем
// намерение: случайный клик не должен двигать реальные деньги.
async function approveDecision(roundId, participant) {
  const live = document.body.dataset.executionMode === "LIVE";
  const question = live
    ? `Одобрить ставку участника ${participant}?\n\nЭТО РЕАЛЬНЫЕ ДЕНЬГИ: ордер уйдёт на Polymarket.`
    : `Одобрить заявку участника ${participant}?`;
  if (!window.confirm(question)) return;
  await act(
    `/api/rounds/${roundId}/decisions/${participant}/approve`,
    undefined,
    `Заявка ${participant} одобрена`
  );
}

async function revokeApproval(roundId, participant) {
  try {
    await api(`/api/rounds/${roundId}/decisions/${participant}/approve`, {
      method: "DELETE",
    });
    flash(`Одобрение ${participant} снято`, "ok");
    setTimeout(() => window.location.reload(), 700);
  } catch (err) {
    flash(err.message, "err");
  }
}

async function prepareRound(roundId) {
  await act(`/api/rounds/${roundId}/prepare`, undefined, "Заявки просчитаны");
}

function splitLines(value) {
  return (value || "")
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
}
