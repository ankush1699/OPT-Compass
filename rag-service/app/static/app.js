"use strict";

const EXAMPLES = [
  "How many days of unemployment are allowed during OPT?",
  "Does my OPT end if I transfer schools?",
  "When must I report a new employer on STEM OPT?",
];
const MIN_CHARS = 3;
const MAX_CHARS = 500;
const TIMEOUT_MS = 120000; // the free-tier model can be slow
const ABSTAIN = "the indexed sources do not answer this";

const chat = document.getElementById("chat");
const intro = document.getElementById("intro");
const form = document.getElementById("form");
const box = document.getElementById("question");
const count = document.getElementById("count");
const sendBtn = document.getElementById("send");
const stopBtn = document.getElementById("stop");

let controller = null;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function scrollDown() {
  window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
}

function setBusy(busy) {
  sendBtn.disabled = busy;
  box.disabled = busy;
  stopBtn.hidden = !busy;
}

function updateCount() {
  count.textContent = box.value.length + " / " + MAX_CHARS;
}

for (const text of EXAMPLES) {
  const chip = el("button", "chip", text);
  chip.type = "button";
  chip.addEventListener("click", () => ask(text));
  document.getElementById("chips").appendChild(chip);
}

box.addEventListener("input", updateCount);
box.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    form.requestSubmit();
  }
});
form.addEventListener("submit", (event) => {
  event.preventDefault();
  ask(box.value);
});
stopBtn.addEventListener("click", () => controller && controller.abort("user"));

// Turns "[1][2]" in the answer into links to the numbered sources, building DOM nodes and text nodes only.
function renderAnswer(target, text, sources) {
  target.replaceChildren();
  for (const part of text.split(/(\[\d+\])/)) {
    const match = /^\[(\d+)\]$/.exec(part);
    const n = match ? Number(match[1]) : 0;
    if (n >= 1 && n <= sources.length) {
      const link = el("a", "cite", part);
      link.href = "#src-" + n;
      target.appendChild(link);
    } else {
      target.appendChild(document.createTextNode(part));
    }
  }
}

function renderSources(sources, open) {
  const panel = el("details", "sources");
  panel.open = open;
  panel.appendChild(el("summary", "", "Sources (" + sources.length + ")"));
  const list = el("ol");
  for (const s of sources) {
    const item = el("li", "src");
    item.id = "src-" + s.n;
    const head = el("div", "head");
    head.appendChild(el("span", "num", "[" + s.n + "]"));
    head.appendChild(el("span", "badge " + (s.kind === "REGULATION" ? "reg" : "com"), s.kind === "REGULATION" ? "Regulation" : "Commentary"));
    if (typeof s.url === "string" && s.url.startsWith("https://")) {
      const link = el("a", "", s.citation);
      link.href = s.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      head.appendChild(link);
    } else {
      head.appendChild(el("span", "", s.citation));
    }
    item.appendChild(head);
    item.appendChild(el("p", "snippet", s.snippet));
    list.appendChild(item);
  }
  panel.appendChild(list);
  return panel;
}

function usageLine(usage) {
  if (!usage) return "";
  const secs = typeof usage.total_s === "number" ? usage.total_s.toFixed(1) + " s" : "";
  const model = usage.model || "no model";
  return [model, "free tier", secs].filter(Boolean).join(" · ");
}

function friendlyHttpError(status, detail) {
  if (detail) return detail;
  if (status === 404) return "The demo is not switched on at this address.";
  if (status === 422) return "Please enter between " + MIN_CHARS + " and " + MAX_CHARS + " characters.";
  if (status === 429) return "Too many requests. Please wait a moment and try again.";
  if (status >= 500) return "The server had a problem. Please try again in a minute.";
  return "Something went wrong (" + status + "). Please try again.";
}

async function readError(response) {
  try {
    const body = await response.json();
    return typeof body.detail === "string" ? body.detail : "";
  } catch {
    return "";
  }
}

// One parsed Server-Sent Event: {name, data}. Events are separated by a blank line.
function parseEvents(buffer) {
  const blocks = buffer.split("\n\n");
  const rest = blocks.pop();
  const events = [];
  for (const block of blocks) {
    let name = "message";
    let data = "";
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) name = line.slice(6).trim();
      else if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    if (data) {
      try {
        events.push({ name, data: JSON.parse(data) });
      } catch {
        /* a malformed event is skipped rather than breaking the answer */
      }
    }
  }
  return [events, rest];
}

function showError(bubble, message, question) {
  bubble.classList.add("error");
  const status = bubble.querySelector(".status");
  if (status) status.remove();
  bubble.appendChild(el("p", "", message));
  const retry = el("button", "secondary retry", "Try again");
  retry.type = "button";
  retry.addEventListener("click", () => {
    bubble.remove();
    ask(question);
  });
  bubble.appendChild(retry);
}

async function ask(raw) {
  const question = raw.trim();
  if (controller) return;
  if (question.length < MIN_CHARS) {
    box.focus();
    return;
  }
  if (question.length > MAX_CHARS) {
    alert("Please keep the question under " + MAX_CHARS + " characters.");
    return;
  }
  intro.hidden = true;
  chat.appendChild(el("div", "msg user", question));
  const bubble = el("div", "msg bot");
  const status = el("p", "status", "Searching the regulations…");
  const textNode = el("p", "text");
  bubble.append(status, textNode);
  chat.appendChild(bubble);
  box.value = "";
  updateCount();
  scrollDown();

  controller = new AbortController();
  const timer = setTimeout(() => controller.abort("timeout"), TIMEOUT_MS);
  setBusy(true);

  let sources = [];
  let text = "";
  let finished = false;
  let failed = false;
  try {
    const response = await fetch("/demo/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
      signal: controller.signal,
    });
    if (!response.ok) {
      showError(bubble, friendlyHttpError(response.status, await readError(response)), question);
      failed = true;
      return;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      const [events, rest] = parseEvents(buffer);
      buffer = rest;
      for (const { name, data } of events) {
        if (name === "sources") {
          sources = Array.isArray(data) ? data : [];
          status.textContent = sources.length ? "Writing an answer from " + sources.length + " passages…" : "No passages found.";
        } else if (name === "token") {
          text += data.text || "";
          textNode.textContent = text;
          status.hidden = true;
          scrollDown();
        } else if (name === "error") {
          failed = true;
          if (sources.length) bubble.appendChild(renderSources(sources, true));
          showError(bubble, data.detail || "Something went wrong. Please try again.", question);
        } else if (name === "done") {
          finished = true;
          status.remove();
          if (text) {
            renderAnswer(textNode, text, sources);
            if (text.toLowerCase().includes(ABSTAIN)) {
              bubble.appendChild(el("p", "abstain-note", "The indexed sources don't cover this, so no answer is given rather than a guess. The closest passages are below."));
            }
          } else if (data.mode === "retrieval-only") {
            textNode.textContent = "No AI model is connected, so these are the best matching passages.";
          } else {
            textNode.textContent = "The model returned no answer. The closest passages are below.";
          }
          if (sources.length) bubble.appendChild(renderSources(sources, !text || text.toLowerCase().includes(ABSTAIN)));
          const line = usageLine(data.usage);
          if (line) bubble.appendChild(el("p", "meta", line));
        }
      }
    }
    if (!finished && !failed) {
      failed = true;
      if (sources.length) bubble.appendChild(renderSources(sources, true));
      showError(bubble, "The connection dropped before the answer finished." + (text ? " What arrived is shown above." : ""), question);
    }
  } catch (error) {
    failed = true;
    const reason = controller && controller.signal.reason;
    let message;
    if (reason === "user") message = "Stopped.";
    else if (reason === "timeout") message = "This is taking too long (the free model can be slow). Please try again.";
    else message = "Could not reach the server. Check your connection and try again.";
    if (sources.length) bubble.appendChild(renderSources(sources, true));
    showError(bubble, message, question);
  } finally {
    clearTimeout(timer);
    controller = null;
    setBusy(false);
    if (failed && !text) textNode.remove();
    box.focus();
    scrollDown();
  }
}

updateCount();
