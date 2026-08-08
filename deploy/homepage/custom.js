/* 页面底部那一行:问一句,agent 答一句。
 *
 * 为什么是一行而不是一个聊天窗:要办的是"顺手的小事" —— 看一眼某个服务活没活、
 * 某个目录多大、某个东西在哪。真要长谈,上面就有 Claude Code,那才是长谈的地方。
 * 一行的形状本身就在说明它适合干什么。
 *
 * 后端在同一个端口的 /ask 下,所以这里是同源请求,没有 CORS 那一摊。
 */
(function () {
  "use strict";
  if (window.__askBarInstalled) return;
  window.__askBarInstalled = true;

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text) n.textContent = text;
    return n;
  }

  const bar = el("div", "askbar");
  const form = el("form", "askbar-form");
  const input = el("input", "askbar-input");
  input.type = "text";
  input.placeholder = "问一句…（回车）";
  input.autocomplete = "off";
  const out = el("div", "askbar-out");

  form.appendChild(input);
  bar.appendChild(form);
  bar.appendChild(out);

  function show(text, kind) {
    out.textContent = text;
    out.dataset.kind = kind || "";
    // 有内容才占位置,否则页面底下永远挂着一条空带子。
    out.style.display = text ? "block" : "none";
  }
  show("");

  let busy = false;
  form.addEventListener("submit", async function (e) {
    e.preventDefault();
    const q = input.value.trim();
    if (!q || busy) return;
    busy = true;
    input.disabled = true;
    show("…", "wait");
    try {
      const r = await fetch("/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: q }),
      });
      const d = await r.json().catch(function () { return {}; });
      show(d.text || d.error || "（没有回应）", d.text ? "ok" : "err");
      if (d.text) input.value = "";
    } catch (err) {
      show("连不上 /ask：" + err.message, "err");
    } finally {
      busy = false;
      input.disabled = false;
      input.focus();
    }
  });

  // Homepage 是客户端渲染的,DOM 在脚本跑的时候可能还没建好。
  function mount() {
    if (!document.body) return setTimeout(mount, 100);
    document.body.appendChild(bar);
  }
  mount();
})();
