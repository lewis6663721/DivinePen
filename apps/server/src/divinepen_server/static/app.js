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
  // 两种写法都支持：表单级（整张表单只有一个动作）、按钮级（同一张表单里
  // "预演"和"后台执行"的代价不一样，不能共用一句话）。
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (ev) => {
      const msg = form.getAttribute("data-confirm");
      if (msg && !window.confirm(msg)) ev.preventDefault();
    });
  });
  document.querySelectorAll("button[data-confirm],a[data-confirm]").forEach((el) => {
    el.addEventListener("click", (ev) => {
      if (!window.confirm(el.getAttribute("data-confirm"))) ev.preventDefault();
    });
  });

  // ── 模型选择器：换供应商/模型 = 自动点「切换」────────────────
  // 参数表单是跟着模型走的（video.t2v 下 wan2.7 有 9 个参数、happyhorse 只有 8 个），
  // 所以换模型必须回服务端拿那份新表单。这里只是替用户点一下「切换」按钮 ——
  // 按钮本身带 formmethod=get，所以不带 JS 时这条路一样走得通，参数也不会丢。
  document.querySelectorAll("[data-autoswitch]").forEach((sel) => {
    sel.addEventListener("change", () => {
      const btn = sel.form && sel.form.querySelector("[data-switch]");
      if (!btn) return;
      try {
        sessionStorage.setItem("dp:scroll", String(window.scrollY));
      } catch {}
      if (sel.form.requestSubmit) sel.form.requestSubmit(btn);
      else btn.click();
    });
  });
  // 切换会整页重渲染，把滚动位置带过去，省得每次都跳回页首。
  try {
    const y = sessionStorage.getItem("dp:scroll");
    if (y !== null) {
      sessionStorage.removeItem("dp:scroll");
      window.scrollTo(0, Number(y) || 0);
    }
  } catch {}

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
  // 没记录过就保留服务端给的状态（侧栏＝当前分组默认展开）；
  // 记录过就一律听用户的，哪怕他手动收起了当前分组
  // ——那是他的选择，标题行的高亮会告诉他"你现在在这儿"。
  document.querySelectorAll("details[data-remember]").forEach((el) => {
    const key = "dp:" + el.getAttribute("data-remember");
    try {
      const stored = localStorage.getItem(key);
      if (stored === "1") el.open = true;
      else if (stored === "0") el.open = false;
    } catch {}
    el.addEventListener("toggle", () => {
      try {
        localStorage.setItem(key, el.open ? "1" : "0");
      } catch {}
    });
  });

  // ── 侧栏：钉住 + 从侧栏跳页后"先亮一下"──────────────────────
  // 侧栏默认藏起来（鼠标碰左边缘才出来）。连续翻页时每次都要重新去够左边缘太累，
  // 所以从侧栏点了链接之后，新页面让侧栏自己亮 3 秒：还想接着翻就直接点，
  // 不想翻就等它自己收回去。定时兜底，避免鼠标全程不动时它一直挂着。
  const navwrap = document.getElementById("navwrap");
  const pinbtn = document.getElementById("navpin");
  if (navwrap && pinbtn) {
    const PIN = "dp:nav-pinned";
    const PEEK = "dp:nav-peek";
    const setPinned = (on) => {
      document.body.classList.toggle("nav-pinned", on);
      pinbtn.setAttribute("aria-pressed", on ? "true" : "false");
      pinbtn.textContent = on ? "已钉住" : "钉住";
      try {
        localStorage.setItem(PIN, on ? "1" : "0");
      } catch {}
    };
    try {
      setPinned(localStorage.getItem(PIN) === "1");
    } catch {}
    pinbtn.addEventListener("click", () =>
      setPinned(!document.body.classList.contains("nav-pinned")),
    );

    navwrap.querySelectorAll("a").forEach((a) =>
      a.addEventListener("click", () => {
        try {
          sessionStorage.setItem(PEEK, "1");
        } catch {}
      }),
    );
    try {
      if (sessionStorage.getItem(PEEK) === "1") {
        sessionStorage.removeItem(PEEK);
        navwrap.classList.add("peek");
        let timer = 0;
        const done = () => {
          navwrap.classList.remove("peek");
          clearTimeout(timer);
        };
        timer = setTimeout(done, 3000);
        navwrap.addEventListener("pointerleave", done, { once: true });
      }
    } catch {}
  }
})();
