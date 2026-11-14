(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (value) =>
    String(value).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));

  let usageRows = [];
  let busy = false;

  function setStatus(text, kind = "") {
    const badge = $("trace-status");
    badge.textContent = text;
    badge.style.color = kind === "err" ? "#ff9a94" : kind === "ok" ? "#9be7b5" : "";
  }

  function showAlert(message, kind = "ok") {
    const box = $("trace-alert");
    box.textContent = message;
    box.className = `alert ${kind}`;
    box.classList.remove("hidden");
  }

  function hideAlert() {
    $("trace-alert").classList.add("hidden");
  }

  function normaliseToken(raw, field) {
    const token = String(raw || "").trim().toUpperCase();
    if (!token) throw new Error(`${field}不能为空`);
    if (!/^[A-Z0-9_.-]+$/.test(token)) throw new Error(`${field}只能包含字母、数字、- _ .`);
    if (token.length > 64) throw new Error(`${field}最长 64 个字符`);
    return token;
  }

  function parseLimit(raw) {
    const text = String(raw).trim();
    if (!/^\d+$/.test(text)) throw new Error("路径条数必须是 1～10 的整数");
    const limit = Number(text);
    if (limit < 1 || limit > 10) throw new Error("路径条数必须在 1～10 之间");
    return limit;
  }

  async function api(url) {
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch (_) {
      data = { error: text };
    }
    if (!response.ok) {
      const error = new Error((data && data.error) || `HTTP ${response.status}`);
      error.code = data && data.code;
      throw error;
    }
    return data;
  }

  function renderSuggestions() {
    const tops = [...new Set(usageRows.map((row) => row.top_part))];
    const leaves = [...new Set(usageRows.map((row) => row.leaf_part))];
    $("t-top-list").innerHTML = tops
      .map((part) => `<option value="${esc(part)}"></option>`)
      .join("");
    $("t-leaf-list").innerHTML = leaves
      .map((part) => `<option value="${esc(part)}"></option>`)
      .join("");

    const box = $("t-suggest");
    if (!usageRows.length) {
      box.innerHTML = '<span class="hint">当前版本没有已落库用量行</span>';
      return;
    }
    box.innerHTML = usageRows
      .slice(0, 40)
      .map(
        (row) =>
          `<button class="chip-button" data-top="${esc(row.top_part)}" data-leaf="${esc(row.leaf_part)}">` +
          `${esc(row.top_part)} → ${esc(row.leaf_part)} <b>${esc(row.qty)}</b></button>`
      )
      .join("");
    box.querySelectorAll("[data-top]").forEach((button) => {
      button.addEventListener("click", () => {
        $("t-top").value = button.dataset.top;
        $("t-leaf").value = button.dataset.leaf;
        run();
      });
    });
  }

  async function loadUsageRows() {
    try {
      const payload = await api("/api/leaf-usage");
      usageRows = payload.rows;
      $("trace-version").textContent = `v${payload.version_seq}`;
      renderSuggestions();
    } catch (error) {
      showAlert(error.message, "err");
    }
  }

  function stepText(step) {
    return `${esc(step.child)} ×${esc(step.qty)}`;
  }

  function render(data) {
    $("trace-version").textContent = `v${data.version_seq}`;
    $("trace-row-label").textContent = `${data.top_part} → ${data.leaf_part}`;
    $("trace-version-note").textContent =
      `版本 v${data.version_seq} · 发布于 ${data.published_at || "-"} · 只读该版本快照`;

    const body = $("trace-body");
    body.innerHTML = "";
    data.paths.forEach((item, index) => {
      const tr = document.createElement("tr");
      const steps = item.steps
        .map((step) => `${esc(step.parent)} → ${stepText(step)}`)
        .join("，");
      tr.innerHTML =
        `<td class="num">${index + 1}</td>` +
        `<td class="mono path-cell">${esc(item.path.join(" → "))}</td>` +
        `<td class="mono steps-cell">${steps}</td>` +
        `<td class="num">${esc(item.qty)}</td>`;
      body.appendChild(tr);
    });

    const inconsistent = $("trace-inconsistent");
    inconsistent.classList.toggle("hidden", data.consistent);

    $("trace-summary").innerHTML =
      `<span class="chip">落库 <b>${esc(data.stored_qty)}</b></span>` +
      `<span class="chip">复算 <b>${esc(data.recomputed_qty)}</b></span>` +
      `<span class="chip">已列 ${data.paths.length} 条 · <b>${esc(data.shown_qty)}</b></span>` +
      (data.other_paths
        ? `<span class="chip">其余路径 <b>${esc(data.other_qty)}</b></span>`
        : `<span class="chip">其余路径 <b>0</b></span>`) +
      (data.consistent
        ? `<span class="chip ok-chip">数据一致</span>`
        : `<span class="chip err-chip">数据不一致</span>`);

    $("t-stored").textContent = data.stored_qty;
    $("t-recomputed").textContent = data.recomputed_qty;
    $("t-shown").textContent = data.shown_qty;
    $("t-other").textContent = data.other_qty;
    $("t-shown-label").textContent = `已列出 ${data.paths.length} 条路径合计`;
    $("t-other-label").textContent = data.other_paths
      ? "其余路径贡献合计"
      : "其余路径贡献合计（无）";
    const sum = data.shown_qty + data.other_qty;
    $("t-exact").textContent = `${data.shown_qty} + ${data.other_qty} = ${sum}`;
    $("t-exact-note").textContent = data.consistent
      ? `已列路径与其余路径精确相加等于落库用量 ${data.stored_qty}（版本 v${data.version_seq}）。`
      : `已列路径与其余路径精确相加等于复算用量 ${data.recomputed_qty}，但与落库用量 ${data.stored_qty} 不符；差额 ${data.stored_qty - data.recomputed_qty} 未计入任何路径。`;
    $("trace-totals").classList.remove("hidden");
  }

  async function run() {
    if (busy) return;
    let top;
    let leaf;
    let limit;
    try {
      top = normaliseToken($("t-top").value, "装配件");
      leaf = normaliseToken($("t-leaf").value, "叶件");
      limit = parseLimit($("t-limit").value);
      if (top === leaf) throw new Error("装配件和叶件不能是同一个料号");
    } catch (error) {
      showAlert(error.message, "err");
      return;
    }
    busy = true;
    setStatus("查询中…");
    hideAlert();
    try {
      const params = new URLSearchParams({ top, leaf, limit: String(limit) });
      const data = await api(`/api/usage-trace?${params}`);
      render(data);
      setStatus(data.consistent ? "已溯源" : "数据不一致", data.consistent ? "ok" : "err");
      if (!data.consistent) {
        showAlert(
          `版本 v${data.version_seq} 中 ${top} → ${leaf} 的复算值 ${data.recomputed_qty} ` +
            `与落库值 ${data.stored_qty} 不符，请核对数据。`,
          "err"
        );
      }
    } catch (error) {
      setStatus("查询失败", "err");
      $("trace-totals").classList.add("hidden");
      $("trace-body").innerHTML = "";
      $("trace-summary").innerHTML = "";
      $("trace-row-label").textContent = "";
      if (error.code === "NotFoundError") {
        showAlert(`${error.message}（请从“可选用量行”中选择，或确认该行已落库）`, "err");
      } else {
        showAlert(error.message, "err");
      }
    } finally {
      busy = false;
    }
  }

  function clearAll() {
    $("t-top").value = "";
    $("t-leaf").value = "";
    $("t-limit").value = "10";
    $("trace-body").innerHTML = "";
    $("trace-summary").innerHTML = "";
    $("trace-row-label").textContent = "";
    $("trace-version-note").textContent = "尚未查询";
    $("trace-totals").classList.add("hidden");
    $("trace-inconsistent").classList.add("hidden");
    hideAlert();
    setStatus("就绪");
  }

  function bind() {
    $("btn-trace").addEventListener("click", run);
    $("btn-trace-clear").addEventListener("click", clearAll);
    ["t-top", "t-leaf", "t-limit"].forEach((id) => {
      $(id).addEventListener("keydown", (event) => {
        if (event.key === "Enter") run();
      });
    });
  }

  function initialFromUrl() {
    const params = new URLSearchParams(window.location.search);
    if (params.get("top")) $("t-top").value = params.get("top");
    if (params.get("leaf")) $("t-leaf").value = params.get("leaf");
    if (params.get("limit")) $("t-limit").value = params.get("limit");
    return Boolean(params.get("top") && params.get("leaf"));
  }

  bind();
  const auto = initialFromUrl();
  loadUsageRows().then(() => {
    if (auto) run();
  });
})();
