/* Sochiera blog unlock. AES-256-GCM + PBKDF2-SHA256; the ciphertext lives in the page, the key never leaves the visitor's machine. */
"use strict";

const AAD = "sochiera/blog-v1";

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
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    const password = input.value;
    if (!password) return;
    try {
      const htmlFragment = await decryptPayload(payload, password);
      if (typeof htmlFragment !== "string") throw new Error("bad plaintext");
      section.classList.remove("locked-entry");
      section.classList.add("entry-block");
      const doc = new DOMParser().parseFromString(htmlFragment, "text/html");
      section.replaceChildren(...doc.body.children);
      form.remove();
      error.remove();
    } catch {
      error.hidden = false;
      input.value = "";
      input.focus();
    }
  });
}

document.querySelectorAll("section.locked-entry").forEach(init);
