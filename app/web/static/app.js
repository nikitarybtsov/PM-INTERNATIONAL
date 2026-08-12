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

function splitLines(value) {
  return (value || "")
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
}
