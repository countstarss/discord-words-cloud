document.addEventListener("DOMContentLoaded", () => {
  const ui = window.RubiiUI;
  const state = {
    scopes: [],
    dates: [],
    selectedScope: null,
    selectedDate: null,
    page: 1,
    pageSize: 20,
    totalPages: 0,
    totalItems: 0,
    messages: [],
  };

  const scopeSelect = document.getElementById("scopeSelect");
  const dateSelect = document.getElementById("reportDate");
  const messageList = document.getElementById("messageList");
  const prevPage = document.getElementById("prevPage");
  const nextPage = document.getElementById("nextPage");

  function currentScopeLabel() {
    const match = state.scopes.find((scope) => scope.scope_key === state.selectedScope);
    return match ? match.label : "No scope";
  }

  function renderFilters(payload) {
    state.scopes = payload.available_scopes || [];
    state.dates = payload.available_dates || [];
    state.selectedScope = payload.selected_scope || state.selectedScope;
    state.selectedDate = payload.selected_date || state.selectedDate;

    ui.populateSelect(
      scopeSelect,
      state.scopes.map((scope) => ({ value: scope.scope_key, label: scope.label })),
      state.selectedScope,
      "No scopes"
    );
    ui.populateSelect(
      dateSelect,
      state.dates.map((value) => ({ value, label: value })),
      state.selectedDate,
      "No dates"
    );
  }

  function renderHeader() {
    ui.setText("messageTitle", currentScopeLabel());
    const meta =
      state.selectedDate && state.totalItems
        ? `${state.selectedDate} · ${Number(state.totalItems).toLocaleString()} messages`
        : state.selectedDate
          ? `${state.selectedDate} · 0 messages`
          : "Choose a scope and date to inspect collected messages.";
    ui.setText("messageMeta", meta);
    ui.setText(
      "pageIndicator",
      state.totalPages ? `Page ${state.page} / ${state.totalPages}` : "Page 0 / 0"
    );
    if (prevPage) {
      prevPage.disabled = state.page <= 1;
    }
    if (nextPage) {
      nextPage.disabled = !state.totalPages || state.page >= state.totalPages;
    }
  }

  function renderMessages() {
    if (!messageList) {
      return;
    }
    if (!state.messages.length) {
      messageList.innerHTML = '<div class="empty">No messages found for the selected scope and date.</div>';
      return;
    }
    messageList.innerHTML = state.messages
      .map((message) => `
        <article class="message-item">
          <div class="message-row">
            <div class="message-cell message-cell-content">${ui.escapeHtml(message.content || "")}</div>
            <div class="message-cell message-cell-datetime">${ui.escapeHtml(ui.formatDateTime(message.created_at))}</div>
            <div class="message-cell message-cell-id">#${ui.escapeHtml(String(message.message_id || "--"))}</div>
          </div>
        </article>
      `)
      .join("");
  }

  async function loadMessages() {
    const params = new URLSearchParams();
    if (state.selectedScope) {
      params.set("scope_key", state.selectedScope);
    }
    if (state.selectedDate) {
      params.set("report_date", state.selectedDate);
    }
    params.set("page", String(state.page));
    params.set("page_size", String(state.pageSize));

    const payload = await ui.fetchJson(`/api/messages/browser?${params.toString()}`);
    state.messages = payload.messages || [];
    state.page = Number(payload.pagination?.page || 1);
    state.totalPages = Number(payload.pagination?.total_pages || 0);
    state.totalItems = Number(payload.pagination?.total_items || 0);
    renderFilters(payload);
    renderHeader();
    renderMessages();
  }

  if (scopeSelect) {
    scopeSelect.addEventListener("change", async (event) => {
      state.selectedScope = event.target.value;
      state.selectedDate = null;
      state.page = 1;
      try {
        await loadMessages();
      } catch (error) {
        console.error(error);
      }
    });
  }

  if (dateSelect) {
    dateSelect.addEventListener("change", async (event) => {
      state.selectedDate = event.target.value;
      state.page = 1;
      try {
        await loadMessages();
      } catch (error) {
        console.error(error);
      }
    });
  }

  if (prevPage) {
    prevPage.addEventListener("click", async () => {
      if (state.page <= 1) {
        return;
      }
      state.page -= 1;
      try {
        await loadMessages();
      } catch (error) {
        console.error(error);
      }
    });
  }

  if (nextPage) {
    nextPage.addEventListener("click", async () => {
      if (!state.totalPages || state.page >= state.totalPages) {
        return;
      }
      state.page += 1;
      try {
        await loadMessages();
      } catch (error) {
        console.error(error);
      }
    });
  }

  loadMessages().catch((error) => {
    console.error(error);
    if (messageList) {
      messageList.innerHTML = '<div class="empty">Failed to load messages.</div>';
    }
  });
});
