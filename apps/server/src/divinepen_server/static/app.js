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

  // ── 参数控件：只做"输入框做不到"的那一小部分 ──────────────────
  // 原则：每一种控件**去掉 JS 也必须能用**。这里的 JS 全是锦上添花
  // （同步显示、一键填入），不是表单能不能提交的前提。
  document.querySelectorAll("input[type=range][data-range]").forEach((el) => {
    const out = document.getElementById(el.getAttribute("data-range"));
    if (!out) return;
    const sync = () => (out.textContent = el.value);
    el.addEventListener("input", sync);
    sync();
  });
  // 取色器 ↔ 文本框：谁动同步谁。文本框才是提交的那一个 ——
  // 因为 ffmpeg 的字幕色 `&H00FFFFFF&` 取色器根本表达不了。
  document.querySelectorAll("[data-color-for]").forEach((picker) => {
    const text = document.getElementById(picker.getAttribute("data-color-for"));
    if (!text) return;
    picker.addEventListener("input", () => (text.value = picker.value.toUpperCase()));
    text.addEventListener("input", () => {
      const v = text.value.trim();
      if (/^#[0-9a-fA-F]{6}$/.test(v)) picker.value = v;
    });
  });
  // 产物库：单个参数点一下直接填；多值参数追加一行，且不重复。
  document.querySelectorAll("[data-media-add]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = document.getElementById(btn.getAttribute("data-media-add"));
      if (!target) return;
      const path = btn.getAttribute("title") || btn.textContent.trim();
      if (target.tagName === "TEXTAREA") {
        const lines = target.value
          .split("\n")
          .map((s) => s.trim())
          .filter(Boolean);
        if (!lines.includes(path)) lines.push(path);
        target.value = lines.join("\n");
      } else {
        target.value = path;
      }
      target.dispatchEvent(new Event("change", { bubbles: true }));
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

  // ── 参数行编辑器（模型表单）────────────────────────────────
  // 行号写在字段名里（p_key.3）。所以删掉中间一行只会留一个空洞，
  // 不会让别的行的值滑到别人头上 —— 和"按提交位置对齐"的做法差别很大，
  // 而勾选框不勾就不提交这个特性，会让后者悄无声息地错位。
  document.querySelectorAll("[data-rows]").forEach((table) => {
    const scope = table.closest("form") || document;
    const body = table.querySelector("[data-rows-body]");
    const tpl = scope.querySelector("template[data-row-template]");
    const addBtn = scope.querySelector("[data-add-row]");
    if (!body || !tpl) return;

    const nextIndex = () => {
      const n = Number(table.dataset.nextIndex || 0);
      table.dataset.nextIndex = String(n + 1);
      return n;
    };

    if (addBtn) {
      addBtn.addEventListener("click", () => {
        const frag = tpl.content.cloneNode(true);
        const i = nextIndex();
        frag.querySelectorAll("[name]").forEach((el) => {
          el.name = el.name.replaceAll("__i__", i);
        });
        body.appendChild(frag);
        const first = body.lastElementChild.querySelector("input,textarea,select");
        if (first) first.focus();
      });
    }

    table.addEventListener("click", (event) => {
      const btn = event.target.closest("[data-del-row]");
      if (!btn) return;
      const row = btn.closest("tr");
      if (!row) return;
      if (body.querySelectorAll("tr").length > 1) {
        row.remove();
        return;
      }
      // 最后一行不删，清空就好：表里总得留一个空行给"下一行"，
      // 否则删完最后一个参数，人反而没地方写了。
      row.querySelectorAll("input,textarea").forEach((el) => {
        if (el.type === "checkbox") el.checked = false;
        else el.value = "";
      });
      row.querySelectorAll("select").forEach((el) => {
        el.selectedIndex = 0;
      });
    });
  });

  // ── 任务详情：让"在跑"这件事自己走起来 ──────────────────────
  // 页面是服务端渲染的，一个跑着 20 分钟的视频任务，不刷新就永远停在
  // 打开那一刻的样子 —— 用户盯着一动不动的"RUNNING"，只能自己去猜。
  //
  // 所以这里只做一件事：把 #task-progress 换成服务端刚算出来的那一块。
  // **停不停由服务端说了算**：片段自己带 data-task-watch，服务端不给，
  // 我们就停。状态机不在 JS 里，JS 只负责"再问一次"。
  const watchBox = document.querySelector("[data-task-watch]");
  if (watchBox) {
    let failures = 0;
    const tick = async () => {
      // 每一轮都**重新从文档里取一次**那块节点。上一轮已经把自己换掉了，
      // 若还抓着最初那个引用，第二次 replaceWith 换的是一块已经脱离文档的
      // 节点 —— 不报错，也不生效，于是"刷第一下就永远停住"。
      const current = document.querySelector("#task-progress");
      if (!current) return;
      if (document.hidden) return; // 切到别的标签页就别再问了，回来再说
      try {
        const res = await fetch(location.pathname + "/progress", {
          headers: { "X-Requested-With": "fetch" },
          cache: "no-store",
        });
        if (!res.ok) throw new Error(String(res.status));
        const html = (await res.text()).trim();
        failures = 0;
        const holder = document.createElement("div");
        holder.innerHTML = html;
        const fresh = holder.firstElementChild;
        if (!fresh) return;
        const stillWatching = fresh.hasAttribute("data-task-watch");
        current.replaceWith(fresh);
        if (!stillWatching) {
          // 跑完了：整页刷一次，好让产物、按钮（取消 → 重跑）、暂停态都跟着更新。
          // 只刷这一次 —— 上面已经确认服务端不再要求盯着了，不会打转。
          location.reload();
        }
      } catch {
        // 网络抖一下不该把刷新链断掉：连错 3 次才提示，之后继续试。
        failures += 1;
        if (failures === 3) {
          const note = document.querySelector("[data-task-watch-note]");
          if (note) note.hidden = false;
        }
      }
    };
    setInterval(tick, 2000);
    // 从后台切回来时立刻对一次，不用等下一个 2 秒。
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) tick();
    });
  }

  // 选了异步任务就把轮询契约展开 —— 那是接下来必须填的东西。只开不关。
  document.querySelectorAll("[data-mode-select]").forEach((select) => {
    const form = select.closest("form");
    const block = form && form.querySelector("[data-poll-block]");
    if (!block) return;
    select.addEventListener("change", () => {
      if (select.value === "async_task") block.setAttribute("open", "open");
    });
  });
})();
