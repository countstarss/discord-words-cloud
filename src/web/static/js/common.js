(function () {
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
    if (!/^https?:\/\//i.test(candidate)) {
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
    value = value.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, label, href) => {
      const safeHref = sanitizeUrl(href);
      if (!safeHref) {
        return label;
      }
      return stashToken(
        `<a href="${safeHref}" target="_blank" rel="noreferrer noopener">${renderInlineMarkdown(label)}</a>`
      );
    });

    let html = escapeHtml(value);
    html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/__([^_]+)__/g, "<strong>$1</strong>");
    html = html.replace(/~~([^~]+)~~/g, "<del>$1</del>");
    html = html.replace(/(^|[^*])\*([^*]+)\*(?!\*)/g, "$1<em>$2</em>");
    html = html.replace(/(^|[^_])_([^_]+)_(?!_)/g, "$1<em>$2</em>");

    return tokens.reduce((output, token, index) => {
      const key = `@@MDTOKEN${index}@@`;
      return output.split(key).join(token);
    }, html);
  }

  function renderMarkdown(markdown) {
    const lines = String(markdown || "").replace(/\t/g, "    ").split(/\r?\n/);
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
      html.push(`<pre><code${languageClass}>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
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
      const line = rawLine.replace(/\s+$/, "");
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

      const headingMatch = trimmed.match(/^(#{1,3})\s+(.*)$/);
      if (headingMatch) {
        flushBlocks();
        const level = headingMatch[1].length;
        html.push(`<h${level}>${renderInlineMarkdown(headingMatch[2])}</h${level}>`);
        continue;
      }

      if (/^(---|\*\*\*|___)$/.test(trimmed)) {
        flushBlocks();
        html.push("<hr>");
        continue;
      }

      const quoteMatch = line.match(/^\s*>\s?(.*)$/);
      if (quoteMatch) {
        closeParagraph();
        closeLists();
        quoteLines.push(quoteMatch[1]);
        continue;
      }

      const listMatch = line.match(/^(\s*)([-*+]|\d+\.)\s+(.*)$/);
      if (listMatch) {
        closeParagraph();
        closeQuote();
        const indent = listMatch[1].length;
        const marker = listMatch[2];
        const type = /\d+\./.test(marker) ? "ol" : "ul";
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
    if (!value) {
      return "--";
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return value;
    }
    return new Intl.DateTimeFormat(undefined, {
      year: "numeric",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  }

  async function fetchJson(url) {
    const response = await fetch(url);
    if (!response.ok) {
      throw new Error(`Request failed: ${response.status} ${response.statusText}`);
    }
    return response.json();
  }

  function setText(target, value) {
    const element = typeof target === "string" ? document.getElementById(target) : target;
    if (!element) {
      return null;
    }
    element.textContent = value;
    return element;
  }

  function renderTargets(target, regions) {
    const root = typeof target === "string" ? document.getElementById(target) : target;
    if (!root) {
      return;
    }
    if (!regions || !regions.length) {
      root.innerHTML = '<div class="empty">No named region/channel configuration found.</div>';
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

  function renderDailyReport(report, options) {
    const opts = Object.assign(
      {
        titleId: "reportTitle",
        windowId: "reportWindow",
        countsId: "reportCounts",
        messagesId: "reportMessages",
        candidateMessagesId: "reportCandidateMessages",
        bodyId: "reportBody",
        scopeLabelId: null,
        emptyTitle: "No report selected",
        emptyBody: "No report available.",
        titlePrefix: "Daily Report",
        scopeLabel: null,
      },
      options || {}
    );

    const body = document.getElementById(opts.bodyId);
    if (!report) {
      setText(opts.titleId, opts.emptyTitle);
      setText(opts.windowId, "--");
      setText(opts.countsId, "--");
      setText(opts.messagesId, "--");
      setText(opts.candidateMessagesId, "--");
      if (opts.scopeLabelId) {
        setText(opts.scopeLabelId, opts.scopeLabel || "All channels");
      }
      if (body) {
        body.innerHTML = `<div class="empty">${escapeHtml(opts.emptyBody)}</div>`;
      }
      return;
    }

    setText(opts.titleId, `${opts.titlePrefix} · ${report.report_date}`);
    setText(opts.windowId, `${formatDateTime(report.window_start)} -> ${formatDateTime(report.window_end)}`);
    setText(opts.countsId, `${report.source_message_count} / ${report.candidate_message_count}`);
    setText(opts.messagesId, Number(report.source_message_count || 0).toLocaleString());
    setText(opts.candidateMessagesId, Number(report.candidate_message_count || 0).toLocaleString());
    if (opts.scopeLabelId) {
      setText(opts.scopeLabelId, opts.scopeLabel || "All channels");
    }
    if (body) {
      body.innerHTML = report.content_html || renderMarkdown(report.content_cn);
    }
  }

  function populateSelect(select, options, selectedValue, placeholder) {
    if (!select) {
      return;
    }
    if (!options.length) {
      select.innerHTML = `<option value="">${escapeHtml(placeholder || "No options")}</option>`;
      return;
    }
    select.innerHTML = options
      .map((option) => {
        const selected = option.value === selectedValue ? " selected" : "";
        return `<option value="${escapeHtml(option.value)}"${selected}>${escapeHtml(option.label)}</option>`;
      })
      .join("");
  }

  window.RubiiUI = {
    escapeHtml,
    fetchJson,
    formatDateTime,
    populateSelect,
    renderDailyReport,
    renderMarkdown,
    renderTargets,
    setText,
  };
})();
