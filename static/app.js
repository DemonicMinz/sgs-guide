(function () {
  "use strict";

  // ---- Home hero search + role filter ----
  (function () {
    const searchInput = document.getElementById("hero-search");
    const filterWrap = document.getElementById("role-filter");
    const grid = document.getElementById("hero-grid");
    const emptyState = document.getElementById("empty-state");
    if (!grid) return;

    const cards = Array.from(grid.querySelectorAll(".hero-card"));
    let query = "";
    let role = "all";

    function apply() {
      let visible = 0;
      for (const card of cards) {
        const name = card.dataset.name || "";
        const cardRole = (card.dataset.role || "").toLowerCase();
        const matchesQuery = !query || name.includes(query);
        const matchesRole = role === "all" || cardRole === role;
        const show = matchesQuery && matchesRole;
        card.style.display = show ? "" : "none";
        if (show) visible++;
      }
      if (emptyState) emptyState.hidden = visible !== 0;
    }

    if (searchInput) {
      searchInput.addEventListener("input", function (e) {
        query = (e.target.value || "").trim().toLowerCase();
        apply();
      });
    }

    if (filterWrap) {
      filterWrap.addEventListener("click", function (e) {
        const btn = e.target.closest(".filter-btn");
        if (!btn) return;
        filterWrap.querySelectorAll(".filter-btn").forEach(function (b) {
          b.classList.remove("active");
        });
        btn.classList.add("active");
        role = (btn.dataset.role || "all").toLowerCase();
        apply();
      });
    }
  })();

  // ---- Hero page tabs: Overview / Skills / Builds / Counters / Synergy / Tips ----
  (function () {
    const tabs = document.querySelectorAll(".hero-tab");
    const panels = document.querySelectorAll(".tab-panel");
    if (!tabs.length || !panels.length) return;

    function setActive(name, opts) {
      opts = opts || {};
      let found = false;
      tabs.forEach(function (t) {
        const is = t.dataset.tab === name;
        t.classList.toggle("is-active", is);
        t.setAttribute("aria-selected", is ? "true" : "false");
        if (is) found = true;
      });
      if (!found) return;
      panels.forEach(function (p) {
        const is = p.dataset.panel === name;
        p.classList.toggle("is-active", is);
        if (is) {
          p.removeAttribute("hidden");
        } else {
          p.setAttribute("hidden", "");
        }
      });
      if (opts.scroll) {
        // Scroll tab-bar into view on small screens so users see the panel top.
        const bar = document.querySelector(".hero-tabs");
        if (bar) {
          const top = bar.getBoundingClientRect().top + window.scrollY - 56;
          window.scrollTo({ top: top, behavior: "smooth" });
        }
      }
    }

    // Pick initial tab from URL hash (so deep-links + shares work).
    const initial = (window.location.hash || "").replace("#", "");
    if (initial) setActive(initial, { scroll: false });

    tabs.forEach(function (tab) {
      tab.addEventListener("click", function (e) {
        e.preventDefault();
        const name = tab.dataset.tab;
        setActive(name, { scroll: true });
        // Update hash without a jump.
        history.replaceState(null, "", "#" + name);
        // Fire an analytics event if gtag is loaded.
        if (typeof window.gtag === "function") {
          window.gtag("event", "hero_tab", { tab_name: name });
        }
      });
    });

    // Respond to back/forward buttons.
    window.addEventListener("hashchange", function () {
      const name = (window.location.hash || "").replace("#", "");
      if (name) setActive(name, { scroll: false });
    });

    // In-content links that should switch tabs (e.g. Overview → Counters).
    document.querySelectorAll("[data-tab-link]").forEach(function (a) {
      a.addEventListener("click", function (e) {
        const name = a.dataset.tabLink;
        if (!name) return;
        e.preventDefault();
        setActive(name, { scroll: true });
        history.replaceState(null, "", "#" + name);
      });
    });
  })();

  // ---- Sticky mobile Telegram CTA: reveal on scroll, hide near footer band ----
  (function () {
    const sticky = document.querySelector(".sticky-cta");
    if (!sticky) return;

    const ctaBand = document.querySelector(".cta-band");
    const threshold = 420;
    let shown = false;

    function handleScroll() {
      const y = window.scrollY || window.pageYOffset;
      if (y > threshold) {
        if (!shown) {
          sticky.classList.add("show", "pulse");
          shown = true;
          setTimeout(function () { sticky.classList.remove("pulse"); }, 3600);
        }
      } else if (shown) {
        sticky.classList.remove("show");
        shown = false;
      }
    }

    let ticking = false;
    window.addEventListener("scroll", function () {
      if (!ticking) {
        window.requestAnimationFrame(function () {
          handleScroll();
          ticking = false;
        });
        ticking = true;
      }
    }, { passive: true });
    handleScroll();

    if (ctaBand && "IntersectionObserver" in window) {
      const io = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          sticky.classList.toggle("hide-near-cta", entry.isIntersecting);
        });
      }, { rootMargin: "0px 0px -15% 0px" });
      io.observe(ctaBand);
    }
  })();

  // ---- Analytics: track any CTA click via GA4 (if gtag is loaded) ----
  (function () {
    document.addEventListener("click", function (e) {
      const el = e.target.closest("[data-cta]");
      if (!el) return;
      const location = el.dataset.cta || "unknown";
      const href = el.getAttribute("href") || "";
      const isTelegram = href.indexOf("t.me/") !== -1;
      if (typeof window.gtag === "function") {
        window.gtag("event", isTelegram ? "join_telegram" : "cta_click", {
          cta_location: location,
          outbound_url: href
        });
      }
    }, { passive: true });
  })();

  // ---- Perf: pause CSS animations when the tab is hidden ----
  // Stops the compositor from repainting the breathing glow / shimmer on
  // background tabs — big win for battery + multi-tab users.
  (function () {
    function sync() {
      document.body.classList.toggle("tab-hidden", document.hidden);
    }
    document.addEventListener("visibilitychange", sync, { passive: true });
    sync();
  })();

  // ---- Perf: pause heavy CTA animations when they scroll offscreen ----
  // The Telegram buttons have several continuous keyframe animations
  // (breathe, shine, plane-idle, spark). When the element isn't visible,
  // flip `anim-paused` so the browser stops doing GPU work for it.
  (function () {
    if (!("IntersectionObserver" in window)) return;
    const watched = document.querySelectorAll(".btn-tg, .cta-trust, .sticky-cta");
    if (!watched.length) return;

    const io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        entry.target.classList.toggle("anim-paused", !entry.isIntersecting);
      });
    }, { rootMargin: "80px" });

    watched.forEach(function (el) { io.observe(el); });
  })();
})();
