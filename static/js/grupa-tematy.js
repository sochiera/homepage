(() => {
  "use strict";

  const form = document.querySelector("#proposal-form");
  const API = form.dataset.apiBase;
  const topic = document.querySelector("#topic");
  const count = document.querySelector("#topic-count");
  const proposalStatus = document.querySelector("#proposal-status");
  const loginForm = document.querySelector("#login-form");
  const passwordInput = document.querySelector("#group-password");
  const loginStatus = document.querySelector("#login-status");
  const privateArea = document.querySelector("#private-area");
  const list = document.querySelector("#proposal-list");
  const listStatus = document.querySelector("#list-status");
  const logoutButton = document.querySelector("#logout-button");
  let csrfToken = "";
  let sessionTimer = null;

  function status(target, message, state = "") {
    target.textContent = message;
    target.dataset.state = state;
  }

  function expireSession() {
    if (sessionTimer) clearTimeout(sessionTimer);
    sessionTimer = null;
    csrfToken = "";
    list.replaceChildren();
    privateArea.hidden = true;
    loginForm.hidden = false;
    status(loginStatus, "Sesja wygasła. Podaj wspólne hasło ponownie.", "error");
    refreshCsrf().catch(() => {});
  }

  function scheduleSessionExpiry(seconds) {
    if (sessionTimer) clearTimeout(sessionTimer);
    if (Number.isFinite(seconds) && seconds > 0) {
      sessionTimer = setTimeout(expireSession, seconds * 1000);
    }
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body) headers.set("Content-Type", "application/json");
    if (options.method && options.method !== "GET" && csrfToken) headers.set("X-CSRF-Token", csrfToken);
    const response = await fetch(`${API}${path}`, {
      ...options,
      headers,
      credentials: "same-origin",
      cache: "no-store",
    });
    const body = response.status === 204 ? null : await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 401 && !privateArea.hidden) expireSession();
      throw new Error(body?.detail || "Nie udało się wykonać tej czynności.");
    }
    return body;
  }

  async function refreshCsrf() {
    const result = await api("/csrf");
    csrfToken = result.csrfToken;
    if (result.expiresIn) scheduleSessionExpiry(result.expiresIn);
  }

  function createAssignmentForm(proposal) {
    const form = document.createElement("form");
    form.className = "assignment-form";
    form.setAttribute("aria-label", "Przypisz osobę");

    const label = document.createElement("label");
    label.textContent = "Osoba prowadząca";
    const input = document.createElement("input");
    input.name = "assignee";
    input.type = "text";
    input.maxLength = 100;
    input.autocomplete = "off";
    input.value = proposal.assignee || "";
    input.placeholder = "Wpisz imię i nazwisko";
    const controls = document.createElement("div");
    controls.className = "topic-assignment-controls";
    const save = document.createElement("button");
    save.type = "submit";
    save.textContent = "Zapisz osobę";
    controls.append(save);

    if (proposal.assignee) {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "topic-remove-button";
      remove.textContent = "Usuń przypisanie";
      remove.addEventListener("click", () => saveAssignment(proposal.id, null));
      controls.append(remove);
    }

    form.append(label, input, controls);
    form.addEventListener("submit", event => {
      event.preventDefault();
      const name = input.value.trim();
      saveAssignment(proposal.id, name || null);
    });
    return form;
  }

  async function saveAssignment(id, name) {
    status(listStatus, "Zapisuję przypisanie…");
    try {
      await api(`/proposals/${encodeURIComponent(id)}/assignment`, {
        method: "PUT",
        body: JSON.stringify({ name }),
      });
      status(listStatus, name ? "Przypisanie zapisane." : "Przypisanie usunięte.", "success");
      await loadProposals();
    } catch (error) {
      status(listStatus, error.message, "error");
    }
  }

  function renderProposal(proposal) {
    const card = document.createElement("article");
    card.className = "topic-card";
    const heading = document.createElement("h3");
    heading.textContent = new Date(proposal.createdAt).toLocaleDateString("pl-PL", { year: "numeric", month: "long", day: "numeric" });
    const text = document.createElement("p");
    text.className = "topic-card-text";
    text.textContent = proposal.topic;
    const assignment = document.createElement("div");
    assignment.className = "topic-card-assignment";
    if (proposal.assignee) {
      const assigned = document.createElement("p");
      assigned.className = "topic-assignee";
      assigned.textContent = `Osoba: ${proposal.assignee}`;
      assignment.append(assigned);
    }
    assignment.append(createAssignmentForm(proposal));
    card.append(heading, text, assignment);
    return card;
  }

  async function loadProposals() {
    const result = await api("/proposals");
    list.replaceChildren();
    if (!result.proposals.length) {
      const empty = document.createElement("p");
      empty.className = "topic-empty";
      empty.textContent = "Nie ma jeszcze żadnych propozycji.";
      list.append(empty);
    } else {
      for (const proposal of result.proposals) list.append(renderProposal(proposal));
    }
    privateArea.hidden = false;
    loginForm.hidden = true;
  }

  topic.addEventListener("input", () => {
    count.textContent = `${topic.value.length} / 1200`;
  });

  form.addEventListener("submit", async event => {
    event.preventDefault();
    status(proposalStatus, "Wysyłam propozycję…");
    try {
      await api("/proposals", { method: "POST", body: JSON.stringify({ topic: topic.value }) });
      form.reset();
      count.textContent = "0 / 1200";
      status(proposalStatus, "Dziękujemy — propozycja została zapisana.", "success");
    } catch (error) {
      status(proposalStatus, error.message, "error");
    }
  });

  loginForm.addEventListener("submit", async event => {
    event.preventDefault();
    status(loginStatus, "Sprawdzam hasło…");
    try {
      const result = await api("/session", { method: "POST", body: JSON.stringify({ password: passwordInput.value }) });
      csrfToken = result.csrfToken;
      passwordInput.value = "";
      status(loginStatus, "");
      await loadProposals();
      status(listStatus, "Lista jest dostępna tylko w tej sesji.");
    } catch (error) {
      passwordInput.value = "";
      status(loginStatus, error.message, "error");
    }
  });

  logoutButton.addEventListener("click", async () => {
    try {
      await api("/session", { method: "DELETE" });
      if (sessionTimer) clearTimeout(sessionTimer);
      sessionTimer = null;
      csrfToken = "";
      list.replaceChildren();
      privateArea.hidden = true;
      loginForm.hidden = false;
      status(loginStatus, "Wylogowano.", "success");
      await refreshCsrf();
    } catch (error) {
      status(listStatus, error.message, "error");
    }
  });

  (async () => {
    try {
      await refreshCsrf();
      try {
        await loadProposals();
        status(listStatus, "Lista jest dostępna tylko w tej sesji.");
      } catch {
        privateArea.hidden = true;
        loginForm.hidden = false;
      }
    } catch {
      status(proposalStatus, "Nie można teraz połączyć się z formularzem.", "error");
    }
  })();
})();
