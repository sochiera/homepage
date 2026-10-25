#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const crypto = require("node:crypto");

const ROOT = path.resolve(__dirname, "../..");
const SITE = path.join(ROOT, ".build/site");
const EVIDENCE = process.env.GRUPA_TEMATY_EVIDENCE_DIR;
const TEST_PASSWORD = "test-only-browser-password-3388";
const TOPIC = "<img src=x onerror=alert(1)> Temat dostępności w serwisach? Jak uprościć formularze, by korzystało się z nich łatwiej.";
const ASSIGNEE = "Osoba testowa — przegląd";
const COOKIE_PATH = "/api/grupa-tematy/";
const MAX_WAIT_MS = 12000;

function check(condition, name) {
  if (!condition) throw new Error(`browser assertion failed: ${name}`);
  return name;
}

function waitFor(predicate, timeout = MAX_WAIT_MS) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const poll = async () => {
      try {
        if (await predicate()) return resolve(true);
        if (Date.now() - started > timeout) return reject(new Error("browser condition timed out"));
        setTimeout(poll, 80);
      } catch (error) { reject(error); }
    };
    poll();
  });
}

function freePort() {
  return new Promise(resolve => {
    const server = http.createServer();
    server.listen(0, "127.0.0.1", () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
  });
}

function writeSyntheticCredential(file) {
  const salt = crypto.randomBytes(16);
  const verifier = crypto.scryptSync(TEST_PASSWORD, salt, 32, { N: 32768, r: 8, p: 1, maxmem: 64 * 1024 * 1024 });
  fs.writeFileSync(file, JSON.stringify({
    version: 1,
    data_key: crypto.randomBytes(32).toString("base64"),
    password_salt: salt.toString("base64"),
    password_verifier: verifier.toString("base64"),
    scrypt_n: 32768,
    scrypt_r: 8,
    scrypt_p: 1,
  }), { mode: 0o600 });
}

function staticServer(apiPort) {
  const mime = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml" };
  return http.createServer((request, response) => {
    const pathname = new URL(request.url, "http://localhost").pathname;
    if (pathname.startsWith("/api/grupa-tematy/")) {
      const upstream = http.request({
        host: "127.0.0.1", port: apiPort, path: pathname, method: request.method,
        headers: { ...request.headers, host: `127.0.0.1:${apiPort}` },
      }, upstreamResponse => {
        response.writeHead(upstreamResponse.statusCode, upstreamResponse.headers);
        upstreamResponse.pipe(response);
      });
      upstream.on("error", () => { response.writeHead(502); response.end(); });
      request.pipe(upstream);
      return;
    }
    const relative = pathname === "/" ? "index.html" : pathname.replace(/^\/+/, "");
    const target = path.resolve(SITE, relative, relative.endsWith("/") ? "index.html" : "");
    if (!target.startsWith(SITE + path.sep) || !fs.existsSync(target) || !fs.statSync(target).isFile()) {
      response.writeHead(404); response.end(); return;
    }
    response.writeHead(200, { "Content-Type": mime[path.extname(target)] ?? "application/octet-stream", "X-Content-Type-Options": "nosniff" });
    fs.createReadStream(target).pipe(response);
  });
}

class Chrome {
  constructor() { this.profile = fs.mkdtempSync(path.join(os.tmpdir(), "grupa-tematy-chrome-")); this.process = null; this.port = 0; }
  start() {
    const candidates = ["/home/jan/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome", "/usr/bin/chromium-browser", "/usr/bin/google-chrome", "/usr/bin/chromium"];
    const binary = candidates.find(file => fs.existsSync(file));
    if (!binary) throw new Error("no Chrome executable available");
    return new Promise((resolve, reject) => {
      this.process = spawn(binary, [
        "--headless=new", "--disable-gpu", "--no-sandbox", "--ignore-certificate-errors",
        "--remote-debugging-port=0", `--user-data-dir=${this.profile}`, "--no-first-run", "--no-default-browser-check", "about:blank",
      ], { stdio: ["ignore", "ignore", "pipe"] });
      let buffer = "";
      const timer = setTimeout(() => reject(new Error("Chrome startup timed out")), 20000);
      this.process.stderr.on("data", chunk => {
        buffer += chunk.toString();
        const match = buffer.match(/DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)\//);
        if (match) { clearTimeout(timer); this.port = Number(match[1]); resolve(this.port); }
      });
      this.process.on("exit", code => { if (!this.port) reject(new Error(`Chrome exited (${code})`)); });
    });
  }
  stop() {
    if (this.process) this.process.kill("SIGKILL");
    fs.rmSync(this.profile, { recursive: true, force: true });
  }
}

async function openDriver(port) {
  let targets = [];
  await waitFor(async () => {
    try {
      targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
      return targets.some(target => target.type === "page" && target.webSocketDebuggerUrl);
    } catch { return false; }
  });
  const endpoint = targets.find(target => target.type === "page" && target.webSocketDebuggerUrl)?.webSocketDebuggerUrl;
  check(Boolean(endpoint), "Chrome DevTools page target started");
  const ws = new WebSocket(endpoint);
  await new Promise((resolve, reject) => { ws.addEventListener("open", resolve, { once: true }); ws.addEventListener("error", reject, { once: true }); });
  let nextId = 0;
  const pending = new Map();
  ws.addEventListener("message", event => {
    const message = JSON.parse(String(event.data));
    if (!message.id || !pending.has(message.id)) return;
    const resolve = pending.get(message.id);
    pending.delete(message.id);
    resolve(message);
  });
  const send = (method, params = {}) => new Promise(resolve => {
    const id = ++nextId; pending.set(id, resolve); ws.send(JSON.stringify({ id, method, params }));
  });
  await send("Page.enable");
  await send("Runtime.enable");
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  const evaluate = async expression => {
    const result = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (result.result?.exceptionDetails) throw new Error("page script evaluation failed");
    return result.result?.result?.value;
  };
  const navigate = async url => {
    await send("Page.navigate", { url });
    await waitFor(async () => (await evaluate("document.readyState")) === "complete");
  };
  return { send, evaluate, navigate, close: () => ws.close() };
}

async function main() {
  if (!EVIDENCE) throw new Error("GRUPA_TEMATY_EVIDENCE_DIR is required");
  if (!fs.existsSync(path.join(SITE, "grupa-tematy/index.html"))) throw new Error("built public page is missing");
  fs.mkdirSync(EVIDENCE, { recursive: true });
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "grupa-tematy-flow-"));
  const database = path.join(temp, "data.sqlite3");
  const credential = path.join(temp, "credential.json");
  const apiLog = path.join(EVIDENCE, "browser-api.log");
  writeSyntheticCredential(credential);
  const apiPort = await freePort();
  const site = staticServer(apiPort);
  await new Promise(resolve => site.listen(0, "127.0.0.1", resolve));
  const sitePort = site.address().port;
  const origin = `http://localhost:${sitePort}`;
  const logFd = fs.openSync(apiLog, "w", 0o600);
  const childEnv = { ...process.env,
    GRUPA_TEMATY_DB_PATH: database,
    GRUPA_TEMATY_CREDENTIAL_FILE: credential,
    GRUPA_TEMATY_ALLOWED_ORIGIN: origin,
    PYTHONPATH: ROOT,
  };
  const api = spawn(path.join(ROOT, ".venv/bin/uvicorn"), [
    "scripts.grupa_tematy_api:app", "--host", "127.0.0.1", "--port", String(apiPort), "--no-access-log", "--log-level", "warning",
  ], { cwd: ROOT, env: childEnv, stdio: ["ignore", logFd, logFd] });
  const browser = new Chrome();
  let driver;
  const results = [];
  try {
    const apiReady = await waitFor(async () => {
      if (api.exitCode !== null) throw new Error("API process exited early");
      try { return (await fetch(`http://127.0.0.1:${apiPort}/api/grupa-tematy/csrf`)).ok; } catch { return false; }
    });
    check(apiReady, "loopback API process is ready");
    const chromePort = await browser.start();
    driver = await openDriver(chromePort);
    await driver.navigate(`${origin}/grupa-tematy/`);
    const initial = await driver.evaluate(`(() => ({
      form: !!document.querySelector('#proposal-form'),
      topic: !!document.querySelector('#proposal-form textarea[name=topic]'),
      login: !!document.querySelector('#login-form'),
      list: !!document.querySelector('#proposal-list'),
      listEmpty: !document.querySelector('#proposal-list')?.children.length,
      overflow: document.documentElement.scrollWidth > innerWidth,
      hasSecretText: document.body.innerText.includes('Tajny temat testowy') || document.body.innerText.includes('Osoba testowa'),
    }))()`);
    check(initial.form && initial.topic && initial.login && initial.list && initial.listEmpty, "public proposal and password forms are present without private list data");
    check(!initial.overflow, "mobile page fits viewport");
    check(!initial.hasSecretText, "private markers are absent before login");
    results.push("PASS mobile form and login gate");

    await driver.evaluate(`(() => {
      const form = document.querySelector('#proposal-form');
      const input = form.querySelector('[name=topic]');
      input.value = ${JSON.stringify(TOPIC)};
      input.dispatchEvent(new Event('input', { bubbles: true }));
      form.requestSubmit();
    })()`);
    await waitFor(async () => (await driver.evaluate("document.querySelector('#proposal-status')?.dataset.state === 'success'")) === true);
    check(await driver.evaluate("!document.querySelector('#proposal-list')?.children.length"), "public submit does not reveal list");
    results.push("PASS public proposal submission");

    await driver.evaluate(`(() => {
      const form = document.querySelector('#login-form');
      form.querySelector('[name=password]').value = ${JSON.stringify(TEST_PASSWORD)};
      form.requestSubmit();
    })()`);
    await waitFor(async () => (await driver.evaluate("document.querySelector('#proposal-list')?.textContent.includes('Temat dostępności')")) === true);
    const visible = await driver.evaluate(`(() => ({
      topic: document.querySelector('#proposal-list').textContent.includes('Temat dostępności'),
      renderedImg: !!document.querySelector('#proposal-list img'),
      renderedScript: !!document.querySelector('#proposal-list script'),
      localStorage: localStorage.length,
      sessionStorage: sessionStorage.length,
    }))()`);
    check(visible.topic && !visible.renderedImg && !visible.renderedScript, "authorized list renders proposal as text without HTML execution");
    check(visible.localStorage === 0 && visible.sessionStorage === 0, "password and session data stay out of browser storage");
    results.push("PASS password login and safe private list");

    await driver.evaluate(`(() => {
      const row = document.querySelector('#proposal-list article');
      row.querySelector('[name=assignee]').value = ${JSON.stringify(ASSIGNEE)};
      row.querySelector('form.assignment-form').requestSubmit();
    })()`);
    await waitFor(async () => (await driver.evaluate("document.querySelector('#proposal-list')?.textContent.includes('Osoba testowa — przegląd')")) === true);
    check(await driver.evaluate("document.querySelector('#proposal-list').textContent.includes('Temat dostępności')"), "named assignment appears after update");
    results.push("PASS named assignment");

    await driver.navigate(`${origin}/grupa-tematy/`);
    await waitFor(async () => (await driver.evaluate("document.querySelector('#proposal-list')?.textContent.includes('Osoba testowa — przegląd')")) === true);
    check(await driver.evaluate("document.querySelector('#proposal-list').textContent.includes('Temat dostępności')"), "reload preserves proposal and assignment");
    results.push("PASS assignment persists after browser reload");

    const leak = fs.readFileSync(apiLog);
    check(!leak.includes(Buffer.from(TEST_PASSWORD)) && !leak.includes(Buffer.from(TOPIC)) && !leak.includes(Buffer.from(ASSIGNEE)), "API log contains no password or private text");
    results.push("PASS no credential or private text in API log");
    fs.writeFileSync(path.join(EVIDENCE, "browser-results.txt"), results.join("\n") + "\n", { mode: 0o600 });
    process.stdout.write(results.join("\n") + "\n");
  } finally {
    if (driver) driver.close();
    browser.stop();
    api.kill("SIGTERM");
    await new Promise(resolve => api.once("exit", resolve));
    fs.closeSync(logFd);
    await new Promise(resolve => site.close(resolve));
    fs.rmSync(temp, { recursive: true, force: true });
  }
}

main().catch(error => {
  if (EVIDENCE) {
    fs.mkdirSync(EVIDENCE, { recursive: true });
    fs.writeFileSync(path.join(EVIDENCE, "browser-results.txt"), `FAIL browser flow: ${error.message}\n`, { mode: 0o600 });
  }
  process.stderr.write(`FAIL browser flow: ${error.message}\n`);
  process.exitCode = 1;
});
