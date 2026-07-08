/* ═══════════════════════════════════════════════════════════════
   Miru project page — 交互引擎
   序章 / 花瓣（昼落·夜升）/ 横向卷轴 / 一天的度盘
   ═══════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  document.documentElement.classList.remove("no-js");
  document.documentElement.classList.add("js");

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var params = new URLSearchParams(location.search);
  var shotTarget = params.get("shot"); // 调试：?shot=hero|day:0.5|memory|...
  var flatMode = params.get("flat") === "1"; // 调试：卷轴平铺，供截图/降级检查
  if (flatMode) document.documentElement.classList.add("flat");

  /* ───────────────────────── 序章 ───────────────────────── */

  var prologue = document.getElementById("prologue");
  var prologueDone = false;

  function endPrologue(instant) {
    if (prologueDone || !prologue) return;
    prologueDone = true;
    try { sessionStorage.setItem("miruPrologueSeen", "1"); } catch (e) {}
    if (instant || reduceMotion) {
      prologue.hidden = true;
      document.body.classList.remove("locked");
      return;
    }
    prologue.classList.add("leaving");
    setTimeout(function () {
      prologue.classList.add("gone");
      document.body.classList.remove("locked");
    }, 1100);
    setTimeout(function () { prologue.hidden = true; }, 2100);
  }

  function runPrologue() {
    var steps = prologue.querySelectorAll("[data-step]");
    var idx = 0;
    var timer = null;
    var started = false;
    var HOLD_MS = 1500;   // 全部出现后的驻留期：期间点击不会跳转
    var lockUntil = 0;

    function showNext() {
      if (idx >= steps.length) return;
      steps[idx].classList.add("on");
      idx++;
      if (idx >= steps.length) {
        clearInterval(timer);
        lockUntil = Date.now() + HOLD_MS;
        setTimeout(function () { endPrologue(false); }, 3400);
      }
    }

    function start() {
      if (started) return;
      started = true;
      prologue.classList.add("started");
      showNext();
      timer = setInterval(showNext, 2050);
    }

    if (reduceMotion) {
      // 静态：全部显示，一次点击关闭
      steps.forEach ? steps.forEach(function (s) { s.classList.add("on"); })
                    : Array.prototype.forEach.call(steps, function (s) { s.classList.add("on"); });
      prologue.classList.add("started");
    } else {
      setTimeout(start, 700); // 自动开始，无需等待点击
    }

    prologue.addEventListener("click", function (e) {
      if (e.target.closest(".prologue-skip")) { endPrologue(false); return; }
      if (reduceMotion) { endPrologue(true); return; }
      if (!started) { start(); return; }
      if (idx < steps.length) { clearInterval(timer); showNext(); timer = setInterval(showNext, 2050); }
      else if (Date.now() >= lockUntil) { endPrologue(false); }
      // 驻留期内的点击忽略——让歌名与歌手署名至少完整停留一段时间
    });

    document.addEventListener("keydown", function (e) {
      if (prologueDone) return;
      if (e.key === "Escape") { endPrologue(false); }
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        prologue.click();
      }
    });
  }

  var seen = false;
  try { seen = sessionStorage.getItem("miruPrologueSeen") === "1"; } catch (e) {}
  if (params.get("prologue") === "1") seen = false;
  if (shotTarget || flatMode || params.get("prologue") === "skip") seen = true;

  if (prologue && !seen) {
    prologue.hidden = false;
    document.body.classList.add("locked");
    runPrologue();
  } else if (prologue) {
    prologue.hidden = true;
    prologueDone = true;
  }

  /* ───────────────────────── 花瓣层（昼落 · 夜升） ───────────────────────── */

  var canvas = document.getElementById("petals");
  if (canvas && !reduceMotion && !flatMode) {
    var ctx = canvas.getContext("2d");
    var W = 0, H = 0, DPR = Math.min(window.devicePixelRatio || 1, 2);
    var petals = [];
    var riseMode = false;

    var DAY_COLORS = ["#F5A8BC", "#F291A9", "#FFD9E3", "#F7BFCE"];
    var NIGHT_COLORS = ["#FFB7CB", "#FFD9E3", "#F5A8BC"];

    function sizeCanvas() {
      W = window.innerWidth; H = window.innerHeight;
      canvas.width = W * DPR; canvas.height = H * DPR;
      ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    }

    function makePetal(spawnAnywhere) {
      var r = 4 + Math.random() * 6;
      return {
        x: Math.random() * W,
        y: spawnAnywhere ? Math.random() * H : (riseMode ? H + 20 : -20),
        r: r,
        rot: Math.random() * Math.PI * 2,
        vr: (Math.random() - 0.5) * 0.02,
        vx: 0.2 + Math.random() * 0.5,
        vy: 0.45 + Math.random() * 0.75,
        cy: 0, // 当前纵向速度（会向目标缓动）
        sway: Math.random() * Math.PI * 2,
        swayAmp: 0.4 + Math.random() * 0.8,
        color: 0,
        alpha: 0.35 + Math.random() * 0.4
      };
    }

    function populate() {
      var n = Math.round(Math.min(26, Math.max(12, window.innerWidth / 64)));
      petals = [];
      for (var i = 0; i < n; i++) {
        var p = makePetal(true);
        p.cy = riseMode ? -p.vy * 0.75 : p.vy;
        petals.push(p);
      }
    }

    function drawPetal(p) {
      var colors = riseMode ? NIGHT_COLORS : DAY_COLORS;
      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.rotate(p.rot);
      ctx.globalAlpha = riseMode ? Math.min(0.9, p.alpha + 0.25) : p.alpha;
      if (riseMode) {
        ctx.shadowColor = "rgba(255,183,203,.85)";
        ctx.shadowBlur = 9;
      }
      ctx.fillStyle = colors[Math.floor(p.color) % colors.length];
      ctx.beginPath();
      ctx.ellipse(0, 0, p.r, p.r * 0.62, 0, 0, Math.PI * 2);
      ctx.fill();
      // 花瓣缺口（樱花瓣的小 V 口）
      ctx.globalCompositeOperation = "destination-out";
      ctx.beginPath();
      ctx.arc(p.r * 0.9, 0, p.r * 0.34, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }

    function tick() {
      ctx.clearRect(0, 0, W, H);
      for (var i = 0; i < petals.length; i++) {
        var p = petals[i];
        var targetVy = riseMode ? -p.vy * 0.75 : p.vy;
        p.cy += (targetVy - p.cy) * 0.02;
        p.sway += 0.012;
        p.x += Math.sin(p.sway) * p.swayAmp * 0.6 + (riseMode ? 0 : p.vx * 0.35);
        p.y += p.cy;
        p.rot += p.vr;
        if (p.y > H + 30) { petals[i] = makePetal(false); petals[i].y = -20; petals[i].cy = petals[i].vy; }
        if (p.y < -30)    { petals[i] = makePetal(false); petals[i].y = H + 20; petals[i].cy = -petals[i].vy * 0.75; }
        if (p.x > W + 40) p.x = -30;
        if (p.x < -40) p.x = W + 30;
        petals[i].color = i;
        drawPetal(petals[i]);
      }
      requestAnimationFrame(tick);
    }

    sizeCanvas();
    populate();
    window.addEventListener("resize", function () { sizeCanvas(); populate(); });
    document.addEventListener("visibilitychange", function () { /* rAF 自动随标签页暂停 */ });
    requestAnimationFrame(tick);

    // 00:00 记忆章 → 花瓣逆转上升
    var memorySec = document.getElementById("memory");
    if (memorySec && "IntersectionObserver" in window) {
      new IntersectionObserver(function (entries) {
        entries.forEach(function (en) { riseMode = en.isIntersecting; });
      }, { rootMargin: "-18% 0px -18% 0px", threshold: 0 }).observe(memorySec);
    }
  } else if (canvas) {
    canvas.style.display = "none";
  }

  /* ───────────────────────── 横向卷轴「日常」 ───────────────────────── */

  var stripOuter = document.querySelector(".strip-outer");
  var stripTrack = document.querySelector(".strip-track");
  var wideQuery = window.matchMedia("(min-width: 861px)");
  var stripDistance = 0;
  var stripCurrent = 0;
  var stripTarget = 0;
  var stripProgress = 0; // 0..1，供度盘用

  function stripLayout() {
    if (!stripOuter || !stripTrack) return;
    if (!wideQuery.matches || reduceMotion) {
      stripOuter.style.height = "";
      stripTrack.style.transform = "";
      stripDistance = 0;
      return;
    }
    stripDistance = stripTrack.scrollWidth - window.innerWidth;
    stripOuter.style.height = (window.innerHeight + stripDistance) + "px";
  }

  function stripFrame() {
    if (stripDistance > 0) {
      var top = stripOuter.getBoundingClientRect().top + window.scrollY;
      var raw = (window.scrollY - top) / stripDistance;
      stripTarget = Math.max(0, Math.min(1, raw));
      stripCurrent += (stripTarget - stripCurrent) * 0.1;
      if (Math.abs(stripTarget - stripCurrent) < 0.0004) stripCurrent = stripTarget;
      stripTrack.style.transform = "translate3d(" + (-stripCurrent * stripDistance) + "px,0,0)";
      stripProgress = stripCurrent;
    }
    requestAnimationFrame(stripFrame);
  }

  if (stripOuter && stripTrack && !flatMode) {
    stripLayout();
    window.addEventListener("resize", stripLayout);
    if (!reduceMotion) requestAnimationFrame(stripFrame);
  }

  /* ───────────────────────── 导航 & 一天的度盘 ───────────────────────── */

  var nav = document.querySelector(".site-nav");
  var dialFill = document.querySelector(".dial-fill");
  var dialNow = document.querySelector(".dial-now");
  var dialMark = document.querySelector(".dial-mark");
  var navLinks = document.querySelectorAll(".nav-links a");
  var hourSections = document.querySelectorAll("[data-hour]");

  function fmtHour(h) {
    h = ((h % 24) + 24) % 24;
    var hh = Math.floor(h);
    var mm = Math.round((h - hh) * 60 / 15) * 15;
    if (mm === 60) { hh = (hh + 1) % 24; mm = 0; }
    return (hh < 10 ? "0" + hh : hh) + ":" + (mm < 10 ? "0" + mm : mm);
  }

  function placeDialMark() {
    var mem = document.getElementById("memory");
    if (!mem || !dialMark) return;
    var docH = document.documentElement.scrollHeight - window.innerHeight;
    if (docH <= 0) return;
    var memTop = mem.getBoundingClientRect().top + window.scrollY;
    var frac = (memTop + mem.offsetHeight * 0.3) / (docH + window.innerHeight);
    dialMark.style.left = Math.min(97, Math.max(2, frac * 100)) + "%";
  }

  function currentHour() {
    // 横滑段内：09:00 → 20:30 连续推进
    if (stripOuter && stripDistance > 0) {
      var r = stripOuter.getBoundingClientRect();
      if (r.top <= 0 && r.bottom >= window.innerHeight) {
        return fmtHour(9 + stripProgress * 11.5);
      }
    }
    var mid = window.innerHeight * 0.5;
    var hour = "07:00";
    for (var i = 0; i < hourSections.length; i++) {
      var rect = hourSections[i].getBoundingClientRect();
      if (rect.top <= mid) hour = hourSections[i].getAttribute("data-hour");
    }
    return hour;
  }

  var idsInNav = ["day", "character", "memory", "privacy", "start", "download"];

  function onScroll() {
    var y = window.scrollY;
    if (nav) nav.classList.toggle("scrolled", y > 40);

    var docH = document.documentElement.scrollHeight - window.innerHeight;
    var pct = docH > 0 ? Math.min(100, Math.max(0, y / docH * 100)) : 0;
    if (dialFill) dialFill.style.width = pct + "%";
    if (dialNow) {
      dialNow.style.left = Math.min(96, Math.max(3, pct)) + "%";
      dialNow.textContent = currentHour();
    }

    // 导航高亮
    var active = "";
    for (var i = 0; i < idsInNav.length; i++) {
      var el = document.getElementById(idsInNav[i]);
      if (el && el.getBoundingClientRect().top <= window.innerHeight * 0.4) active = idsInNav[i];
    }
    for (var j = 0; j < navLinks.length; j++) {
      var href = navLinks[j].getAttribute("href") || "";
      navLinks[j].classList.toggle("active", href === "#" + active);
    }
  }

  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", placeDialMark);
  placeDialMark();
  onScroll();

  /* ───────────────────────── 滚动揭示 ───────────────────────── */

  var fades = document.querySelectorAll(".fade");
  if (flatMode) {
    Array.prototype.forEach.call(fades, function (el) { el.classList.add("in"); });
  } else if ("IntersectionObserver" in window && !reduceMotion) {
    var pending = 0;
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        var el = en.target;
        el.style.transitionDelay = (pending % 5) * 70 + "ms";
        pending++;
        el.classList.add("in");
        io.unobserve(el);
        setTimeout(function () { pending = Math.max(0, pending - 1); }, 400);
      });
    }, { threshold: 0.16, rootMargin: "0px 0px -6% 0px" });
    Array.prototype.forEach.call(fades, function (el) { io.observe(el); });
  } else {
    Array.prototype.forEach.call(fades, function (el) { el.classList.add("in"); });
  }

  /* ───────────────────────── 调试：?off=px 位移截图 / ?probe=1 列出偏移 ───────────────────────── */

  var offPx = parseFloat(params.get("off") || "0");
  if (offPx > 0) {
    var navEl = document.querySelector(".site-nav");
    if (navEl) navEl.style.display = "none";
    window.addEventListener("load", function () {
      setTimeout(function () { document.body.style.marginTop = (-offPx) + "px"; }, 120);
    });
  }
  if (params.get("probe") === "1") {
    window.addEventListener("load", function () {
      setTimeout(function () {
        var ids = ["dawn", "p0", "p1", "p2", "p3", "p4", "p5", "p6", "character", "memory", "privacy", "start", "download", "faq"];
        var lines = ids.map(function (id) {
          var el = document.getElementById(id);
          return el ? id + ": " + Math.round(el.getBoundingClientRect().top + window.scrollY) : id + ": -";
        });
        var box = document.createElement("pre");
        box.style.cssText = "position:fixed;top:70px;left:10px;z-index:9999;background:#fff;color:#000;font:13px/1.6 monospace;padding:10px 14px;border:2px solid #000;";
        box.textContent = lines.join("\n") + "\ndocH: " + document.documentElement.scrollHeight;
        document.body.appendChild(box);
      }, 300);
    });
  }

  /* ───────────────────────── 调试截图定位 ?shot= ───────────────────────── */

  if (shotTarget) {
    document.documentElement.style.scrollBehavior = "auto";
    setTimeout(function () {
      var y = 0;
      var m = shotTarget.match(/^day(?::([\d.]+))?$/);
      if (m && stripOuter) {
        stripLayout();
        var frac = m[1] ? parseFloat(m[1]) : 0;
        var top = stripOuter.getBoundingClientRect().top + window.scrollY;
        y = top + stripDistance * frac;
        window.scrollTo(0, y);
        stripCurrent = stripTarget = Math.max(0, Math.min(1, frac));
        if (stripDistance > 0) stripTrack.style.transform = "translate3d(" + (-stripCurrent * stripDistance) + "px,0,0)";
      } else if (shotTarget !== "hero") {
        var el = document.getElementById(shotTarget);
        if (el) { y = el.getBoundingClientRect().top + window.scrollY + 2; window.scrollTo(0, y); }
      }
      onScroll();
      // 让 fade 立即完成，便于截图
      Array.prototype.forEach.call(document.querySelectorAll(".fade"), function (el) {
        el.style.transitionDelay = "0ms"; el.classList.add("in");
      });
    }, 350);
  }
})();
