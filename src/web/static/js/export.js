document.addEventListener("DOMContentLoaded", () => {
  const ui = window.RubiiUI;
  const state = {
    reportType: "daily",
    scopes: [],
    dates: [],
    items: [],
    selectedScope: "__all__",
    selectedDate: null,
    selectedKeys: new Set(),
  };

  const reportTypeSelect = document.getElementById("reportType");
  const scopeSelect = document.getElementById("scopeSelect");
  const dateSelect = document.getElementById("reportDate");
  const exportList = document.getElementById("exportList");
  const exportSelectedButton = document.getElementById("exportSelected");
  const selectAllVisibleButton = document.getElementById("selectAllVisible");
  const clearSelectionButton = document.getElementById("clearSelection");

  function selectedItems() {
    return state.items
      .filter((item) => state.selectedKeys.has(item.export_key))
      .map((item) => ({ report_type: item.report_type, id: item.id }));
  }

  function updateSummary() {
    const count = state.selectedKeys.size;
    ui.setText("selectedCount", `${count} selected`);
    if (exportSelectedButton) {
      exportSelectedButton.disabled = count === 0;
    }
    const label = state.reportType === "hourly" ? "Hourly reports" : "Daily reports";
    ui.setText("exportTitle", `${label} export`);
    const meta =
      state.selectedDate && state.items.length
        ? `${state.selectedDate} · ${state.items.length} exportable reports`
        : state.selectedDate
          ? `${state.selectedDate} · 0 exportable reports`
          : "Choose report type, scope, and date to load exportable reports.";
    ui.setText("exportMeta", meta);
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

  function renderItems() {
    if (!exportList) {
      return;
    }
    if (!state.items.length) {
      exportList.innerHTML = '<div class="empty">No exportable reports found for the current filter.</div>';
      updateSummary();
      return;
    }

    exportList.innerHTML = state.items
      .map((item) => {
        const checked = state.selectedKeys.has(item.export_key) ? " checked" : "";
        const metrics = `Messages ${item.source_message_count} / Candidates ${item.candidate_message_count}`;
        return `
          <article class="export-item" data-export-key="${ui.escapeHtml(item.export_key)}">
            <label class="export-item-check">
              <input type="checkbox" data-export-toggle="${ui.escapeHtml(item.export_key)}"${checked} />
            </label>
            <div class="export-item-main">
              <div class="export-item-top">
                <div>
                  <strong>${ui.escapeHtml(item.title)}</strong>
                  <span>${ui.escapeHtml(item.scope_label || item.subtitle || "--")}</span>
                </div>
                <div class="export-item-badge export-item-badge-${ui.escapeHtml(item.report_type)}">
                  ${ui.escapeHtml(item.report_type)}
                </div>
              </div>
              <p>${ui.escapeHtml(item.window_label || metrics)}</p>
              <small>${ui.escapeHtml(metrics)}</small>
            </div>
            <div class="export-item-actions">
              <button type="button" class="action-button action-button-compact" data-export-single="${ui.escapeHtml(item.export_key)}">Export</button>
            </div>
          </article>
        `;
      })
      .join("");
    updateSummary();
  }

  async function triggerDownload(items) {
    if (!items.length) {
      return;
    }
    const response = await fetch("/export/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }),
    });
    if (!response.ok) {
      throw new Error(`Export failed: ${response.status} ${response.statusText}`);
    }
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const matched = disposition.match(/filename=\"?([^\";]+)\"?/i);
    const filename = matched ? matched[1] : "reports-export.zip";
    const objectUrl = window.URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.URL.revokeObjectURL(objectUrl);
  }

  async function loadCatalog() {
    const params = new URLSearchParams();
    params.set("report_type", state.reportType);
    if (state.selectedScope) {
      params.set("scope_key", state.selectedScope);
    }
    if (state.selectedDate) {
      params.set("report_date", state.selectedDate);
    }
    const payload = await ui.fetchJson(`/api/export/catalog?${params.toString()}`);
    state.items = payload.items || [];
    state.selectedKeys = new Set();
    renderFilters(payload);
    renderItems();
  }

  if (reportTypeSelect) {
    reportTypeSelect.addEventListener("change", async (event) => {
      state.reportType = event.target.value;
      state.selectedDate = null;
      try {
        await loadCatalog();
      } catch (error) {
        console.error(error);
      }
    });
  }

  if (scopeSelect) {
    scopeSelect.addEventListener("change", async (event) => {
      state.selectedScope = event.target.value;
      state.selectedDate = null;
      try {
        await loadCatalog();
      } catch (error) {
        console.error(error);
      }
    });
  }

  if (dateSelect) {
    dateSelect.addEventListener("change", async (event) => {
      state.selectedDate = event.target.value;
      try {
        await loadCatalog();
      } catch (error) {
        console.error(error);
      }
    });
  }

  if (selectAllVisibleButton) {
    selectAllVisibleButton.addEventListener("click", () => {
      state.items.forEach((item) => state.selectedKeys.add(item.export_key));
      renderItems();
    });
  }

  if (clearSelectionButton) {
    clearSelectionButton.addEventListener("click", () => {
      state.selectedKeys = new Set();
      renderItems();
    });
  }

  if (exportSelectedButton) {
    exportSelectedButton.addEventListener("click", async () => {
      try {
        await triggerDownload(selectedItems());
      } catch (error) {
        console.error(error);
      }
    });
  }

  if (exportList) {
    exportList.addEventListener("change", (event) => {
      const input = event.target.closest("[data-export-toggle]");
      if (!input) {
        return;
      }
      const key = input.getAttribute("data-export-toggle");
      if (input.checked) {
        state.selectedKeys.add(key);
      } else {
        state.selectedKeys.delete(key);
      }
      updateSummary();
    });

    exportList.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-export-single]");
      if (!button) {
        return;
      }
      const key = button.getAttribute("data-export-single");
      const item = state.items.find((entry) => entry.export_key === key);
      if (!item) {
        return;
      }
      try {
        await triggerDownload([{ report_type: item.report_type, id: item.id }]);
      } catch (error) {
        console.error(error);
      }
    });
  }

  loadCatalog().catch((error) => {
    console.error(error);
    if (exportList) {
      exportList.innerHTML = '<div class="empty">Failed to load exportable reports.</div>';
    }
  });
});
