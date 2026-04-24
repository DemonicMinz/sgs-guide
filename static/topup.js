(function () {
  "use strict";

  // ---- Configuration ----
  // Same-origin Flask blueprint (topup_payment.py). Empty string = same host.
  const API_BASE = "";

  // ---- Mock data (used when API is not configured yet) ----
  const MOCK_PACKAGES = {
    diamonds: [
      { pid: "1",  name: "11 Diamonds",    price: "0.95",  original: "1.20",  icon: "💎" },
      { pid: "2",  name: "22 Diamonds",    price: "1.80",  original: "2.30",  icon: "💎" },
      { pid: "3",  name: "56 Diamonds",    price: "3.50",  original: "4.50",  icon: "💎" },
      { pid: "4",  name: "112 Diamonds",   price: "6.80",  original: "8.50",  icon: "💎" },
      { pid: "5",  name: "172 Diamonds",   price: "9.90",  original: "12.50", icon: "💎" },
      { pid: "6",  name: "224 Diamonds",   price: "12.80", original: "16.00", icon: "💎" },
      { pid: "7",  name: "336 Diamonds",   price: "18.90", original: "24.00", icon: "💎" },
      { pid: "8",  name: "448 Diamonds",   price: "24.50", original: "31.00", icon: "💎" },
      { pid: "9",  name: "560 Diamonds",   price: "29.90", original: "38.00", icon: "💎" },
      { pid: "10", name: "1120 Diamonds",  price: "56.00", original: "72.00", icon: "💎" },
      { pid: "11", name: "2240 Diamonds",  price: "108.00",original: "140.00",icon: "💎" },
      { pid: "12", name: "4480 Diamonds",  price: "210.00",original: "270.00",icon: "💎" },
    ],
    passes: [
      { pid: "20", name: "Weekly Diamond Pass", price: "1.50", original: "2.00", icon: "🎫" },
      { pid: "21", name: "Twilight Pass",       price: "8.90", original: "12.00",icon: "🌙" },
    ]
  };

  // ---- State ----
  let state = {
    userId: "",
    serverId: "",
    playerName: "",
    verified: false,
    region: "my",
    category: "diamonds",
    selectedPkg: null,
    packages: null,
    useMock: false,  // Backend now proxies to MooGold (or mock if creds missing)
  };

  // ---- DOM refs ----
  const $ = (id) => document.getElementById(id);
  const userIdInput = $("user-id");
  const serverIdInput = $("server-id");
  const btnCheckId = $("btn-check-id");
  const idResult = $("id-result");
  const idSuccess = $("id-success");
  const idError = $("id-error");
  const playerNameEl = $("player-name");
  const regionGrid = $("region-grid");
  const pkgLoading = $("pkg-loading");
  const pkgGrid = $("pkg-grid");
  const stepCheckout = $("step-checkout");
  const stepDone = $("step-done");
  const btnPay = $("btn-pay");
  const btnBackPkg = $("btn-back-pkg");

  // ---- Region selector ----
  if (regionGrid) {
    regionGrid.addEventListener("click", function (e) {
      var btn = e.target.closest(".region-btn");
      if (!btn) return;
      regionGrid.querySelectorAll(".region-btn").forEach(function (b) {
        b.classList.remove("active");
      });
      btn.classList.add("active");
      state.region = btn.dataset.region;
      loadPackages();
    });
  }

  // ---- Category tabs ----
  var catTabs = document.querySelector(".pkg-category-tabs");
  if (catTabs) {
    catTabs.addEventListener("click", function (e) {
      var btn = e.target.closest(".filter-btn");
      if (!btn) return;
      catTabs.querySelectorAll(".filter-btn").forEach(function (b) {
        b.classList.remove("active");
      });
      btn.classList.add("active");
      state.category = btn.dataset.cat;
      renderPackages();
    });
  }

  // ---- Check ID ----
  if (btnCheckId) {
    btnCheckId.addEventListener("click", checkId);
  }

  async function checkId() {
    var uid = (userIdInput.value || "").trim();
    var sid = (serverIdInput.value || "").trim();

    if (!uid || !sid) {
      showIdError("Please enter both User ID and Server ID.");
      return;
    }

    btnCheckId.disabled = true;
    btnCheckId.textContent = "Checking…";
    idResult.hidden = true;

    try {
      if (state.useMock) {
        // Mock validation — simulate API delay
        await sleep(800);
        // Simple mock: any numeric UID > 5 chars is "valid"
        if (uid.length >= 5 && /^\d+$/.test(uid)) {
          showIdSuccess("Player_" + uid.slice(-4));
        } else {
          showIdError("Account not found. Please check your User ID and Server ID.");
        }
      } else {
        // Real API call via Flask blueprint (/api/topup/validate)
        var res = await fetch(API_BASE + "/api/topup/validate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            game: "mobile-legends",
            user_id: uid,
            server_id: sid,
          }),
        });
        var data = await res.json();
        if (data.ok && data.player_name) {
          showIdSuccess(data.player_name);
        } else {
          showIdError(data.error || "Account not found. Please check your User ID and Server ID.");
        }
      }
    } catch (err) {
      showIdError("Network error. Please try again.");
    }

    btnCheckId.disabled = false;
    btnCheckId.textContent = "Check ID";
  }

  function showIdSuccess(name) {
    state.userId = userIdInput.value.trim();
    state.serverId = serverIdInput.value.trim();
    state.playerName = name;
    state.verified = true;
    playerNameEl.textContent = name;
    idResult.hidden = false;
    idSuccess.hidden = false;
    idError.hidden = true;
  }

  function showIdError(msg) {
    state.verified = false;
    state.playerName = "";
    idResult.hidden = false;
    idSuccess.hidden = true;
    idError.hidden = false;
    var errorMsg = $("id-error-msg");
    if (errorMsg) errorMsg.textContent = msg;
  }

  // ---- Load packages ----
  async function loadPackages() {
    if (!pkgGrid || !pkgLoading) return;

    pkgGrid.innerHTML = "";
    pkgLoading.hidden = false;
    state.selectedPkg = null;
    updateCheckoutButton();

    try {
      if (state.useMock) {
        await sleep(400);
        state.packages = MOCK_PACKAGES;
      } else {
        // Fetch from Flask blueprint (/api/topup/products)
        var res = await fetch(API_BASE + "/api/topup/products?product=mobilelegends");
        var data = await res.json();
        if (data.ok && data.products) {
          // Transform API response to UI format
          var mapped = data.products.map(function (p) {
            return {
              pid: p.sku,
              name: p.name,
              price: p.price_sgd.toFixed(2),
              original: null,  // supplier doesn't expose retail; show discount only when set
              icon: (p.name || "").toLowerCase().indexOf("pass") >= 0 ? "🎫" : "💎",
            };
          });
          state.packages = {
            diamonds: mapped.filter(function (p) { return p.icon !== "🎫"; }),
            passes:   mapped.filter(function (p) { return p.icon === "🎫"; }),
          };
        }
      }
    } catch (err) {
      console.error("Failed to load packages:", err);
      state.packages = MOCK_PACKAGES; // Fallback to mock
    }

    pkgLoading.hidden = true;
    renderPackages();
  }

  function renderPackages() {
    if (!pkgGrid || !state.packages) return;

    var items = state.packages[state.category] || [];
    pkgGrid.innerHTML = "";

    items.forEach(function (pkg) {
      var card = document.createElement("div");
      card.className = "pkg-card";
      card.dataset.pid = pkg.pid;

      var discount = pkg.original
        ? Math.round((1 - parseFloat(pkg.price) / parseFloat(pkg.original)) * 100)
        : 0;

      card.innerHTML =
        (discount > 0 ? '<span class="pkg-discount">-' + discount + "%</span>" : "") +
        '<span class="pkg-icon">' + (pkg.icon || "💎") + "</span>" +
        '<span class="pkg-name">' + escapeHtml(pkg.name) + "</span>" +
        '<span class="pkg-price">$' + pkg.price + "</span>" +
        (pkg.original ? '<span class="pkg-original">$' + pkg.original + "</span>" : "");

      card.addEventListener("click", function () {
        selectPackage(pkg, card);
      });

      pkgGrid.appendChild(card);
    });
  }

  function selectPackage(pkg, cardEl) {
    // Deselect all
    pkgGrid.querySelectorAll(".pkg-card").forEach(function (c) {
      c.classList.remove("selected");
    });
    cardEl.classList.add("selected");
    state.selectedPkg = pkg;
    updateCheckout();
    updateCheckoutButton();
  }

  function updateCheckout() {
    if (!state.selectedPkg) return;
    if ($("checkout-uid")) $("checkout-uid").textContent = state.userId || "—";
    if ($("checkout-sid")) $("checkout-sid").textContent = state.serverId || "—";
    if ($("checkout-name")) $("checkout-name").textContent = state.playerName || "—";
    if ($("checkout-pkg")) $("checkout-pkg").textContent = state.selectedPkg.name;
    if ($("checkout-total")) $("checkout-total").textContent = "$" + state.selectedPkg.price;

    // Show checkout section
    if (stepCheckout) stepCheckout.hidden = false;
    // Scroll to it
    if (stepCheckout) stepCheckout.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function updateCheckoutButton() {
    if (!btnPay) return;
    btnPay.disabled = !(state.verified && state.selectedPkg);
  }

  // ---- Back to packages ----
  if (btnBackPkg) {
    btnBackPkg.addEventListener("click", function () {
      if (stepCheckout) stepCheckout.hidden = true;
      state.selectedPkg = null;
      pkgGrid.querySelectorAll(".pkg-card").forEach(function (c) {
        c.classList.remove("selected");
      });
      var pkgSection = $("step-packages");
      if (pkgSection) pkgSection.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
  }

  // ---- Pay button ----
  if (btnPay) {
    btnPay.addEventListener("click", handlePay);
  }

  async function handlePay() {
    var email = $("checkout-email");
    if (email && !email.value.trim()) {
      email.focus();
      email.reportValidity();
      return;
    }

    btnPay.disabled = true;
    btnPay.textContent = "Processing…";

    try {
      if (state.useMock) {
        // Mock order — simulate API delay
        await sleep(1500);
        showConfirmation("SGS-" + Date.now().toString(36).toUpperCase());
      } else {
        // Real checkout via Flask blueprint — creates HitPay payment, returns redirect URL
        var res = await fetch(API_BASE + "/api/topup/checkout", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            game: "mobile-legends",
            sku: state.selectedPkg.pid,
            user_id: state.userId,
            server_id: state.serverId,
            email: email ? email.value.trim() : "",
            name: state.playerName || "",
          }),
        });
        var data = await res.json();
        if (data.ok && data.checkout_url) {
          // Redirect user to HitPay hosted checkout (PayNow QR / card)
          window.location.href = data.checkout_url;
          return;  // don't reset button — we're navigating away
        } else {
          alert("Order failed: " + (data.error || "Unknown error. Please try again or contact support."));
        }
      }
    } catch (err) {
      alert("Network error. Please try again.");
    }

    btnPay.disabled = false;
    btnPay.innerHTML =
      '<svg class="trust-icon" aria-hidden="true"><use href="#icon-shield"></use></svg> Pay Now';
  }

  function showConfirmation(orderId) {
    // Hide all steps except confirmation
    document.querySelectorAll(".topup-step").forEach(function (s) {
      if (s.id !== "step-done") s.hidden = true;
    });
    document.querySelector(".topup-product-hero").hidden = true;
    document.querySelector(".breadcrumb").hidden = true;

    if ($("done-order-id")) $("done-order-id").textContent = orderId;
    if (stepDone) stepDone.hidden = false;
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  // ---- Utils ----
  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function escapeHtml(str) {
    var div = document.createElement("div");
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
  }

  // ---- Init ----
  loadPackages();
})();
