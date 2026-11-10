/* FM1 Unbricker: the interactive bits. Plain JS, no dependencies. */
(function () {
  "use strict";

  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, reduced ? Math.min(ms, 30) : ms); }); };

  // Browser storage can be missing or blocked: every access is guarded.
  var store = {
    get: function (k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { window.localStorage.setItem(k, v); } catch (e) { /* ignore */ } }
  };

  /* ───────────── theme ───────────── */
  var root = document.documentElement;
  var saved = store.get("fm1r-theme");
  if (saved === "light" || saved === "dark") root.setAttribute("data-theme", saved);
  $("#themeBtn").addEventListener("click", function () {
    var dark = root.getAttribute("data-theme") === "dark" ||
      (!root.getAttribute("data-theme") && window.matchMedia("(prefers-color-scheme: dark)").matches);
    var next = dark ? "light" : "dark";
    root.setAttribute("data-theme", next);
    store.set("fm1r-theme", next);
  });

  /* ───────────── the little synth ───────────── */
  function synthSVG() {
    var keys = "";
    for (var i = 0; i < 10; i++) keys += '<rect class="key-w" x="' + (34 + i * 33.2) + '" y="196" width="31" height="70" rx="4"/>';
    [0, 1, 3, 4, 5, 7, 8].forEach(function (i) {
      keys += '<rect class="key-b" x="' + (34 + i * 33.2 + 21) + '" y="196" width="22" height="42" rx="3"/>';
    });
    return '<svg class="synth" viewBox="0 0 400 290" xmlns="http://www.w3.org/2000/svg">' +
      '<rect class="body" x="10" y="20" width="380" height="260" rx="26" stroke-width="2"/>' +
      '<rect class="panel" x="26" y="36" width="348" height="146" rx="16"/>' +
      '<rect class="screen-bezel" x="44" y="54" width="170" height="100" rx="10"/>' +
      '<rect class="screen" x="52" y="62" width="154" height="84" rx="5" fill="#0f2a22"/>' +
      '<path class="wave" d="M62 104c10-30 20-30 30 0s20 30 30 0 20-30 30 0 20 30 30 0"/>' +
      '<text class="scrawl" x="129" y="110" text-anchor="middle">Pr?#5 ▒░▓</text>' +
      '<circle class="knob" cx="262" cy="84" r="22" stroke-width="2"/>' +
      '<circle class="knob" cx="326" cy="84" r="22" stroke-width="2"/>' +
      '<line x1="262" y1="84" x2="262" y2="66" stroke="var(--muted)" stroke-width="3" stroke-linecap="round"/>' +
      '<line x1="326" y1="84" x2="340" y2="72" stroke="var(--muted)" stroke-width="3" stroke-linecap="round"/>' +
      '<rect x="240" y="128" width="40" height="18" rx="6" fill="var(--line)"/>' +
      '<rect x="296" y="128" width="40" height="18" rx="6" fill="var(--line)"/>' +
      '<circle class="led" cx="356" cy="140" r="5" fill="#2ecc71"/>' +
      keys + "</svg>";
  }
  function setSynth(el, state) {
    if (!el) return;
    el.setAttribute("data-state", state);
    var on = state === "on", garbled = state === "garbled", busy = state === "busy";
    $(".screen", el).setAttribute("fill", on ? "#0f2a22" : garbled ? "#2a210f" : busy ? "#10192c" : "#05070a");
    $(".wave", el).style.opacity = on ? 1 : 0;
    $(".scrawl", el).style.opacity = garbled ? 1 : 0;
    $(".led", el).setAttribute("fill", on || garbled ? "#2ecc71" : busy ? "#f0a649" : "#3a3f4a");
  }
  $$("[data-synth]").forEach(function (el) {
    el.innerHTML = synthSVG();
    setSynth(el, el.getAttribute("data-state"));
  });
  // The hero unit "comes back to life" once, to set the tone.
  var heroSynth = $('[data-synth="hero"]');
  setTimeout(function () { setSynth(heroSynth, "busy"); }, 1200);
  setTimeout(function () { setSynth(heroSynth, "on"); }, 2200);

  /* ───────────── glossary ───────────── */
  var TERMS = {
    rescue: ["Rescue mode", "A mode built into the FM-1's chip at the factory. It can't be erased, and from it the memory can be read and rewritten. On a computer it appears as a USB drive called “WL82 UBOOT1.00”."],
    rom: ["ROM", "“Read-only memory”: memory written once when the chip is made and never changeable afterwards. The rescue mode lives there."],
    starter: ["The bootloader (boot head)", "The first, tiny part of the memory. When you switch the FM-1 on, this runs first and starts the firmware. If it's damaged or replaced with one that doesn't work, the FM-1 stays dark even with perfect firmware."],
    fingerprint: ["Fingerprint", "A long code calculated from a file's contents (a “SHA-256 hash”). Change one byte of the file and the fingerprint changes completely, so it proves the file is the genuine one."],
    firmware: ["Firmware", "The program that makes the FM-1 an FM-1: the screen, the sounds, the menus. Updates replace it."]
  };
  var pop = null;
  function closePop() { if (pop) { pop.remove(); pop = null; } }
  document.addEventListener("click", function (e) {
    var t = e.target.closest(".term");
    if (!t) { closePop(); return; }
    e.preventDefault();
    var wasSame = pop && pop.dataset.for === t.dataset.term;
    closePop();
    if (wasSame) return;
    var d = TERMS[t.dataset.term];
    if (!d) return;
    pop = document.createElement("div");
    pop.className = "tip-pop";
    pop.setAttribute("role", "tooltip");
    pop.dataset.for = t.dataset.term;
    pop.innerHTML = "<b></b><span></span>";
    pop.firstChild.textContent = d[0];
    pop.lastChild.textContent = d[1];
    document.body.appendChild(pop);
    var r = t.getBoundingClientRect();
    var w = pop.offsetWidth;
    var left = Math.max(16, Math.min(window.scrollX + r.left, window.scrollX + document.documentElement.clientWidth - w - 16));
    pop.style.left = left + "px";
    pop.style.top = (window.scrollY + r.bottom + 8) + "px";
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closePop(); });
  window.addEventListener("resize", closePop);

  /* ───────────── memory map ───────────── */
  var MEM = [
    { id: "head", name: "Bootloader", grow: 7, color: "var(--m-head)", icon: "▶",
      title: "1. The bootloader (16 KB)",
      text: "The very first part. When you switch the FM-1 on, this little program runs first and starts the firmware. It's tiny (we drew it bigger so you can see it), and normal updates never touch it. Other tools can replace it, and if the new one doesn't work, the FM-1 stays dark.",
      fix: "Fixed by: the Bootloader fix." },
    { id: "fw", name: "Firmware", grow: 57, color: "var(--m-fw)", icon: "♫",
      title: "2. The firmware (572 KB)",
      text: "The program that makes your FM-1 what it is: the sounds engine, the screen, the menus. A firmware update erases and rewrites this part, piece by piece. If that's interrupted halfway, the FM-1 has half a program and can't start. This is the most common brick.",
      fix: "Fixed by: the USB fix, or the Transporter." },
    { id: "data", name: "Your stuff", grow: 34, color: "var(--m-data)", icon: "★",
      title: "3. Your stuff",
      text: "Settings, presets, and with custom firmwares (Felucca, ChoralRoot…) your songs and sounds. Updates don't erase it. That's good for your songs, but it's also why leftovers from one firmware can confuse another.",
      fix: "Kept by every fix. Always in the backup the tool makes." },
    { id: "factory", name: "Factory", grow: 8, color: "var(--m-factory)", icon: "⚙",
      title: "4. Factory data",
      text: "Things set up at the factory: the built-in presets, sounds and fonts, and this FM-1's own Bluetooth identity. Firmware installs never put it back, so if a firmware (or V16) overwrites it, presets and text can come out garbled.",
      fix: "The normal fixes never touch it. Only a backup brings it back: see “Garbled presets & sounds”." }
  ];
  var memBar = $("#memBar"), memDetail = $("#memDetail");
  MEM.forEach(function (m, i) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "mem-seg";
    b.style.flexGrow = m.grow;
    b.style.flexBasis = "0";
    b.style.background = m.color;
    b.setAttribute("aria-pressed", i === 0 ? "true" : "false");
    b.innerHTML = "<span></span>";
    b.firstChild.textContent = m.name;
    b.addEventListener("click", function () { showMem(i); });
    memBar.appendChild(b);
  });
  function showMem(i) {
    var m = MEM[i];
    $$(".mem-seg", memBar).forEach(function (b, j) { b.setAttribute("aria-pressed", j === i ? "true" : "false"); });
    memDetail.innerHTML = '<div class="swatch"></div><div><h3></h3><p></p><p><small></small></p></div>';
    var sw = $(".swatch", memDetail);
    sw.style.background = m.color;
    sw.textContent = m.icon;
    $("h3", memDetail).textContent = m.title;
    $("p", memDetail).textContent = m.text;
    $("small", memDetail).textContent = m.fix;
  }
  showMem(0);

  /* ───────────── V15 / V16 layouts ───────────── */
  var KB = 1024;
  var LAYOUT = {
    v15: [
      [0x00000, 0x04000, "Bootloader", "var(--m-head)", "The stock V15 bootloader. Starts the firmware."],
      [0x04000, 0x93000, "Firmware", "var(--m-fw)", "V15's firmware ends here, at 588 KB. This is everything the normal USB fix and the Transporter write."],
      [0x93000, 0xE9000, "Settings", "var(--m-data)", "V15's settings. Custom firmwares built on V15 also keep their songs and sounds in this stretch."],
      [0xE9000, 0xEA000, "BT", "var(--m-factory)", "This FM-1's own Bluetooth identity."],
      [0xEA000, 0xFC000, "Presets & sounds", "#b08a3e", "The factory presets, sounds and fonts. Installs never rewrite this area, so it stays as the factory left it unless a firmware overwrites it."],
      [0xFC000, 0x100000, "Other", "var(--m-empty)", "Small areas some firmwares use, and a reserved area."]
    ],
    v16: [
      [0x00000, 0x04000, "Bootloader", "var(--m-head)", "V16 brings a larger stock bootloader of its own."],
      [0x04000, 0xA9000, "Firmware", "var(--m-fw)", "V16's firmware is about 84 KB longer than V15's: it runs into what V15 uses for settings. That's why the normal fix and the Transporter refuse V16: they stop where V15's firmware ends."],
      [0xA9000, 0xBA000, "", "var(--m-empty)", "Unused by V16."],
      [0xBA000, 0xDA000, "Settings", "var(--m-data)", "V16's settings live here, in a different place than V15's."],
      [0xDA000, 0xEC000, "Sequences", "#3fa7a0", "V16's sequencer. Note how it covers V15's Bluetooth spot and the start of the factory presets."],
      [0xEC000, 0xED000, "BT", "var(--m-factory)", "V16 keeps the Bluetooth data here instead."],
      [0xED000, 0xFC000, "Presets & sounds", "#b08a3e", "V16 rewrites the factory presets in its own format once it runs. Going back to V15 afterwards leaves garbled presets and wrong pitch until a backup puts the original area back."],
      [0xFC000, 0x100000, "Other", "var(--m-empty)", "Small areas some firmwares use, and a reserved area."]
    ]
  };
  var verBar = $("#verBar"), verDetail = $("#verDetail");
  function showVer(v, idx) {
    var segs = LAYOUT[v];
    verBar.innerHTML = "";
    segs.forEach(function (g, i) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "mem-seg";
      b.style.flexGrow = Math.max(g[1] - g[0], 0x5000);
      b.style.flexBasis = "0";
      b.style.background = g[3];
      b.setAttribute("aria-pressed", i === idx ? "true" : "false");
      b.setAttribute("aria-label", (g[2] || "Unused") + ", " + Math.round((g[1] - g[0]) / KB) + " KB");
      b.innerHTML = "<span></span>";
      if (g[1] - g[0] >= 0xC000) b.firstChild.textContent = g[2];
      if (g[3] === "var(--m-empty)") b.style.color = "var(--muted)";
      b.addEventListener("click", function () { showVer(v, i); });
      verBar.appendChild(b);
    });
    var g = segs[idx];
    verDetail.innerHTML = '<div class="swatch"></div><div><h3></h3><p></p></div>';
    $(".swatch", verDetail).style.background = g[3];
    $("h3", verDetail).textContent = (v === "v15" ? "V15: " : "V16: ") + (g[2] || "Unused") + " (" + Math.round((g[1] - g[0]) / KB) + " KB)";
    $("p", verDetail).textContent = g[4];
    $$("#verPick [data-ver]").forEach(function (b) { b.setAttribute("aria-pressed", b.dataset.ver === v ? "true" : "false"); });
  }
  $$("#verPick [data-ver]").forEach(function (b) {
    b.addEventListener("click", function () { showVer(b.dataset.ver, 1); });
  });
  showVer("v15", 1);

  /* ───────────── stories / simulator ───────────── */
  var N_HEAD = 4, N_FW = 96, N_DATA = 32;
  function makeCells(el, n) {
    el.innerHTML = "";
    var out = [];
    for (var i = 0; i < n; i++) { var c = document.createElement("i"); c.className = "cell"; el.appendChild(c); out.push(c); }
    return out;
  }
  var cells = {
    head: makeCells($("#cellsHead"), N_HEAD),
    fw: makeCells($("#cellsFw"), N_FW),
    data: makeCells($("#cellsData"), N_DATA)
  };
  var simSynth = $('[data-synth="sim"]');
  var say = $("#simSay"), stepsEl = $("#simSteps"), controls = $("#simControls");
  var run = 0;          // bumps on every reset: running animations stop when it changes

  function paint(list, cls) { list.forEach(function (c) { c.className = "cell " + cls; }); }
  function dataLayout() {
    cells.data.forEach(function (c, i) { c.className = "cell " + (i < 10 ? "data" : ""); });
  }
  function setSay(html) { say.innerHTML = html; }
  function setSteps(list) {
    stepsEl.innerHTML = "";
    list.forEach(function (t) { var li = document.createElement("li"); li.textContent = t; stepsEl.appendChild(li); });
    return $$("li", stepsEl);
  }
  function button(label, primary, fn) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "btn btn-small " + (primary ? "btn-primary" : "btn-ghost");
    b.textContent = label;
    b.addEventListener("click", fn);
    controls.appendChild(b);
    return b;
  }
  function clearControls() { controls.innerHTML = ""; }

  async function writeCells(list, cls, id, delay) {
    for (var i = 0; i < list.length; i++) {
      if (id !== run) return false;
      list[i].className = "cell writing";
      await sleep(delay);
      if (id !== run) return false;
      list[i].className = "cell " + cls;
    }
    return true;
  }
  async function stepThrough(lis, i, ms, id) {
    lis[i].className = "run";
    await sleep(ms);
    if (id !== run) return false;
    lis[i].className = "done";
    return true;
  }
  async function verifyFlash(list, id) {
    for (var i = 0; i < list.length; i += 6) {
      if (id !== run) return false;
      list.slice(i, i + 6).forEach(function (c) { c.classList.add("check"); });
      await sleep(18);
    }
    await sleep(300);
    list.forEach(function (c) { c.classList.remove("check"); });
    return true;
  }

  var STORIES = {
    cut: {
      setup: function () {
        paint(cells.head, "head"); paint(cells.fw, "fw-old"); dataLayout();
        $("#fwLabel").textContent = "light = the old version, dark = the new one";
        setSynth(simSynth, "on");
        setSay("Your FM-1 works fine with its current firmware. You start an update…");
        setSteps([]);
        clearControls();
        var bat = document.createElement("span");
        bat.className = "battery";
        bat.innerHTML = '<span class="b"><span class="lvl" style="width:100%;display:block"></span></span><span class="t">battery</span>';
        button("▶ Start the update", true, function () { STORIES.cut.update(bat); });
        controls.appendChild(bat);
      },
      update: async function (bat) {
        var id = ++run;
        clearControls();
        controls.appendChild(bat);
        var lvl = $(".lvl", bat);
        var stopAt = 38 + Math.floor(Math.random() * 30);
        var cut = false;
        var cutBtn = button("⚡ Battery dies!", false, function () { cut = true; });
        controls.insertBefore(cutBtn, bat);
        setSynth(simSynth, "busy");
        setSay("The update <strong>erases and rewrites the firmware, piece by piece</strong>. Watch the battery…");
        for (var i = 0; i < N_FW; i++) {
          if (id !== run) return;
          var pct = Math.max(4, 100 - i * 1.6);
          lvl.style.width = pct + "%";
          lvl.style.background = pct < 30 ? "var(--danger)" : pct < 55 ? "var(--warn)" : "var(--ok)";
          if (cut || i === stopAt) break;
          cells.fw[i].className = "cell writing";
          await sleep(45);
          if (id !== run) return;
          cells.fw[i].className = "cell fw";
        }
        if (id !== run) return;
        // the piece being written when it stopped is half-written
        cells.fw[i].className = "cell torn";
        for (var j = i + 1; j < Math.min(i + 3, N_FW); j++) cells.fw[j].className = "cell";
        lvl.style.width = "0%";
        setSynth(simSynth, "dark");
        clearControls();
        setSay("💥 <strong>The power dropped mid-update.</strong> Part of the firmware is new, part is old, and one piece " +
          '(<span style="color:var(--m-torn)">red</span>) is half-written. The FM-1 can\'t start with half a program: <strong>black screen</strong>. ' +
          "But notice: the bootloader and your stuff are untouched, and the rescue mode in the chip is still there.");
        button("🔧 Run the fix", true, function () { STORIES.cut.fix(); });
        button("↺ Start over", false, function () { run++; STORIES.cut.setup(); });
      },
      fix: async function () {
        var id = ++run;
        clearControls();
        setSay("The rescue tool connects through the rescue mode…");
        var lis = setSteps(["Check that it's really an FM-1", "Read the whole memory twice and save a backup",
          "Check the firmware file is the genuine official one", "You type WRITE to confirm",
          "Rewrite the firmware", "Read it all back and compare every byte"]);
        if (!await stepThrough(lis, 0, 600, id)) return;
        if (!await stepThrough(lis, 1, 1100, id)) return;
        if (!await stepThrough(lis, 2, 600, id)) return;
        if (!await stepThrough(lis, 3, 700, id)) return;
        lis[4].className = "run";
        setSynth(simSynth, "busy");
        if (!await writeCells(cells.fw, "fw", id, 14)) return;
        lis[4].className = "done";
        lis[5].className = "run";
        if (!await verifyFlash(cells.fw, id)) return;
        lis[5].className = "done";
        setSynth(simSynth, "on");
        setSay("✅ <strong>Back to life.</strong> The firmware is complete again, and your stuff was never touched. That's the <a href=\"#guides\" data-goto=\"usb\">USB fix</a>.");
        button("↺ Play again", false, function () { run++; STORIES.cut.setup(); });
      }
    },

    head: {
      setup: function () {
        run++;
        paint(cells.head, "head"); paint(cells.fw, "fw"); dataLayout();
        $("#fwLabel").textContent = "the program";
        setSynth(simSynth, "on");
        setSay("Some tools can replace the <button class=\"term\" data-term=\"starter\">bootloader</button> with a different one, for extra features. Usually that works. Sometimes the new bootloader doesn't start on a particular FM-1…");
        setSteps([]);
        clearControls();
        button("▶ Install a different bootloader", true, function () { STORIES.head.replace(); });
      },
      replace: async function () {
        var id = ++run;
        clearControls();
        setSynth(simSynth, "busy");
        if (!await writeCells(cells.head, "alien", id, 260)) return;
        setSynth(simSynth, "dark");
        setSay("💥 The bootloader is now a different one (<span style=\"color:var(--m-alien)\">purple</span>) that doesn't start this FM-1. " +
          "The firmware is <strong>perfect</strong>, but nothing starts it: black screen. Rewriting only the firmware (the usual fix) won't help here.");
        button("🔧 Run the full fix", true, function () { STORIES.head.fix(); });
        button("↺ Start over", false, function () { STORIES.head.setup(); });
      },
      fix: async function () {
        var id = ++run;
        clearControls();
        var lis = setSteps(["Check that it's really an FM-1", "Read the whole memory twice and save a backup",
          "Check the bootloader it will write is the genuine stock one", "You type FULL to confirm",
          "Rewrite only what differs, the bootloader last", "Read it all back and compare every byte"]);
        if (!await stepThrough(lis, 0, 600, id)) return;
        if (!await stepThrough(lis, 1, 1100, id)) return;
        if (!await stepThrough(lis, 2, 600, id)) return;
        if (!await stepThrough(lis, 3, 700, id)) return;
        lis[4].className = "run";
        setSynth(simSynth, "busy");
        if (!await writeCells(cells.head, "head", id, 300)) return;
        lis[4].className = "done";
        lis[5].className = "run";
        if (!await verifyFlash(cells.head.concat(cells.fw), id)) return;
        lis[5].className = "done";
        setSynth(simSynth, "on");
        setSay("✅ <strong>Stock bootloader back, FM-1 starts.</strong> The firmware didn't even need rewriting. That's the <a href=\"#guides\" data-goto=\"starter\">Bootloader fix</a>.");
        button("↺ Play again", false, function () { STORIES.head.setup(); });
      }
    },

    left: {
      setup: function () {
        run++;
        paint(cells.head, "head"); paint(cells.fw, "alien"); dataLayout();
        cells.data.forEach(function (c, i) { c.className = "cell " + (i < 6 ? "data" : i < 24 ? "alien" : ""); });
        $("#fwLabel").textContent = "purple = a custom firmware";
        setSynth(simSynth, "on");
        setSay("This FM-1 runs a custom firmware (like Felucca or ChoralRoot). It keeps its songs and sounds in “your stuff” (<span style=\"color:var(--m-alien)\">purple</span> there too). Now you go back to the official firmware…");
        setSteps([]);
        clearControls();
        button("▶ Install the official firmware", true, function () { STORIES.left.back(); });
      },
      back: async function () {
        var id = ++run;
        clearControls();
        setSynth(simSynth, "busy");
        if (!await writeCells(cells.fw, "fw", id, 14)) return;
        setSynth(simSynth, "garbled");
        setSay("😵 It starts, but the presets are scrambled. The official firmware was installed perfectly, but installers only rewrite the firmware, " +
          "so the custom firmware's leftovers are still in “your stuff”, and the official one tries to read them as its own settings. " +
          "Some firmwares (and V16) also overwrite the factory presets, sounds and fonts, which no install puts back. <strong>Not a brick</strong>, just confused.");
        button("🔧 Reset the settings", true, function () { STORIES.left.fix(); });
        button("↺ Start over", false, function () { STORIES.left.setup(); });
      },
      fix: async function () {
        var id = ++run;
        clearControls();
        var lis = setSteps(["First: System → Reset Patches on the FM-1", "If that's not enough: put back a backup (yours, or a factory one)"]);
        if (!await stepThrough(lis, 0, 900, id)) return;
        lis[1].className = "run";
        for (var i = 6; i < 24; i++) {
          if (id !== run) return;
          cells.data[i].className = "cell writing";
          await sleep(40);
          cells.data[i].className = "cell";
        }
        lis[1].className = "done";
        setSynth(simSynth, "on");
        setSay("✅ <strong>Clean settings, clean presets.</strong> The leftovers are gone (and still in the backup). That's the <a href=\"#guides\" data-goto=\"settings\">Garbled presets &amp; sounds</a> guide.");
        button("↺ Play again", false, function () { STORIES.left.setup(); });
      }
    }
  };
  $$("[data-story]").forEach(function (t) {
    t.addEventListener("click", function () {
      $$("[data-story]").forEach(function (o) { o.setAttribute("aria-selected", o === t ? "true" : "false"); });
      run++;
      STORIES[t.dataset.story].setup();
    });
  });
  STORIES.cut.setup();

  /* ───────────── guides: tabs, OS, checklists, copy ───────────── */
  function openGuide(name, scroll) {
    $$("[data-guide]").forEach(function (t) { t.setAttribute("aria-selected", t.dataset.guide === name ? "true" : "false"); });
    $$(".guide").forEach(function (g) { g.classList.toggle("on", g.id === "guide-" + name); });
    if (scroll) $("#guides").scrollIntoView({ behavior: reduced ? "auto" : "smooth" });
  }
  $$("[data-guide]").forEach(function (t) { t.addEventListener("click", function () { openGuide(t.dataset.guide); }); });

  function pickOS(guide, os) {
    guide.setAttribute("data-os", os);
    $$("[data-os-pick]", guide).forEach(function (b) { b.setAttribute("aria-pressed", b.dataset.osPick === os ? "true" : "false"); });
    updateProgress(guide);
  }
  $$(".guide").forEach(function (g) {
    $$("[data-os-pick]", g).forEach(function (b) {
      b.addEventListener("click", function () { pickOS(g, b.dataset.osPick); store.set("fm1r-os-" + g.id, b.dataset.osPick); });
    });
    var os = store.get("fm1r-os-" + g.id);
    if (os && $('[data-os-pick="' + os + '"]', g)) pickOS(g, os);
  });

  function visibleSteps(guide) {
    return $$("ol.steps > li", guide).filter(function (li) { return li.offsetParent !== null || getComputedStyle(li).display !== "none"; });
  }
  function updateProgress(guide) {
    var steps = visibleSteps(guide);
    var done = steps.filter(function (li) { return li.classList.contains("done"); }).length;
    var bar = $(".progress-line div", guide);
    if (bar) bar.style.width = (steps.length ? (100 * done / steps.length) : 0) + "%";
  }
  $$(".guide").forEach(function (g) {
    $$("ol.steps > li", g).forEach(function (li, i) {
      var key = "fm1r-step-" + g.id + "-" + i;
      var label = document.createElement("label");
      label.className = "done-toggle";
      label.innerHTML = '<input type="checkbox"> Done';
      var box = label.firstChild;
      box.checked = store.get(key) === "1";
      li.classList.toggle("done", box.checked);
      box.addEventListener("change", function () {
        li.classList.toggle("done", box.checked);
        store.set(key, box.checked ? "1" : "0");
        updateProgress(g);
      });
      li.appendChild(label);
    });
    updateProgress(g);
  });

  $$(".code").forEach(function (block) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "copy";
    b.textContent = "Copy";
    b.addEventListener("click", function () {
      var text = $("pre", block).textContent;
      var done = function () { b.textContent = "Copied ✓"; b.classList.add("ok"); setTimeout(function () { b.textContent = "Copy"; b.classList.remove("ok"); }, 1600); };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text); done(); });
      } else { fallbackCopy(text); done(); }
    });
    block.appendChild(b);
  });
  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); } catch (e) { /* ignore */ }
    ta.remove();
  }

  // Links that open a guide (anywhere on the page, including ones added later).
  document.addEventListener("click", function (e) {
    var a = e.target.closest("[data-goto]");
    if (!a) return;
    e.preventDefault();
    var os = a.getAttribute("data-os-set");
    var g = $("#guide-" + a.dataset.goto);
    if (os && g && $('[data-os-pick="' + os + '"]', g)) pickOS(g, os);
    openGuide(a.dataset.goto, true);
  });

  /* ───────────── wizard ───────────── */
  var HOWCHECK =
    '<div class="os-check">' +
    '<details><summary>Windows</summary><p>Open <strong>File Explorer → This PC</strong>. A new drive may appear, or Windows may offer to format a disk (<strong>click Cancel!</strong>). ' +
    'To be sure: open <strong>Device Manager → Disk drives</strong> and look for <strong>WL82 UBOOT1.00 USB Device</strong>.</p></details>' +
    '<details><summary>Mac</summary><p>A message says the disk is <strong>not readable</strong> (click <strong>Ignore</strong>, never Initialize). Or open <strong>Disk Utility</strong>: a disk called <strong>WL82 UBOOT1.00</strong> appears in the list.</p></details>' +
    '<details><summary>Linux</summary><p>Run <code class="inline">dmesg | tail</code> right after plugging in, or <code class="inline">lsscsi -g</code>: look for <strong>WL82 UBOOT1.00</strong>.</p></details>' +
    "</div>";

  var W = {
    start: { q: "Switch your FM-1 on. What does the screen show?", opts: [
      ["⬛", "It stays black", "Nothing on the screen at all", "black"],
      ["😵", "It starts, but presets, sounds or fonts are garbled", "Usually after switching firmware, or between V15 and V16", "r_settings"],
      ["🔒", "It works, but I'm stuck on a firmware", "Can't install anything else, or saves vanish when switched off", "r_lock"],
      ["🙂", "It works fine", "I just want to understand, or prepare", "r_fine"]
    ] },
    black: { q: "Plug it into a computer with a USB data cable. Does a drive called “WL82 UBOOT1.00” appear?",
      help: "That's the chip's rescue mode. Switch the FM-1 on first, and plug it straight into the computer (no hub).",
      opts: [
        ["💽", "Yes, I see WL82 UBOOT1.00", "Or Windows asks to format a disk, or a Mac says the disk isn't readable", "cause"],
        ["🚫", "Nothing appears at all", "Tried another data cable and another USB port too", "r_transporter"],
        ["🤔", "I'm not sure how to check", "Show me how", "howcheck"]
      ] },
    howcheck: { q: "How to check for the rescue mode", help: HOWCHECK, opts: [
      ["💽", "Found it: WL82 UBOOT1.00 is there", "", "cause"],
      ["🚫", "Nothing appears at all", "Even with another cable and port", "r_transporter"]
    ] },
    cause: { q: "What happened just before it went black?", opts: [
      ["⏳", "An update stopped, failed or froze", "Or the battery died, or the cable came out", "os:update"],
      ["🔁", "I installed a different bootloader / used M-Flasher", "Or another tool that changes the start-up part", "os:head"],
      ["🤷", "I don't know", "We'll start with the most common fix", "os:update"]
    ] },
    os: { q: "Which computer can you use for the fix?", help: "Any computer works for checking; the fix itself needs one of these.", opts: [
      ["🪟", "Windows", "Windows 10 or 11", "pick:win"],
      ["🐧", "Linux or a Raspberry Pi", "", "pick:linux"],
      ["🍎", "Only a Mac", "", "pick:mac"]
    ] }
  };

  var RESULTS = {
    usb: function (os) {
      return { tag: "Your fix", title: "The USB fix", body:
        "<p>Your FM-1's firmware was most likely cut off mid-update. The USB fix puts the official V15 firmware back through the rescue mode: about 15 minutes, no extra parts.</p>" +
        '<p>If it finishes with <strong>Done</strong> but the screen is still black, the bootloader is the problem: then do the <a href="#guides" data-goto="starter" data-os-set="' + os + '">Bootloader fix</a>.</p>',
        go: "usb", os: os };
    },
    usb_mac: { tag: "Two options", title: "USB fix on a borrowed computer, or the Transporter", body:
      "<p>The usual fix (the USB fix) can't run on a Mac. You can:</p><ul>" +
      '<li><strong>Borrow a Windows PC</strong> (or use a Raspberry Pi) for 15 minutes and do the <a href="#guides" data-goto="usb" data-os-set="win">USB fix</a>. Easiest.</li>' +
      '<li>Or use the <a href="#guides" data-goto="transporter" data-os-set="mac">Transporter</a> with your Mac: two small parts (about $15), no soldering.</li></ul>' },
    head: function (os) {
      return { tag: "Your fix", title: "The Bootloader fix", body:
        "<p>The start-up part of the memory (the “bootloader”) was replaced and doesn't start your FM-1. The Bootloader fix puts the stock one back, along with the official firmware, and keeps your settings.</p>",
        go: "starter", os: os };
    },
    head_mac: { tag: "Needs a PC", title: "Bootloader fix on a borrowed computer", body:
      "<p>Putting the bootloader back needs a <strong>Windows PC or a Raspberry Pi</strong>: neither the USB tool nor the Transporter can do it from a Mac. Borrow one for 15 minutes and follow the <a href=\"#guides\" data-goto=\"starter\" data-os-set=\"win\">Bootloader fix</a>.</p>" +
      "<p>If you used M-Flasher, it runs on a Mac and has its own options to restore a backup or put the stock bootloader back; that's also worth a try.</p>" },
    r_transporter: { tag: "Your fix", title: "The Transporter route", body:
      "<p>When nothing shows up at all, the rescue mode needs a knock from the outside. A small board, the FM-1 Transporter, does exactly that. Two parts (about $15), no soldering, and it works with a Mac, Windows or Linux.</p>",
      go: "transporter" },
    r_settings: { tag: "Good news", title: "Not a brick: garbled presets or sounds", body:
      "<p>Your FM-1 works. It's reading another firmware's leftovers as its own settings, or its factory presets, sounds and fonts were overwritten. Start with the FM-1's own <strong>System → Reset Patches</strong>. If that's not enough, a backup (yours, or a factory one) puts everything back.</p>",
      go: "settings" },
    r_lock: { tag: "Your fix", title: "New memory chip & Firmware Lock", body:
      "<p>FM-1s sold from autumn 2026 have a newer memory chip that many firmwares don't recognise: saves vanish, and updates get refused. Nothing is broken. You'll get the FM-1 into its rescue mode from outside the firmware (there's a browser button for that), then put V15 back.</p>",
      go: "lock" },
    r_fine: { tag: "Nice", title: "Your FM-1 is fine", body:
      '<p>Worth knowing before your next update: charge the FM-1 fully, use a good data cable plugged straight into the computer, keep the computer awake, and don\'t touch anything until it says it\'s finished. Curious how bricks happen? Play with the <a href="#why">stories above</a>.</p>' }
  };

  var wiz = $("#wizard");
  var history = [];
  var cause = null;

  function renderNode(key) {
    var n = W[key];
    var depth = history.length;
    var html = '<div class="wiz-progress">' + [0, 1, 2, 3].map(function (i) { return '<i class="' + (i <= depth ? "on" : "") + '"></i>'; }).join("") + "</div>";
    html += '<div class="wiz-q"></div>';
    if (n.help) html += '<div class="wiz-help">' + n.help + "</div>";
    html += '<div class="wiz-opts"></div>';
    html += '<div class="wiz-nav">' + (history.length ? '<button class="btn btn-ghost btn-small" type="button" data-wback>← Back</button>' : "<span></span>") + "</div>";
    wiz.innerHTML = html;
    $(".wiz-q", wiz).textContent = n.q;
    var box = $(".wiz-opts", wiz);
    n.opts.forEach(function (o) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "wiz-opt";
      b.innerHTML = '<span class="emoji" aria-hidden="true"></span><span><strong></strong><small></small></span>';
      $(".emoji", b).textContent = o[0];
      $("strong", b).textContent = o[1];
      $("small", b).textContent = o[2];
      b.addEventListener("click", function () { choose(key, o[3]); });
      box.appendChild(b);
    });
  }

  function choose(from, to) {
    history.push(from);
    if (to.indexOf("os:") === 0) { cause = to.slice(3); renderNode("os"); return; }
    if (to.indexOf("pick:") === 0) {
      var os = to.slice(5);
      if (cause === "head") renderResult(os === "mac" ? RESULTS.head_mac : RESULTS.head(os));
      else renderResult(os === "mac" ? RESULTS.usb_mac : RESULTS.usb(os));
      return;
    }
    if (W[to]) { renderNode(to); return; }
    renderResult(RESULTS[to]);
  }

  function renderResult(r) {
    var html = '<div class="wiz-progress">' + [0, 1, 2, 3].map(function () { return '<i class="on"></i>'; }).join("") + "</div>";
    html += '<div class="wiz-result"><span class="tag"></span><h3></h3>' + r.body + "</div>";
    html += '<div class="wiz-nav"><button class="btn btn-ghost btn-small" type="button" data-wback>← Back</button>';
    if (r.go) html += '<a class="btn btn-primary" href="#guides" data-goto="' + r.go + '"' + (r.os ? ' data-os-set="' + r.os + '"' : "") + ">Open the steps →</a>";
    html += '<button class="btn btn-ghost btn-small" type="button" data-wrestart>Start over</button></div>';
    wiz.innerHTML = html;
    $(".tag", wiz).textContent = r.tag;
    $("h3", wiz).textContent = r.title;
  }

  wiz.addEventListener("click", function (e) {
    if (e.target.closest("[data-wback]")) {
      var prev = history.pop();
      if (prev) renderNode(prev);
    } else if (e.target.closest("[data-wrestart]")) {
      history = []; cause = null; renderNode("start");
    }
  });
  renderNode("start");
})();
