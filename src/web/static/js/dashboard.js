document.addEventListener("DOMContentLoaded", () => {
  const ui = window.RubiiUI;
  const state = { reports: [], selectedDate: null };
  const reportSelect = document.getElementById("reportDate");

  function renderSummary(summary) {
    ui.setText("totalMessages", Number(summary.total_messages || 0).toLocaleString());
    ui.setText("totalReports", Number(summary.total_reports || 0).toLocaleString());
    ui.setText("activeUsers", Number(summary.active_users_24h || 0).toLocaleString());
    ui.setText("latestReport", summary.latest_report_date || "--");
    ui.setText("latestMessageAt", ui.formatDateTime(summary.latest_message_at));
    ui.setText("hourlyReportsToday", Number(summary.total_hourly_reports_today || 0).toLocaleString());
    ui.setText("configuredChannelCount", `${summary.configured_channel_count || 0} configured channels`);
    ui.renderTargets("targetTree", summary.configured_regions || []);
  }

  function syncReportSelector() {
    if (!state.reports.length) {
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

  function renderSelectedReport() {
    const report = state.reports.find((item) => item.report_date === state.selectedDate);
    ui.renderDailyReport(report, {
      titleId: "reportTitle",
      windowId: "reportWindow",
      countsId: "reportCounts",
      messagesId: "reportMessages",
      candidateMessagesId: "reportCandidateMessages",
      bodyId: "reportBody",
      emptyTitle: "No report selected",
      emptyBody: "No report available.",
      titlePrefix: "Daily Report",
    });
  }

  async function loadDashboard() {
    const [summary, reportPayload] = await Promise.all([
      ui.fetchJson("/api/dashboard"),
      ui.fetchJson("/daily-report"),
    ]);

    state.reports = reportPayload.reports || [];

    renderSummary(summary);
    syncReportSelector();
    renderSelectedReport();
  }

  if (reportSelect) {
    reportSelect.addEventListener("change", (event) => {
      state.selectedDate = event.target.value;
      renderSelectedReport();
    });
  }

  loadDashboard().catch((error) => {
    console.error(error);
    const reportBody = document.getElementById("reportBody");
    if (reportBody) {
      reportBody.innerHTML = '<div class="empty">Failed to load dashboard data.</div>';
    }
    const targetTree = document.getElementById("targetTree");
    if (targetTree) {
      targetTree.innerHTML = '<div class="empty">Failed to load coverage map.</div>';
    }
  });
});

