(() => {
  const state = {
    staged: [],
    parts: [],
    demoMode: false,
    busy: false,
  };

  const $ = (id) => document.getElementById(id);
  const esc = (value) =>
    String(value).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));

  function setStatus(text, kind = "") {
    const badge = $("badge-status");
    badge.textContent = text;
    badge.style.color = kind === "err" ? "#ff9a94" : kind === "ok" ? "#9be7b5" : "";
  }

  function showAlert(message, kind = "ok") {
    const box = $("alert");
    box.textContent = message;
    box.className = `alert ${kind}`;
    box.classList.remove("hidden");
  }

  function hideAlert() {
    $("alert").classList.add("hidden");
  }

  function showCycles(paths) {
    const box = $("cycle");
    if (!paths || !paths.length) {
      box.classList.add("hidden");
      return;
    }
    const items = paths.map((p) => `<li>${esc(p.join(" → "))}</li>`).join("");
    box.innerHTML = `<h4>发现环路，整批修改已拒绝（数据库未改动）</h4><ul>${items}</ul>`;
    box.classList.remove("hidden");
  }

  function normaliseToken(raw, field) {
    const token = raw.trim().toUpperCase();
    if (!token) throw new Error(`${field}不能为空`);
    if (!/^[A-Z0-9_.-]+$/.test(token)) throw new Error(`${field}只能包含字母、数字、- _ .`);
    if (token.length > 64) throw new Error(`${field}最长 64 个字符`);
    return token;
  }

  function normaliseQty(raw) {
    const text = String(raw).trim();
    if (!/^\d+$/.test(text) || Number(text) < 1) throw new Error("用量必须是正整数");
    const qty = Number(text);
    if (qty > 1000000000) throw new Error("用量不能超过 1000000000");
    return qty;
  }

  function stage(op) {
    try {
      const parent = normaliseToken($("f-parent").value, "父件");
      const child = normaliseToken($("f-child").value, "子件");
      if (parent === child) throw new Error("父件和子件不能是同一个料号");
      if (op === "upsert") {
        state.staged.push({ op: "upsert", parent, child, qty: normaliseQty($("f-qty").value) });
      } else {
        state.staged.push({ op: "delete", parent, child });
      }
      hideAlert();
      showCycles(null);
      renderBatch();
    } catch (error) {
      showAlert(error.message, "err");
    }
  }

  function renderBatch() {
    const body = $("batch-body");
    body.innerHTML = "";
    state.staged.forEach((item, index) => {
      const tr = document.createElement("tr");
      const qty = item.op === "upsert" ? item.qty : "—";
      const label = item.op === "upsert" ? "新增/改用量" : "删除";
      tr.innerHTML =
        `<td class="num">${index + 1}</td><td>${esc(label)}</td>` +
        `<td class="mono">${esc(item.parent)}</td><td class="mono">${esc(item.child)}</td>` +
        `<td class="num">${esc(qty)}</td>` +
        `<td><button class="ghost" data-remove="${index}">移除</button></td>`;
      body.appendChild(tr);
    });
    body.querySelectorAll("[data-remove]").forEach((button) => {
      button.addEventListener("click", () => {
        state.staged.splice(Number(button.dataset.remove), 1);
        renderBatch();
      });
    });
    $("batch-count").textContent = `批改清单 ${state.staged.length} 条`;
    $("batch-empty").classList.toggle("hidden", state.staged.length > 0);
    $("btn-commit").disabled = state.busy || state.staged.length === 0;
  }

  function renderState(data) {
    $("badge-version").textContent = `v${data.version.seq}`;
    $("badge-instance").textContent = data.instance;
    $("limit-parts").textContent = data.limits.max_parts;
    $("limit-edges").textContent = data.limits.max_edges;
    state.parts = data.parts;
    state.demoMode = data.demo_mode;
    $("demo-pause").disabled = !data.demo_mode;
    $("demo-pause-note").textContent = data.demo_mode ? "演示模式：在计算期间暂停" : "仅演示模式可用";
    document.querySelectorAll(".demo-only").forEach((element) => {
      element.classList.toggle("hidden", !data.demo_mode);
    });

    const edgesBody = $("edges-body");
    edgesBody.innerHTML = "";
    data.edges.forEach((edge) => {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td class="mono">${esc(edge.parent)}</td><td class="mono">${esc(edge.child)}</td>` +
        `<td class="num">${esc(edge.qty)}</td>` +
        `<td><button class="ghost" data-edit="${esc(edge.parent)}|${esc(edge.child)}|${esc(edge.qty)}">编辑</button> ` +
        `<button class="danger" data-del="${esc(edge.parent)}|${esc(edge.child)}">加入删除</button></td>`;
      edgesBody.appendChild(tr);
    });
    edgesBody.querySelectorAll("[data-edit]").forEach((button) => {
      button.addEventListener("click", () => {
        const [parent, child, qty] = button.dataset.edit.split("|");
        $("f-parent").value = parent;
        $("f-child").value = child;
        $("f-qty").value = qty;
      });
    });
    edgesBody.querySelectorAll("[data-del]").forEach((button) => {
      button.addEventListener("click", () => {
        const [parent, child] = button.dataset.del.split("|");
        $("f-parent").value = parent;
        $("f-child").value = child;
        stage("delete");
      });
    });

    renderUsage(data.leaf_usage);
  }

  function renderUsage(rows) {
    const body = $("usage-body");
    body.innerHTML = "";
    rows.forEach((row) => {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td class="mono">${esc(row.top_part)}</td>` +
        `<td class="mono">${esc(row.leaf_part)}</td>` +
        `<td class="num">${esc(row.qty)}</td>`;
      body.appendChild(tr);
    });
    $("summary").innerHTML =
      `<span class="chip">叶件用量行 <b>${rows.length}</b></span>`;
  }

  async function api(method, url, body, headers = {}) {
    const response = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json", ...headers },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch (_) {
      data = { error: text };
    }
    if (!response.ok) {
      const error = new Error((data && data.error) || `HTTP ${response.status}`);
      error.paths = data && data.paths;
      throw error;
    }
    return data;
  }

  async function refresh() {
    setStatus("刷新中…");
    try {
      const top = $("q-top").value.trim();
      const leaf = $("q-leaf").value.trim();
      const params = new URLSearchParams();
      if (top) params.set("top", top.toUpperCase());
      if (leaf) params.set("leaf", leaf.toUpperCase());
      const suffix = params.toString() ? `?${params}` : "";
      const [full, usage] = await Promise.all([
        api("GET", "/api/state"),
        api("GET", `/api/leaf-usage${suffix}`),
      ]);
      // The usage response and state are separate reads; label the table with
      // the version the usage rows actually came from.
      renderState({ ...full, leaf_usage: usage.rows });
      $("badge-version").textContent = `v${usage.version_seq}`;
      $("usage-version").textContent = `v${usage.version_seq}`;
      const link = $("btn-download");
      link.href = `/api/leaf-usage.csv${suffix}`;
      setStatus("就绪", "ok");
    } catch (error) {
      setStatus("读取失败", "err");
      showAlert(error.message, "err");
    }
  }

  async function commit() {
    if (!state.staged.length) return;
    state.busy = true;
    renderBatch();
    hideAlert();
    showCycles(null);
    setStatus("提交中…");
    const pause = Number($("demo-pause").value || 0);
    const headers = pause > 0 ? { "X-Demo-Pause": String(pause) } : {};
    const ops = state.staged.slice();
    try {
      const result = await api("POST", "/api/batch", { ops }, headers);
      state.staged = [];
      showAlert(
        `已提交为版本 v${result.version_seq}（叶件用量 ${result.leaf_rows} 行，` +
          `重试 ${result.retries} 次${result.reused ? "，结构与当前版本相同" : ""}）。`,
        "ok"
      );
      setStatus("已提交", "ok");
      await refresh();
    } catch (error) {
      setStatus("提交失败", "err");
      if (error.paths && error.paths.length) {
        showCycles(error.paths);
        showAlert("结构含环，整批未写入；请调整批改后重试。", "err");
      } else {
        showAlert(error.message, "err");
      }
    } finally {
      state.busy = false;
      renderBatch();
    }
  }

  async function refreshSummary() {
    try {
      const summary = await api("GET", "/api/usage-summary");
      const chips = summary.tops
        .map((item) => `<span class="chip">${esc(item.top_part)} · 叶件 <b>${item.leaf_count}</b></span>`)
        .join("");
      $("summary").innerHTML =
        `<span class="chip">版本 <b>v${summary.version_seq}</b></span>` +
        `<span class="chip">总行数 <b>${summary.row_count}</b></span>` + chips;
    } catch (error) {
      showAlert(error.message, "err");
    }
  }

  async function seedDemo() {
    if (!confirm("载入演示结构（共享子装配、叶件转装配件样例）？将作为一整批提交。")) return;
    try {
      setStatus("载入示例…");
      const result = await api("POST", "/api/demo/seed", {});
      showAlert(`演示结构已提交为版本 v${result.version_seq}。`, "ok");
      await refresh();
    } catch (error) {
      showAlert(error.message, "err");
    }
  }

  async function demoAction(label, path, pause) {
    const note = $("demo-result");
    note.textContent = `${label}…`;
    try {
      const headers = pause ? { "X-Demo-Pause": String(pause) } : {};
      const response = await fetch(path, { method: "POST", headers, body: "{}" });
      const data = await response.json();
      if (!response.ok) {
        note.textContent = `${label} 失败：${data.error}`;
        if (data.paths) showCycles(data.paths);
        else showAlert(data.error, "err");
        return;
      }
      note.textContent = `${label} 已提交 v${data.version_seq}（重试 ${data.retries} 次）`;
      showCycles(null);
      await refresh();
    } catch (error) {
      note.textContent = `${label} 失败：${error.message}`;
    }
  }

  function bind() {
    $("btn-stage-upsert").addEventListener("click", () => stage("upsert"));
    $("btn-stage-delete").addEventListener("click", () => stage("delete"));
    $("btn-clear-batch").addEventListener("click", () => {
      state.staged = [];
      renderBatch();
    });
    $("btn-commit").addEventListener("click", commit);
    $("btn-refresh").addEventListener("click", refresh);
    $("btn-refresh-summary").addEventListener("click", refreshSummary);
    $("btn-seed-demo").addEventListener("click", seedDemo);
    $("btn-promote").addEventListener("click", () =>
      demoAction("叶件转装配件", "/api/demo/promote-leaf", 0));
    $("btn-interleave-a").addEventListener("click", () =>
      demoAction("写 A 侧", "/api/demo/interleave/a", Number($("demo-pause").value || 0)));
    $("btn-interleave-b").addEventListener("click", () =>
      demoAction("写 B 侧", "/api/demo/interleave/b", 0));
    $("f-qty").addEventListener("keydown", (event) => {
      if (event.key === "Enter") stage("upsert");
    });
  }

  bind();
  renderBatch();
  refresh();
})();
