/* DivinePen 控制台 · 少量原生 JS。
   原则：页面不依赖 JS 也能用（表单是普通 POST + 重渲染）；JS 只做增强。 */
(() => {
  "use strict";

  // ── flash 自动消失 ──────────────────────────────────────────
  document.querySelectorAll(".flash").forEach((el) => {
    const kill = () => el.remove();
    el.querySelector("button")?.addEventListener("click", kill);
    setTimeout(kill, 9000);
  });

  // ── 代码块复制 ─────────────────────────────────────────────
  document.querySelectorAll("[data-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const sel = btn.getAttribute("data-copy");
      const node = sel ? document.querySelector(sel) : null;
      const text = node ? node.textContent : "";
      try {
        await navigator.clipboard.writeText(text);
        const old = btn.textContent;
        btn.textContent = "已复制";
        setTimeout(() => (btn.textContent = old), 1400);
      } catch {
        btn.textContent = "复制失败";
      }
    });
  });

  // ── 计费确认：让"花钱"必须被多按一次 ────────────────────────
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (ev) => {
      const msg = form.getAttribute("data-confirm");
      if (msg && !window.confirm(msg)) ev.preventDefault();
    });
  });

  // ── 表格过滤（模型列表那种长表）────────────────────────────
  document.querySelectorAll("[data-filter]").forEach((input) => {
    const table = document.querySelector(input.getAttribute("data-filter"));
    if (!table) return;
    input.addEventListener("input", () => {
      const q = input.value.trim().toLowerCase();
      table.querySelectorAll("tbody tr").forEach((tr) => {
        const hit = !q || tr.textContent.toLowerCase().includes(q);
        tr.style.display = hit ? "" : "none";
      });
    });
  });

  // ── 行列表过滤（配置管理那种长清单）────────────────────────
  // 与表格版同源：输入框内容匹配行内任意文字。供应商上百家时靠它定位。
  document.querySelectorAll("[data-filter-rows]").forEach((input) => {
    const root = document.querySelector(input.getAttribute("data-filter-rows"));
    const sel = input.getAttribute("data-filter-item") || ":scope > *";
    if (!root) return;
    input.addEventListener("input", () => {
      const q = input.value.trim().toLowerCase();
      root.querySelectorAll(sel).forEach((el) => {
        const hit = !q || el.textContent.toLowerCase().includes(q);
        el.style.display = hit ? "" : "none";
      });
    });
  });

  // ── 目录树折叠记忆（记在 localStorage，跨页面保留）─────────
  document.querySelectorAll("details[data-remember]").forEach((el) => {
    const key = "dp:" + el.getAttribute("data-remember");
    try {
      if (localStorage.getItem(key) === "1") el.open = true;
    } catch {}
    el.addEventListener("toggle", () => {
      try {
        localStorage.setItem(key, el.open ? "1" : "0");
      } catch {}
    });
  });
})();
