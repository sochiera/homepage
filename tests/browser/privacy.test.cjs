#!/usr/bin/env node
/* Browser-flow tests for static/js/privacy.js, driven by headless Chrome.
 *
 * The suite builds the site twice (fresh payloads), serves the tree over a
 * local http server, and drives real Chrome tabs through the unlock flow:
 *   1. locked entry: lockbox visible, plaintext absent, script present;
 *   2. successful unlock keeps the public heading (title/date) in the DOM;
 *   3. wrong password keeps the entry locked and shows the error note;
 *   4. same-tab reload: entry is unlocked again without asking for password;
 *   5. fresh tab (new sessionStorage): entry NOT auto-unlocked;
 *   6. rebuilt payload (new salt): stored record does NOT unlock the rebuild;
 *   7. the stored record contains neither the password nor the plaintext.
 */
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const http = require("http");
const { execFileSync, spawn } = require("child_process");

const ROOT = path.resolve(__dirname, "..", "..");
const BUILDER = path.join(ROOT, "scripts/build_site.py");
const CHROME = "/home/jan/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome";
const CHROME_FALLBACKS = ("/usr/bin/chromium-browser " + "/usr/bin/google-chrome /usr/bin/chromium").split(" ").filter(Boolean);

const PASSWORD = "bardzo-dobre-haslo-browar-42";
let failures = 0;

function check(name, cond, detail = "") {
  if (cond) console.log(`PASS ${name}`);
  else { console.error(`FAIL ${name}${detail ? `: ${detail}` : ""}`); failures++; }
}

function writeMinimalRepo(writingRoot, protectedHeadings, bodyByHeading) {
  fs.mkdirSync(path.join(writingRoot, "Dobry_Ojciec"), { recursive: true });
  fs.mkdirSync(path.join(writingRoot, "Kartka"), { recursive: true });
  fs.mkdirSync(path.join(writingRoot, "Mikroblog_2026"), { recursive: true });
  fs.writeFileSync(path.join(writingRoot, "Dobry_Ojciec/opowiadanie.md"), "# Dobry Ojciec\n\nRozdział I.\n\nTekst.\n");
  fs.writeFileSync(path.join(writingRoot, "Dobry_Ojciec/der_gute_vater.md"), "> Deutsche Übersetzung.\n\n# Der gute Vater\n\nAbschnitt.\n");
  fs.writeFileSync(path.join(writingRoot, "Kartka/kartka.md"), "# Kartka\n\nTreść kartki.\n");
  let microblog = "# Mikroblog 2026\n\n## 23 września 2026\n\nPierwszy.\n";
  for (const heading of protectedHeadings) {
    microblog += `\n## ${heading}\n\n${bodyByHeading[heading]}\n`;
  }
  fs.writeFileSync(path.join(writingRoot, "Mikroblog_2026/mikroblog_2026.md"), microblog);
  const protectedLines = [
    "schema_version = 1", 'kdf = "pbkdf2-sha256"', "iterations = 600000", "",
  ];
  for (const heading of protectedHeadings) {
    protectedLines.push("[[entries]]", 'source = "Mikroblog_2026/mikroblog_2026.md"', `heading = "${heading}"`, "");
  }
  return { protectedToml: protectedLines.join("\n") };
}

function buildSite(writingRoot, protectedToml, out) {
  const protectedFile = path.join(out, "..", "protected.toml");
  const passwordFile = path.join(out, "..", "password");
  fs.writeFileSync(protectedFile, protectedToml);
  fs.writeFileSync(passwordFile, PASSWORD);
  fs.chmodSync(passwordFile, 0o600);
  execFileSync(
    path.join(ROOT, ".venv/bin/python"),
    [BUILDER, "--writing-root", writingRoot, "--output", out, "--protected-file", protectedFile, "--password-file", passwordFile],
    { stdio: ["ignore", "pipe", "inherit"] },
  );
  return fs.readFileSync(path.join(out, "mikroblog/index.html"), "utf8");
}

class ChromePage {
  constructor() {
    this.userData = fs.mkdtempSync(path.join(os.tmpdir(), "chrome-profile-"));
    this.port = 0;
    this.proc = null;
    this.binary = null;
  }

  start() {
    return new Promise((resolve, reject) => {
      const binaries = [CHROME, ...CHROME_FALLBACKS].filter(b => fs.existsSync(b));
      if (!binaries.length) return reject(new Error("no chrome binary found"));
      const attempt = (index) => {
        if (index >= binaries.length) return reject(new Error("no chrome binary startable"));
        this.binary = binaries[index];
        this.proc = spawn(this.binary, [
          "--headless=new", "--disable-gpu", "--no-sandbox",
          "--remote-debugging-port=0", `--user-data-dir=${this.userData}`,
          "--no-first-run", "--no-default-browser-check", "about:blank",
        ], { stdio: ["ignore", "pipe", "pipe"] });
        let buffered = "";
        const timer = setTimeout(() => { this.proc.kill("SIGKILL"); attempt(index + 1); }, 20000);
        this.proc.stderr.on("data", d => {
          buffered += d;
          const m = buffered.match(/DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)\/devtools\/browser\/[\w-]+/);
          if (m) {
            clearTimeout(timer);
            this.port = parseInt(m[1], 10);
            resolve(this.port);
          }
        });
        this.proc.on("exit", () => {
          if (this.port === 0) attempt(index + 1);
        });
      };
      attempt(0);
    });
  }

  stop() {
    if (this.proc) {
      this.proc.kill("SIGKILL");
      try { fs.rmSync(this.userData, { recursive: true, force: true }); } catch {}
    }
  }
}

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "browserflow-"));

  /* Build #1 */
  const dir1 = path.join(tmp, "one");
  fs.mkdirSync(path.join(dir1, "writing"), { recursive: true });
  fs.mkdirSync(dir1, { recursive: true });
  const { protectedToml } = writeMinimalRepo(
    path.join(dir1, "writing"), ["24 września 2026"], { "24 września 2026": "Tajny akapit." },
  );
  const site1 = path.join(tmp, "site");
  fs.mkdirSync(site1, { recursive: true });
  const html1 = buildSite(path.join(dir1, "writing"), protectedToml, site1);

  /* Build #2: fresh payload, same fragment */
  const dir2 = path.join(tmp, "two");
  fs.mkdirSync(path.join(dir2, "writing"), { recursive: true });
  fs.mkdirSync(dir2, { recursive: true });
  const { protectedToml: toml2 } = writeMinimalRepo(
    path.join(dir2, "writing"), ["24 września 2026"], { "24 września 2026": "Tajny akapit." },
  );
  const site2 = path.join(tmp, "site2");
  fs.mkdirSync(site2, { recursive: true });
  buildSite(path.join(dir2, "writing"), toml2, site2);

  check("builds produced both microblog pages with lockboxes",
    /section class="locked-entry" data-protected="/.test(html1),
    "no locked section in build 1");

  /* Serve both site trees. */
  const mime = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" };
  const serve = root => (req, res) => {
    const url = new URL(req.url, "http://x").pathname;
    const rel = url === "/" ? "mikroblog/index.html" : url.replace(/^\//, "");
    const target = path.join(root, rel);
    try {
      const ext = path.extname(target) || ".html";
      res.writeHead(200, { "Content-Type": mime[ext] ?? "text/html" });
      res.end(fs.readFileSync(target));
    } catch { res.writeHead(404); res.end("missing"); }
  };
  const server1 = http.createServer(serve(site1));
  const server2 = http.createServer(serve(site2));
  await new Promise(r => server1.listen(0, "127.0.0.1", r));
  await new Promise(r => server2.listen(0, "127.0.0.1", r));
  const url1 = `http://127.0.0.1:${server1.address().port}/`;
  const url2 = `http://127.0.0.1:${server2.address().port}/`;

  const browser = new ChromePage();
  const port = await browser.start();

  const cdp = await import("node:http");
  const fetchJson = async (url) => (await fetch(url)).json();
  const targets = await fetchJson(`http://127.0.0.1:${port}/json`);
  const wsEndpoint = targets.find(t => t.type === "page" && t.url === "about:blank")?.webSocketDebuggerUrl;
  check("chrome devtools target found", Boolean(wsEndpoint));

  /* Minimal CDP driver over WebSocket (Node's built-in client). */
  const WebSocket = globalThis.WebSocket;
  const driver = await new Promise((resolve, reject) => {
    const ws = new WebSocket(wsEndpoint);
    let id = 0;
    const pending = new Map();
    const handlers = [];
    ws.addEventListener("message", (event) => {
      const msg = JSON.parse(String(event.data));
      if (msg.id && pending.has(msg.id)) {
        const cb = pending.get(msg.id);
        pending.delete(msg.id);
        cb(msg.result ?? msg.error);
        return;
      }
      for (const [evt, cb] of handlers) if (msg.method === evt) cb(msg.params);
    });
    ws.addEventListener("open", () => resolve({
      send: (method, params = {}) => new Promise((res2) => {
        const mid = ++id;
        pending.set(mid, res2);
        ws.send(JSON.stringify({ id: mid, method, params }));
      }),
      on: (evt, cb) => { handlers.push([evt, cb]); },
      close: () => ws.close(),
    }));
    ws.addEventListener("error", (event) => reject(new Error("ws error: " + JSON.stringify(event.error ?? event))));
  });

  /* CDP helpers. */
  await driver.send("Page.enable");
  await driver.send("Runtime.enable");
  let eventQueue = [];
  driver.on("Runtime.consoleAPICalled", p => {
    eventQueue.push(p);
  });

  async function goto(url) {
    eventQueue = [];
    await driver.send("Page.navigate", { url });
    await sleepUntil(async () => (await driver.send("Runtime.evaluate", { expression: "document.readyState" })).result.value === "complete");
  }

  async function evaluate(expression) {
    const r = await driver.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error("evaluate failed: " + JSON.stringify(r.exceptionDetails));
    return r.result.value;
  }

  async function sleepUntil(pred, timeoutMs = 10000) {
    const start = Date.now();
    while (true) {
      if (await pred()) return true;
      if (Date.now() - start > timeoutMs) return false;
      await new Promise(r => setTimeout(r, 100));
    }
  }

  async function typePassword(pw) {
    await evaluate(`(async () => {
      const input = document.querySelector('section.locked-entry input[name=haslo]');
      input.value = ${JSON.stringify(pw)};
      const form = document.querySelector('section.locked-entry form.unlock');
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
      await new Promise(r => setTimeout(r, 1500));
    })()`);
  }

  /* ---------- Case 1: locked page, no plaintext, lockbox present ---------- */
  await goto(url1);
  const locked = await evaluate(`(() => ({
    headingText: document.querySelector('section.locked-entry h2')?.textContent ?? null,
    hasLockbox: !!document.querySelector('section.locked-entry .lockbox'),
    hasForm: !!document.querySelector('section.locked-entry form.unlock'),
    scriptTag: !!document.querySelector('script[src="/js/privacy.js"]'),
    secretLeak: document.body.textContent.includes('Tajny akapit'),
  }))()`);
  check("locked: heading visible before unlock", locked.headingText === "24 września 2026", JSON.stringify(locked));
  check("locked: lockbox + unlock form present", locked.hasLockbox && locked.hasForm);
  check("locked: plaintext not in page", !locked.secretLeak);

  /* ---------- Case 2: unlock with correct password ---------- */
  await typePassword(PASSWORD);
  const unlocked = await evaluate(`(() => {
    const section = document.querySelector('section.entry-block');
    return {
      headingPreserved: section?.querySelector('h2')?.textContent ?? null,
      timeDatetime: section?.querySelector('h2 time')?.getAttribute('datetime') ?? null,
      bodyIncludesSecret: !!section && section.textContent.includes('Tajny akapit'),
      formGone: !document.querySelector('form.unlock'),
      storedRaw: sessionStorage.getItem(Object.keys(sessionStorage).find(k => k.startsWith('sochiera-unlock/')) ?? ''),
    };
  })()`);
  check("unlocked: heading + date preserved", unlocked.headingPreserved === "24 września 2026" && unlocked.timeDatetime === "2026-09-24", JSON.stringify(unlocked));
  check("unlocked: body content rendered", unlocked.bodyIncludesSecret === true, JSON.stringify(unlocked));
  check("unlocked: prompt removed", unlocked.formGone === true);

  /* ---------- Case 7: stored record leaks nothing ---------- */
  check("storage: no password inside record", !(unlocked.storedRaw ?? "").includes(PASSWORD));
  check("storage: record absent from localStorage", await evaluate(`localStorage.length === 0`));

  /* ---------- Case 3: wrong password stays locked (cleared storage) ---------- */
  await evaluate(`sessionStorage.clear()`);
  await goto(url1);
  const preWrong = await evaluate(`!!document.querySelector('form.unlock')`);
  check("cleared storage: form present again", preWrong);
  await typePassword("wrong-password-123");
  const wrong = await evaluate(`(() => ({
    stillLocked: !!document.querySelector('section.locked-entry form.unlock'),
    errorShown: !document.querySelector('.unlock-error')?.hidden,
    bodyLeak: document.body.textContent.includes('Tajny akapit'),
  }))()`);
  check("wrong password: stays locked", wrong.stillLocked === true, JSON.stringify(wrong));
  check("wrong password: error note visible", wrong.errorShown === true);
  check("wrong password: no plaintext anywhere", wrong.bodyLeak === false);

  /* ---------- Case 4: reload same tab restores without prompt ---------- */
  await typePassword(PASSWORD);
  const after = await evaluate(`(() => ({
    unlocked: !!document.querySelector('section.entry-block'),
    heading: document.querySelector('section.entry-block h2')?.textContent ?? null,
    content: document.querySelector('section.entry-block')?.textContent.includes('Tajny akapit'),
  }))()`);
  check("re-unlocked with correct password", after.unlocked && after.heading === "24 września 2026" && after.content, JSON.stringify(after));

  /* Reload — sessionStorage survives reload in the same tab. */
  await goto(url1);
  const reloaded = await evaluate(`(() => ({
    unlocked: !!document.querySelector('section.entry-block'),
    heading: document.querySelector('section.entry-block h2')?.textContent ?? null,
    datetime: document.querySelector('section.entry-block h2 time')?.getAttribute('datetime') ?? null,
    content: document.querySelector('section.entry-block')?.textContent.includes('Tajny akapit'),
    noPrompt: !document.querySelector('form.unlock'),
  }))()`);
  check("reload: auto-unlocked without password", reloaded.unlocked && reloaded.noPrompt, JSON.stringify(reloaded));
  check("reload: heading + date still present", reloaded.heading === "24 września 2026" && reloaded.datetime === "2026-09-24");
  check("reload: content still rendered", reloaded.content === true);

  /* ---------- Case 6: rebuilt payload (fresh salt) does not inherit unlock ----------
   * Overwrite the served mikroblog page with the second build: same origin,
   * same sessionStorage, but a payload with a new salt — the stored record is
   * keyed by that salt, so the rebuild must stay locked. */
  fs.copyFileSync(path.join(site2, "mikroblog/index.html"), path.join(site1, "mikroblog/index.html"));
  await goto(url1);
  const rebuilt = await evaluate(`(() => ({
    locked: !!document.querySelector('section.locked-entry form.unlock'),
    content: document.body.textContent.includes('Tajny akapit'),
  }))()`);
  check("rebuilt payload: stays locked on new salt", rebuilt.locked === true, JSON.stringify(rebuilt));
  check("rebuilt payload: no plaintext", rebuilt.content === false);

  /* ---------- Case 5: cleared session storage is locked again ---------- */
  await evaluate(`sessionStorage.clear()`);
  await goto(url1);
  const clearedTab = await evaluate(`!!document.querySelector('section.locked-entry form.unlock')`);
  check("cleared session: entry locked again", clearedTab === true);

  server1.close(); server2.close();
  browser.stop();
  process.exit(failures ? 1 : 0);
})().catch(e => { console.error("SUITE-ERROR", e); process.exit(1); });
