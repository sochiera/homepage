/* Sochiera blog unlock. AES-256-GCM + PBKDF2-SHA256; the ciphertext lives in the page, the key never leaves the visitor's machine. */
"use strict";

const AAD = "sochiera/blog-v1";
const STORAGE_PREFIX = "sochiera-unlock/";
const STORAGE_TTL_MS = 6 * 60 * 60 * 1000;
const protectedEntries = new Map();
let unlockInProgress = false;

function bytesToU8(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function u8ToText(bytes) {
  return new TextDecoder().decode(bytes);
}

async function deriveKey(password, salt, iterations) {
  const material = new TextEncoder().encode(password);
  const base = await crypto.subtle.importKey("raw", material, "PBKDF2", false, ["deriveKey"]);
  return crypto.subtle.deriveKey(
    { name: "PBKDF2", hash: "SHA-256", iterations: iterations, salt: salt },
    base,
    { name: "AES-GCM", length: 256 },
    false,
    ["decrypt"]
  );
}

async function decryptPayload(payload, password) {
  const key = await deriveKey(password, bytesToU8(payload.s), payload.i);
  const plaintext = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: bytesToU8(payload.n), additionalData: new TextEncoder().encode(AAD) },
    key,
    bytesToU8(payload.c)
  );
  return u8ToText(new Uint8Array(plaintext));
}

/* After a successful unlock the entry is remembered for the current tab only:
 * sessionStorage holds the decrypted fragment — never the password — scoped
 * under the entry's key-agreement identity (iterations+salt), so a rebuilt
 * page (new salt) never inherits an old unlock and the marker never travels
 * between machines, browsers, or sessions. sessionStorage is tab-scoped and
 * cleared when the tab closes; every restore requires this same-tab record,
 * so entry content stays locked wherever it has never been unlocked. */
function authorizeEntry(payload, fragment) {
  const record = { v: 1, t: Date.now(), c: fragment };
  try {
    sessionStorage.setItem(STORAGE_PREFIX + `${payload.i}.${payload.s}`, JSON.stringify(record));
  } catch {
    /* Persistence is an enhancement; without storage the manual unlock stays fully functional. */
  }
}

function storedFragment(payload) {
  const raw = sessionStorage.getItem(STORAGE_PREFIX + `${payload.i}.${payload.s}`);
  if (!raw) return null;
  let record;
  try { record = JSON.parse(raw); } catch { return null; }
  if (!record || record.v !== 1 || !Number.isFinite(record.t) || Date.now() - record.t > STORAGE_TTL_MS) return null;
  return typeof record.c === "string" && record.c ? record.c : null;
}

function parseChildren(fragment) {
  const doc = new DOMParser().parseFromString(fragment, "text/html");
  return [...doc.body.children];
}

function applyUnlocked(section, children) {
  section.classList.remove("locked-entry");
  section.classList.add("entry-block");
  const heading = section.querySelector("h2");
  if (heading) section.replaceChildren(heading, ...children);
  else section.replaceChildren(...children);
  section.querySelector("form.unlock")?.remove();
  section.querySelector(".unlock-error")?.remove();
}

function init(section) {
  const form = section.querySelector("form.unlock");
  const input = form.querySelector("input[name=haslo]");
  const error = section.querySelector(".unlock-error");
  let payload;
  try {
    payload = JSON.parse(section.dataset.protected);
    if (payload.v !== 1 || !Number.isFinite(payload.i) || payload.i <= 0) throw new Error("bad payload");
  } catch {
    section.hidden = true;
    return;
  }
  protectedEntries.set(section, payload);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (unlockInProgress) return;
    error.hidden = true;
    const password = input.value;
    if (!password) return;
    unlockInProgress = true;
    try {
      const htmlFragment = await decryptPayload(payload, password);
      if (typeof htmlFragment !== "string") throw new Error("bad plaintext");
      const decryptedEntries = await Promise.all(
        [...protectedEntries].map(async ([candidate, candidatePayload]) => {
          if (!candidate.isConnected || !candidate.classList.contains("locked-entry")) return null;
          try {
            const fragment = candidate === section ? htmlFragment : await decryptPayload(candidatePayload, password);
            return { section: candidate, payload: candidatePayload, fragment };
          } catch {
            return null;
          }
        })
      );
      for (const entry of decryptedEntries) {
        if (!entry) continue;
        authorizeEntry(entry.payload, entry.fragment);
        applyUnlocked(entry.section, parseChildren(entry.fragment));
      }
      document.querySelectorAll('form.unlock input[name="haslo"]').forEach((field) => { field.value = ""; });
    } catch {
      error.hidden = false;
      input.value = "";
      input.focus();
    } finally {
      unlockInProgress = false;
    }
  });
  try {
    const restored = storedFragment(payload);
    if (restored) applyUnlocked(section, parseChildren(restored));
  } catch {
    /* A broken record merely keeps the entry locked behind the prompt. */
  }
}

document.querySelectorAll("section.locked-entry").forEach(init);
