# API application
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

from ..common import load_config
from ..storage import get_db, init_db

app = FastAPI(title="Rubii Words Cloud", version="0.1.0")


def _configured_regions() -> list[dict]:
    config = load_config()
    targets = config.get("targets", {})
    regions = []
    for region in targets.get("regions", []) or []:
        channels = []
        for channel in region.get("channels", []) or []:
            channels.append(
                {
                    "id": int(channel.get("id")),
                    "name": str(channel.get("name") or f"channel {channel.get('id')}").strip(),
                    "group": str(channel.get("group") or "").strip(),
                    "scope_key": f"{str(region.get('key') or '').strip() or '__all__'}:{int(channel.get('id'))}",
                    "guild_ids": [int(item) for item in channel.get("guild_ids", []) or []],
                }
            )
        regions.append(
            {
                "key": str(region.get("key") or ""),
                "name": str(region.get("name") or region.get("key") or "Region").strip(),
                "guild_ids": [int(item) for item in region.get("guild_ids", []) or []],
                "channels": channels,
            }
        )
    return regions


def _message_browser_scopes() -> list[dict]:
    return [
        {
            "scope_key": channel["scope_key"],
            "label": f"{region['name']} / {channel['name']}",
            "region_key": region["key"],
            "region_name": region["name"],
            "channel_id": channel["id"],
            "channel_name": channel["name"],
        }
        for region in _configured_regions()
        for channel in region.get("channels", [])
    ]


def _local_date_window(report_date: date, timezone_name: str = "Asia/Shanghai") -> tuple[datetime, datetime]:
    local_tz = ZoneInfo(timezone_name)
    local_start = datetime.combine(report_date, time.min, tzinfo=local_tz)
    local_end = local_start + timedelta(days=1)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def _dashboard_html() -> str:
    return """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Rubii Words Cloud Dashboard</title>
    <style>
      :root {
        --bg: #f3f6fb;
        --panel: rgba(255,255,255,0.84);
        --panel-strong: rgba(255,255,255,0.96);
        --text: #0e1726;
        --muted: #5f6f84;
        --line: rgba(15, 23, 42, 0.08);
        --accent: #0ea5e9;
        --accent-soft: rgba(14,165,233,0.12);
        --success: #10b981;
        --shadow: 0 20px 60px rgba(15, 23, 42, 0.08);
      }

      * { box-sizing: border-box; }
      html, body {
        height: 100%;
        overflow: hidden;
      }
      body {
        margin: 0;
        font-family: "IBM Plex Sans", "SF Pro Display", "Segoe UI", sans-serif;
        color: var(--text);
        background:
          radial-gradient(circle at top left, rgba(14,165,233,0.18), transparent 30%),
          radial-gradient(circle at top right, rgba(16,185,129,0.14), transparent 24%),
          linear-gradient(180deg, #f8fbff 0%, #eef3fa 100%);
      }

      .shell {
        max-width: 1380px;
        margin: 0 auto;
        height: 100vh;
        padding: 18px 22px;
        display: grid;
        grid-template-rows: auto minmax(0, 1fr);
        gap: 16px;
        overflow: hidden;
      }

      .hero {
        display: grid;
        gap: 14px;
        grid-template-columns: 1.45fr 0.95fr;
      }

      .panel {
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 24px;
        box-shadow: var(--shadow);
        backdrop-filter: blur(18px);
        min-height: 0;
      }

      .hero-card {
        padding: 20px 24px;
        position: relative;
        overflow: hidden;
      }

      .hero-card::after {
        content: "";
        position: absolute;
        inset: auto -80px -100px auto;
        width: 220px;
        height: 220px;
        background: radial-gradient(circle, rgba(14,165,233,0.18), transparent 70%);
        pointer-events: none;
      }

      .eyebrow {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 7px 12px;
        border-radius: 999px;
        background: var(--accent-soft);
        color: #0369a1;
        font-size: 12px;
        font-weight: 700;
        letter-spacing: 0.08em;
        text-transform: uppercase;
      }

      h1 {
        margin: 12px 0 8px;
        font-size: clamp(28px, 3.8vw, 46px);
        line-height: 0.95;
        letter-spacing: -0.04em;
      }

      .subtle {
        margin: 0;
        max-width: 60ch;
        color: var(--muted);
        line-height: 1.6;
        font-size: 14px;
      }

      .status-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 12px;
        padding: 16px;
      }

      .status-item {
        border: 1px solid var(--line);
        border-radius: 18px;
        padding: 14px 16px;
        background: var(--panel-strong);
      }

      .status-item span {
        display: block;
        color: var(--muted);
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        margin-bottom: 8px;
      }

      .status-item strong {
        display: block;
        font-size: 28px;
        letter-spacing: -0.04em;
      }

      .layout {
        display: grid;
        grid-template-columns: minmax(0, 0.84fr) minmax(0, 1.45fr);
        gap: 18px;
        min-height: 0;
      }

      .section {
        padding: 18px;
        display: flex;
        flex-direction: column;
        min-height: 0;
      }

      .section-title {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        margin-bottom: 12px;
      }

      .section-title h2 {
        margin: 0;
        font-size: 18px;
        letter-spacing: -0.03em;
      }

      .section-title p {
        margin: 0;
        color: var(--muted);
        font-size: 13px;
      }

      .select {
        width: 100%;
        border: 1px solid var(--line);
        border-radius: 16px;
        background: var(--panel-strong);
        padding: 12px 14px;
        font: inherit;
        color: var(--text);
        outline: none;
      }

      .mini-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 12px;
        margin-top: 14px;
      }

      .mini-card {
        padding: 14px 16px;
        border-radius: 18px;
        border: 1px solid var(--line);
        background: var(--panel-strong);
      }

      .mini-card label {
        display: block;
        color: var(--muted);
        font-size: 12px;
        margin-bottom: 6px;
      }

      .mini-card strong {
        font-size: 18px;
        letter-spacing: -0.03em;
      }

      .snapshot-note {
        margin-top: 14px;
        padding: 14px 16px;
        border-radius: 18px;
        border: 1px solid var(--line);
        background: linear-gradient(180deg, rgba(255,255,255,0.9), rgba(243,247,252,0.94));
      }

      .snapshot-note span {
        display: block;
        color: var(--muted);
        font-size: 12px;
        margin-bottom: 6px;
      }

      .snapshot-note strong {
        display: block;
        font-size: 16px;
        letter-spacing: -0.03em;
        margin-bottom: 4px;
      }

      .snapshot-note p {
        margin: 0;
        color: #1f2937;
        font-size: 13px;
        line-height: 1.6;
      }

      .target-tree {
        margin-top: 14px;
        border: 1px solid var(--line);
        border-radius: 18px;
        background: linear-gradient(180deg, rgba(255,255,255,0.94), rgba(245,249,253,0.92));
        padding: 12px;
        flex: 1;
        min-height: 0;
        overflow: auto;
      }

      .target-tree.compact {
        flex: 0 0 auto;
      }

      .target-region + .target-region {
        margin-top: 12px;
        padding-top: 12px;
        border-top: 1px solid var(--line);
      }

      .target-region-title {
        font-size: 13px;
        font-weight: 700;
        color: #0f172a;
        margin-bottom: 8px;
        letter-spacing: 0.01em;
      }

      .target-channel {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        padding: 8px 10px;
        border-radius: 12px;
        background: rgba(255,255,255,0.7);
      }

      .target-channel + .target-channel {
        margin-top: 8px;
      }

      .target-channel-name {
        font-size: 13px;
        color: #1f2937;
      }

      .target-channel-id {
        font-size: 11px;
        color: var(--muted);
        white-space: nowrap;
      }

      .report-shell {
        border: 1px solid var(--line);
        border-radius: 20px;
        background: linear-gradient(180deg, rgba(255,255,255,0.96), rgba(248,250,252,0.92));
        flex: 1;
        min-height: 0;
        overflow: hidden;
        display: flex;
        flex-direction: column;
      }

      .report-header {
        display: flex;
        justify-content: space-between;
        gap: 12px;
        padding: 18px 20px;
        border-bottom: 1px solid var(--line);
        background: rgba(14,165,233,0.05);
      }

      .report-header strong {
        display: block;
        font-size: 20px;
        letter-spacing: -0.03em;
      }

      .report-header span {
        display: block;
        color: var(--muted);
        font-size: 13px;
      }

      .report-body {
        padding: 20px 24px 26px;
        flex: 1;
        min-height: 0;
        overflow: auto;
      }

      .report-body h1,
      .report-body h2,
      .report-body h3 {
        margin-top: 1.35em;
        margin-bottom: 0.55em;
        letter-spacing: -0.03em;
      }

      .report-body h1 { font-size: 30px; }
      .report-body h2 { font-size: 22px; }
      .report-body h3 { font-size: 17px; }
      .report-body p { margin: 0 0 0.95em; line-height: 1.78; color: #1f2937; }
      .report-body ul,
      .report-body ol { margin: 0 0 1.1em 1.1em; padding: 0; color: #1f2937; }
      .report-body ul ul,
      .report-body ul ol,
      .report-body ol ul,
      .report-body ol ol {
        margin-top: 0.55em;
        margin-bottom: 0.55em;
      }
      .report-body li { margin-bottom: 0.5em; line-height: 1.75; }
      .report-body a {
        color: #0f766e;
        text-decoration: none;
      }
      .report-body a:hover { text-decoration: underline; }
      .report-body blockquote {
        margin: 0 0 1.1em;
        padding: 12px 16px;
        border-left: 3px solid rgba(14,165,233,0.35);
        border-radius: 0 14px 14px 0;
        background: rgba(14,165,233,0.05);
        color: #334155;
      }
      .report-body blockquote p:last-child { margin-bottom: 0; }
      .report-body pre {
        margin: 0 0 1.1em;
        padding: 14px 16px;
        border-radius: 16px;
        background: #0f172a;
        color: #e2e8f0;
        overflow: auto;
        line-height: 1.6;
      }
      .report-body code {
        padding: 2px 6px;
        border-radius: 8px;
        background: rgba(14,165,233,0.08);
        color: #0369a1;
      }
      .report-body pre code {
        padding: 0;
        border-radius: 0;
        background: transparent;
        color: inherit;
      }
      .report-body hr {
        border: 0;
        border-top: 1px solid var(--line);
        margin: 1.5em 0;
      }

      .empty {
        padding: 48px 24px;
        text-align: center;
        color: var(--muted);
      }

      .pulse {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        color: var(--success);
        font-weight: 700;
      }

      .pulse::before {
        content: "";
        width: 10px;
        height: 10px;
        border-radius: 50%;
        background: currentColor;
        box-shadow: 0 0 0 0 rgba(16,185,129,0.45);
        animation: pulse 1.6s infinite;
      }

      @keyframes pulse {
        0% { box-shadow: 0 0 0 0 rgba(16,185,129,0.35); }
        70% { box-shadow: 0 0 0 12px rgba(16,185,129,0); }
        100% { box-shadow: 0 0 0 0 rgba(16,185,129,0); }
      }

      @media (max-width: 1080px) {
        .hero,
        .layout {
          grid-template-columns: 1fr;
        }

        html, body {
          overflow: auto;
        }

        .shell {
          height: auto;
          min-height: 100vh;
          overflow: visible;
        }
      }

      @media (max-width: 720px) {
        .shell { padding: 16px; }
        .status-grid,
        .mini-grid { grid-template-columns: 1fr; }
      }
    </style>
  </head>
  <body>
    <div class="shell">
      <section class="hero">
        <div class="panel hero-card">
          <div class="eyebrow">Rubii Intelligence Monitor</div>
          <h1>Tech Minimal Dashboard</h1>
          <p class="subtle">
            Observe collection volume, daily report production, and switch across generated daily reports without leaving FastAPI.
          </p>
        </div>
        <div class="panel status-grid" id="statusGrid">
          <div class="status-item"><span>Collected Messages</span><strong id="totalMessages">--</strong></div>
          <div class="status-item"><span>Daily Reports</span><strong id="totalReports">--</strong></div>
          <div class="status-item"><span>24h Active Users</span><strong id="activeUsers">--</strong></div>
          <div class="status-item"><span>Latest Report</span><strong id="latestReport">--</strong></div>
        </div>
      </section>

      <section class="layout">
        <div class="panel section">
          <div class="section-title">
            <div>
              <h2>Snapshot</h2>
              <p>Operational counters and report context.</p>
            </div>
            <div class="pulse">Live</div>
          </div>

          <label for="reportDate" style="display:block;margin-bottom:8px;color:var(--muted);font-size:13px;">Switch Daily Report</label>
          <select id="reportDate" class="select"></select>

          <div class="mini-grid">
            <div class="mini-card">
              <label>Latest Message</label>
              <strong id="latestMessageAt">--</strong>
            </div>
            <div class="mini-card">
              <label>Today Hourly Reports</label>
              <strong id="hourlyReportsToday">--</strong>
            </div>
            <div class="mini-card">
              <label>Current Report Messages</label>
              <strong id="reportMessages">--</strong>
            </div>
            <div class="mini-card">
              <label>Current Report Candidates</label>
              <strong id="reportCandidateMessages">--</strong>
            </div>
          </div>

          <div class="target-tree" id="targetTree">
            <div class="empty" style="padding:24px 12px;">Loading region and channel map...</div>
          </div>
        </div>

        <div class="panel section">
          <div class="report-shell">
            <div class="report-header">
              <div>
                <strong id="reportTitle">No report selected</strong>
                <span id="reportWindow">--</span>
              </div>
              <div style="text-align:right">
                <span>Messages / Candidates</span>
                <strong id="reportCounts">--</strong>
              </div>
            </div>
            <article class="report-body" id="reportBody">
              <div class="empty">Loading dashboard data...</div>
            </article>
          </div>
        </div>
      </section>
    </div>

    <script>
      const state = { reports: [], summary: null, selectedDate: null };

      function escapeHtml(value) {
        return String(value ?? "")
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;")
          .replace(/'/g, "&#039;");
      }

      function sanitizeUrl(value) {
        const candidate = String(value || "").trim();
        if (!/^https?:\\/\\//i.test(candidate)) {
          return null;
        }
        return escapeHtml(candidate);
      }

      function renderInlineMarkdown(text) {
        const tokens = [];
        const stashToken = (html) => {
          const key = `@@MDTOKEN${tokens.length}@@`;
          tokens.push(html);
          return key;
        };

        let value = String(text || "");
        value = value.replace(/`([^`]+)`/g, (_, code) => stashToken(`<code>${escapeHtml(code)}</code>`));
        value = value.replace(/\\[([^\\]]+)\\]\\((https?:\\/\\/[^\\s)]+)\\)/g, (_, label, href) => {
          const safeHref = sanitizeUrl(href);
          if (!safeHref) {
            return label;
          }
          return stashToken(
            `<a href="${safeHref}" target="_blank" rel="noreferrer noopener">${renderInlineMarkdown(label)}</a>`
          );
        });

        let html = escapeHtml(value);
        html = html.replace(/\\*\\*([^*]+)\\*\\*/g, "<strong>$1</strong>");
        html = html.replace(/__([^_]+)__/g, "<strong>$1</strong>");
        html = html.replace(/~~([^~]+)~~/g, "<del>$1</del>");
        html = html.replace(/(^|[^*])\\*([^*]+)\\*(?!\\*)/g, "$1<em>$2</em>");
        html = html.replace(/(^|[^_])_([^_]+)_(?!_)/g, "$1<em>$2</em>");

        return tokens.reduce((output, token, index) => {
          const key = `@@MDTOKEN${index}@@`;
          return output.split(key).join(token);
        }, html);
      }

      function renderMarkdown(markdown) {
        const lines = String(markdown || "").replace(/\\t/g, "    ").split(/\\r?\\n/);
        if (!lines.some((line) => line.trim())) {
          return '<div class="empty">No report content.</div>';
        }

        const html = [];
        const listStack = [];
        let paragraphLines = [];
        let quoteLines = [];
        let codeFence = null;
        let codeLines = [];

        const closeParagraph = () => {
          if (!paragraphLines.length) {
            return;
          }
          html.push(`<p>${renderInlineMarkdown(paragraphLines.join(" "))}</p>`);
          paragraphLines = [];
        };

        const closeQuote = () => {
          if (!quoteLines.length) {
            return;
          }
          html.push(`<blockquote><p>${quoteLines.map((line) => renderInlineMarkdown(line)).join("<br>")}</p></blockquote>`);
          quoteLines = [];
        };

        const closeCodeBlock = () => {
          if (codeFence === null) {
            return;
          }
          const languageClass = /^[a-z0-9_-]+$/i.test(codeFence) ? ` class="language-${codeFence}"` : "";
          html.push(`<pre><code${languageClass}>${escapeHtml(codeLines.join("\\n"))}</code></pre>`);
          codeFence = null;
          codeLines = [];
        };

        const closeListLevel = () => {
          const current = listStack.pop();
          if (!current) {
            return;
          }
          if (current.liOpen) {
            html.push("</li>");
          }
          html.push(`</${current.type}>`);
        };

        const closeLists = () => {
          while (listStack.length) {
            closeListLevel();
          }
        };

        const prepareList = (indent, type) => {
          while (listStack.length) {
            const current = listStack[listStack.length - 1];
            if (current.indent < indent) {
              break;
            }
            if (current.indent === indent && current.type === type) {
              break;
            }
            closeListLevel();
          }

          const current = listStack[listStack.length - 1];
          if (!current || current.indent < indent) {
            html.push(`<${type}>`);
            listStack.push({ type, indent, liOpen: false });
          }
        };

        const flushBlocks = () => {
          closeParagraph();
          closeQuote();
          closeLists();
        };

        for (const rawLine of lines) {
          const line = rawLine.replace(/\\s+$/, "");
          const trimmed = line.trim();

          if (codeFence !== null) {
            if (trimmed.startsWith("```")) {
              closeCodeBlock();
            } else {
              codeLines.push(rawLine);
            }
            continue;
          }

          if (!trimmed) {
            closeParagraph();
            closeQuote();
            closeLists();
            continue;
          }

          if (trimmed.startsWith("```")) {
            closeParagraph();
            closeQuote();
            closeLists();
            codeFence = trimmed.slice(3).trim();
            codeLines = [];
            continue;
          }

          const headingMatch = trimmed.match(/^(#{1,3})\\s+(.*)$/);
          if (headingMatch) {
            flushBlocks();
            const level = headingMatch[1].length;
            html.push(`<h${level}>${renderInlineMarkdown(headingMatch[2])}</h${level}>`);
            continue;
          }

          if (/^(---|\\*\\*\\*|___)$/.test(trimmed)) {
            flushBlocks();
            html.push("<hr>");
            continue;
          }

          const quoteMatch = line.match(/^\\s*>\\s?(.*)$/);
          if (quoteMatch) {
            closeParagraph();
            closeLists();
            quoteLines.push(quoteMatch[1]);
            continue;
          }

          const listMatch = line.match(/^(\\s*)([-*+]|\\d+\\.)\\s+(.*)$/);
          if (listMatch) {
            closeParagraph();
            closeQuote();
            const indent = listMatch[1].length;
            const marker = listMatch[2];
            const type = /\\d+\\./.test(marker) ? "ol" : "ul";
            prepareList(indent, type);
            const current = listStack[listStack.length - 1];
            if (current.liOpen) {
              html.push("</li>");
            }
            html.push(`<li>${renderInlineMarkdown(listMatch[3])}`);
            current.liOpen = true;
            continue;
          }

          closeQuote();
          closeLists();
          paragraphLines.push(trimmed);
        }

        closeCodeBlock();
        closeParagraph();
        closeQuote();
        closeLists();
        return html.join("");
      }

      function formatDateTime(value) {
        if (!value) return "--";
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return value;
        return new Intl.DateTimeFormat(undefined, {
          year: "numeric",
          month: "short",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
        }).format(date);
      }

      function renderSummary(summary) {
        document.getElementById("totalMessages").textContent = summary.total_messages.toLocaleString();
        document.getElementById("totalReports").textContent = summary.total_reports.toLocaleString();
        document.getElementById("activeUsers").textContent = summary.active_users_24h.toLocaleString();
        document.getElementById("latestReport").textContent = summary.latest_report_date || "--";
        document.getElementById("latestMessageAt").textContent = formatDateTime(summary.latest_message_at);
        document.getElementById("hourlyReportsToday").textContent = summary.total_hourly_reports_today.toLocaleString();
        const configuredChannelCount = document.getElementById("configuredChannelCount");
        if (configuredChannelCount) {
          configuredChannelCount.textContent = `${summary.configured_channel_count || 0} configured channels`;
        }
        renderTargets(summary.configured_regions || []);
      }

      function renderTargets(regions) {
        const root = document.getElementById("targetTree");
        if (!root) {
          return;
        }
        if (!regions.length) {
          root.innerHTML = '<div class="empty" style="padding:24px 12px;">No named region/channel configuration found. The collector will fall back to flat guild and channel IDs.</div>';
          return;
        }

        root.innerHTML = regions
          .map((region) => `
            <section class="target-region">
              <div class="target-region-title">${escapeHtml(region.name)}</div>
              ${(region.channels || []).map((channel) => `
                <div class="target-channel">
                  <div class="target-channel-name">${escapeHtml(channel.name)}</div>
                  <div class="target-channel-id">${escapeHtml(String(channel.id))}</div>
                </div>
              `).join("")}
            </section>
          `)
          .join("");
      }

      function syncReportSelector() {
        const select = document.getElementById("reportDate");
        if (!state.reports.length) {
          select.innerHTML = '<option value="">No reports</option>';
          return;
        }
        if (!state.selectedDate) {
          state.selectedDate = state.reports[0].report_date;
        }
        select.innerHTML = state.reports
          .map((report) => `<option value="${report.report_date}" ${report.report_date === state.selectedDate ? "selected" : ""}>${report.report_date}</option>`)
          .join("");
      }

      function renderSelectedReport() {
        const report = state.reports.find((item) => item.report_date === state.selectedDate);
        const body = document.getElementById("reportBody");
        if (!report) {
          document.getElementById("reportTitle").textContent = "No report selected";
          document.getElementById("reportWindow").textContent = "--";
          document.getElementById("reportCounts").textContent = "--";
          document.getElementById("reportMessages").textContent = "--";
          document.getElementById("reportCandidateMessages").textContent = "--";
          body.innerHTML = '<div class="empty">No report available.</div>';
          return;
        }

        document.getElementById("reportTitle").textContent = `Daily Report · ${report.report_date}`;
        document.getElementById("reportWindow").textContent = `${formatDateTime(report.window_start)} → ${formatDateTime(report.window_end)}`;
        document.getElementById("reportCounts").textContent = `${report.source_message_count} / ${report.candidate_message_count}`;
        document.getElementById("reportMessages").textContent = report.source_message_count.toLocaleString();
        document.getElementById("reportCandidateMessages").textContent = report.candidate_message_count.toLocaleString();
        body.innerHTML = renderMarkdown(report.content_cn);
      }

      async function loadDashboard() {
        const [summaryRes, reportRes] = await Promise.all([
          fetch("/api/dashboard"),
          fetch("/daily-report"),
        ]);

        const summary = await summaryRes.json();
        const reportPayload = await reportRes.json();

        state.summary = summary;
        state.reports = reportPayload.reports || [];

        renderSummary(summary);
        syncReportSelector();
        renderSelectedReport();
      }

      document.getElementById("reportDate").addEventListener("change", (event) => {
        state.selectedDate = event.target.value;
        renderSelectedReport();
      });

      loadDashboard().catch((error) => {
        console.error(error);
        document.getElementById("reportBody").innerHTML = '<div class="empty">Failed to load dashboard data.</div>';
      });
    </script>
  </body>
</html>
"""


# MARK: - Health & Status APIs
@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return _dashboard_html()


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/status")
def status(hours: int = Query(default=24, ge=1, le=168)) -> dict:
    db = get_db()
    return db.get_stats(hours=hours)


# MARK: - Message APIs
@app.get("/api/messages")
def get_messages(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    scope_key: Optional[str] = Query(default=None),
) -> dict:
    db = get_db()
    messages = db.get_all_messages(
        limit=limit,
        offset=offset,
        scope_key=scope_key,
    )
    return {
        "messages": [
            {
                "message_id": m.message_id,
                "guild_id": m.guild_id,
                "channel_id": m.channel_id,
                "author_id": m.author_id,
                "region_key": m.region_key,
                "region_name": m.region_name,
                "channel_name": m.channel_name,
                "channel_group": m.channel_group,
                "scope_key": m.scope_key,
                "content": m.content,
                "detected_language": m.detected_language,
                "detected_language_confidence": m.detected_language_confidence,
                "cleaned_text": m.cleaned_text,
                "quality_score": m.quality_score,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in messages
        ],
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/messages/browser")
def get_messages_browser(
    scope_key: Optional[str] = Query(default=None),
    report_date: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    db = get_db()
    scopes = _message_browser_scopes()
    selected_scope = scope_key or (scopes[0]["scope_key"] if scopes else None)
    if not selected_scope:
        return {
            "available_scopes": [],
            "available_dates": [],
            "selected_scope": None,
            "selected_date": None,
            "messages": [],
            "pagination": {"page": 1, "page_size": page_size, "total_items": 0, "total_pages": 0},
        }

    available_dates = [item.isoformat() for item in db.get_message_dates_for_scope(scope_key=selected_scope)]
    if report_date:
        try:
            selected_date = date.fromisoformat(report_date)
        except ValueError:
            selected_date = date.fromisoformat(available_dates[0]) if available_dates else date.today()
    else:
        selected_date = date.fromisoformat(available_dates[0]) if available_dates else date.today()

    window_start, window_end = _local_date_window(selected_date)
    total_items = db.count_messages_for_window(window_start, window_end, scope_key=selected_scope)
    total_pages = max(1, (total_items + page_size - 1) // page_size) if total_items else 0
    current_page = min(page, total_pages or 1)
    offset = (current_page - 1) * page_size
    messages = db.get_messages_page_for_window(
        window_start,
        window_end,
        scope_key=selected_scope,
        limit=page_size,
        offset=offset,
    )

    return {
        "available_scopes": scopes,
        "available_dates": available_dates,
        "selected_scope": selected_scope,
        "selected_date": selected_date.isoformat(),
        "messages": [
            {
                "message_id": m.message_id,
                "guild_id": m.guild_id,
                "channel_id": m.channel_id,
                "author_id": m.author_id,
                "region_key": m.region_key,
                "region_name": m.region_name,
                "channel_name": m.channel_name,
                "channel_group": m.channel_group,
                "scope_key": m.scope_key,
                "content": m.content,
                "detected_language": m.detected_language,
                "detected_language_confidence": m.detected_language_confidence,
                "quality_score": m.quality_score,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in messages
        ],
        "pagination": {
            "page": current_page,
            "page_size": page_size,
            "total_items": total_items,
            "total_pages": total_pages,
        },
    }


@app.get("/api/messages/{message_id}")
def get_message(message_id: int) -> dict:
    db = get_db()
    message = db.get_message_by_id(message_id)
    if message is None:
        return {"error": "Message not found"}
    return {
        "message_id": message.message_id,
        "guild_id": message.guild_id,
        "channel_id": message.channel_id,
        "author_id": message.author_id,
        "region_key": message.region_key,
        "region_name": message.region_name,
        "channel_name": message.channel_name,
        "channel_group": message.channel_group,
        "scope_key": message.scope_key,
        "content": message.content,
        "detected_language": message.detected_language,
        "detected_language_confidence": message.detected_language_confidence,
        "cleaned_text": message.cleaned_text,
        "tokens": message.tokens,
        "quality_score": message.quality_score,
        "created_at": message.created_at.isoformat() if message.created_at else None,
        "updated_at": message.updated_at.isoformat() if message.updated_at else None,
    }


# MARK: - Stats APIs
@app.get("/api/stats")
def get_stats(
    hours: int = Query(default=24, ge=1, le=168),
    scope_key: Optional[str] = Query(default=None),
) -> dict:
    db = get_db()
    del scope_key
    return db.get_stats(hours=hours)


@app.get("/api/dashboard")
def get_dashboard() -> dict:
    db = get_db()
    stats = db.get_stats(hours=24)
    reports = db.get_all_daily_reports()
    latest_report = reports[0] if reports else None
    hourly_count_today = db.count_hourly_reports(date.today())
    regions = _configured_regions()
    return {
        "total_messages": db.count_messages(),
        "total_reports": len(reports),
        "total_hourly_reports_today": hourly_count_today,
        "active_users_24h": stats.get("active_users", 0),
        "detected_language_breakdown_24h": stats.get("detected_language_breakdown", []),
        "latest_message_at": stats.get("last_message_at"),
        "latest_report_date": latest_report.report_date.isoformat() if latest_report else None,
        "configured_channel_count": sum(len(region.get("channels", [])) for region in regions),
        "configured_regions": regions,
        "available_scopes": [
            {"scope_key": "global", "scope_type": "global", "label": "全部频道"}
        ] + [
            {
                "scope_key": channel["scope_key"],
                "scope_type": "channel",
                "label": f"{region['name']} / {channel['name']}",
                "region_key": region["key"],
                "region_name": region["name"],
                "channel_id": channel["id"],
                "channel_name": channel["name"],
            }
            for region in regions
            for channel in region.get("channels", [])
        ],
    }


@app.get("/daily-report")
def get_daily_reports(scope_key: str = Query(default="global")) -> dict:
    db = get_db()
    reports = db.get_all_daily_reports(scope_key=scope_key)
    return {
        "reports": [
            {
                "report_date": report.report_date.isoformat(),
                "timezone": report.timezone,
                "scope_type": report.scope_type,
                "scope_key": report.scope_key,
                "region_key": report.region_key,
                "channel_id": report.channel_id,
                "channel_name": report.channel_name,
                "window_start": report.window_start.isoformat(),
                "window_end": report.window_end.isoformat(),
                "generated_at": report.generated_at.isoformat(),
                "source_message_count": report.source_message_count,
                "candidate_message_count": report.candidate_message_count,
                "content_cn": report.content_cn,
            }
            for report in reports
        ]
    }


# MARK: - Web Runtime
def run_web(config_path: Optional[str] = None) -> None:
    config = load_config(config_path)
    
    # Initialize database
    db_cfg = config.get("database", {})
    database_url = None
    if db_cfg.get("url"):
        database_url = db_cfg["url"]
    else:
        host = db_cfg.get("host", "localhost")
        port = db_cfg.get("port", "5432")
        name = db_cfg.get("name", "rubii_words")
        user = db_cfg.get("user", "postgres")
        password = db_cfg.get("password", "")
        database_url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"
    
    init_db(database_url=database_url)

    web_cfg = config.get("web", {})
    host_addr = web_cfg.get("host", "0.0.0.0")
    port_num = int(web_cfg.get("port", 8080))

    uvicorn.run("src.api.app:app", host=host_addr, port=port_num, reload=False)


# MARK: - Main
def main() -> None:
    parser = argparse.ArgumentParser(description="Run Rubii Words Cloud API server")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    run_web(config_path=args.config)


if __name__ == "__main__":
    main()
