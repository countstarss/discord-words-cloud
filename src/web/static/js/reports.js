document.addEventListener("DOMContentLoaded", () => {
  const ui = window.RubiiUI;
  const state = {
    reports: [],
    scopes: [],
    selectedScope: "global",
    selectedDate: null,
  };

  const scopeSelect = document.getElementById("scopeSelect");
  const reportSelect = document.getElementById("reportDate");
  const reportList = document.getElementById("reportList");

  function currentScopeLabel() {
    const match = state.scopes.find((scope) => scope.scope_key === state.selectedScope);
    return match ? match.label : "All channels";
  }

  function renderTopStats(summary) {
    ui.setText("reportsTotalReports", Number(summary.total_reports || 0).toLocaleString());
    ui.setText("reportsLatestDate", summary.latest_report_date || "--");
    ui.setText("reportsHourlyToday", Number(summary.total_hourly_reports_today || 0).toLocaleString());
    ui.setText("reportsScopeCount", Number((summary.available_scopes || []).length || 0).toLocaleString());
  }

  function renderScopes(summary) {
    const availableScopes = summary.available_scopes || [];
    state.scopes = availableScopes.length
      ? availableScopes
      : [{ scope_key: "global", label: "All channels" }];
    if (!state.scopes.some((scope) => scope.scope_key === state.selectedScope)) {
      state.selectedScope = state.scopes[0].scope_key;
    }
    ui.populateSelect(
      scopeSelect,
      state.scopes.map((scope) => ({ value: scope.scope_key, label: scope.label })),
      state.selectedScope,
      "No scopes"
    );
    ui.setText("currentScopeLabel", currentScopeLabel());
    ui.setText("reportScopeLabel", currentScopeLabel());
  }

  function syncReportSelector() {
    if (!state.reports.length) {
      state.selectedDate = null;
      ui.populateSelect(reportSelect, [], null, "No reports");
      return;
    }
    if (!state.selectedDate || !state.reports.some((report) => report.report_date === state.selectedDate)) {
      state.selectedDate = state.reports[0].report_date;
    }
    ui.populateSelect(
      reportSelect,
      state.reports.map((report) => ({ value: report.report_date, label: report.report_date })),
      state.selectedDate,
      "No reports"
    );
  }

  function renderReportList() {
    if (!reportList) {
      return;
    }
    if (!state.reports.length) {
      reportList.innerHTML = '<div class="empty">No daily reports available for the selected scope.</div>';
      return;
    }
    reportList.innerHTML = state.reports
      .map((report) => {
        const activeClass = report.report_date === state.selectedDate ? " is-active" : "";
        const preview = `Messages ${report.source_message_count} / Candidates ${report.candidate_message_count}`;
        return `
          <button type="button" class="report-list-item${activeClass}" data-report-date="${ui.escapeHtml(report.report_date)}">
            <small>${ui.escapeHtml(report.scope_key || state.selectedScope)}</small>
            <strong>${ui.escapeHtml(report.report_date)}</strong>
            <p>${ui.escapeHtml(preview)}</p>
          </button>
        `;
      })
      .join("");
  }

  function renderSelectedReport() {
    const report = state.reports.find((item) => item.report_date === state.selectedDate);
    ui.setText("currentScopeLabel", currentScopeLabel());
    ui.renderDailyReport(report, {
      titleId: "reportTitle",
      windowId: "reportWindow",
      countsId: "reportCounts",
      messagesId: "reportMessages",
      candidateMessagesId: "reportCandidateMessages",
      bodyId: "reportBody",
      scopeLabelId: "reportScopeLabel",
      scopeLabel: currentScopeLabel(),
      emptyTitle: "No report selected",
      emptyBody: "Choose a scope and report date to inspect the markdown output.",
      titlePrefix: "Daily Report",
    });
  }

  async function loadReports() {
    const reportPayload = await ui.fetchJson(`/daily-report?scope_key=${encodeURIComponent(state.selectedScope)}`);
    state.reports = reportPayload.reports || [];
    syncReportSelector();
    renderReportList();
    renderSelectedReport();
  }

  async function initializePage() {
    const summary = await ui.fetchJson("/api/dashboard");
    renderTopStats(summary);
    renderScopes(summary);
    await loadReports();
  }

  if (scopeSelect) {
    scopeSelect.addEventListener("change", async (event) => {
      state.selectedScope = event.target.value;
      state.selectedDate = null;
      try {
        await loadReports();
      } catch (error) {
        console.error(error);
      }
    });
  }

  if (reportSelect) {
    reportSelect.addEventListener("change", (event) => {
      state.selectedDate = event.target.value;
      renderReportList();
      renderSelectedReport();
    });
  }

  if (reportList) {
    reportList.addEventListener("click", (event) => {
      const button = event.target.closest("[data-report-date]");
      if (!button) {
        return;
      }
      state.selectedDate = button.getAttribute("data-report-date");
      if (reportSelect) {
        reportSelect.value = state.selectedDate;
      }
      renderReportList();
      renderSelectedReport();
    });
  }

  initializePage().catch((error) => {
    console.error(error);
    if (reportList) {
      reportList.innerHTML = '<div class="empty">Failed to load daily report list.</div>';
    }
    const reportBody = document.getElementById("reportBody");
    if (reportBody) {
      reportBody.innerHTML = '<div class="empty">Failed to load report data.</div>';
    }
  });
});
