// Run with: node --test events/tests/test_chat_ui.cjs
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const vm = require("node:vm");

class Element {
    constructor(tag = "div", className = "") {
        this.tag = tag;
        this.className = className;
        this.children = [];
        this.dataset = {};
        this.events = {};
        this.queries = {};
        this.textContent = "";
        this.value = "";
        this.style = {};
        this.classList = {
            add: (name) => { this.className += ` ${name}`; },
            remove: (name) => { this.className = this.className.split(" ").filter((c) => c !== name).join(" "); },
            contains: (name) => this.className.split(" ").includes(name),
        };
    }
    append(...nodes) {
        nodes.forEach((node) => { node.parent = this; this.children.push(node); });
    }
    remove() {
        this.removed = true;
        if (this.parent) this.parent.children = this.parent.children.filter((n) => n !== this);
    }
    querySelector(selector) { return this.queries[selector] || this.querySelectorAll(selector)[0] || null; }
    querySelectorAll(selector) {
        const matches = (node) => selector.split(",").some((part) => {
            part = part.trim();
            if (part.startsWith(".")) return part.slice(1).split(".").every((c) => node.classList.contains(c));
            if (part.startsWith("[data-message-id=")) return String(node.dataset.messageId) === part.match(/"(.*?)"/)[1];
            return node.tag === part;
        });
        return this.children.flatMap((child) => [...(matches(child) ? [child] : []), ...child.querySelectorAll(selector)]);
    }
    setAttribute(name, value) { this[name] = value; }
    addEventListener(name, listener) { this.events[name] = listener; }
    focus() { this.focused = true; }
}

const response = (data, ok = true) => ({ ok, json: async () => data });
const settle = () => new Promise((done) => setImmediate(done));
const incoming = (id) => ({ id, sender_id: 2, sender_name: "Peer", body: "New message", created_at: "2026-11-08T12:00:00Z" });
const banned = { code: "user_banned", error: "Your account is currently banned." };

function conversation({ composer = true, sendReply = response(banned, false) } = {}) {
    const history = new Element("section", "chat-history");
    history.dataset = { latestMessageId: "0", currentUserId: "1", updatesUrl: "/updates/" };
    const form = new Element("form");
    const input = new Element("textarea");
    const button = new Element("button");
    form.append(input, button);
    const status = new Element();
    const connection = new Element();
    const selectors = { ".chat-history": history, ".chat-composer": composer ? form : null, ".chat-status": status, ".chat-connection-status": connection };
    let tick;
    let reads = 0;
    let writes = 0;
    const context = {
        document: { hidden: false, querySelector: (s) => selectors[s], createElement: (tag) => new Element(tag), addEventListener() {} },
        window: { location: { href: "/conversation/" }, setInterval: (fn) => { tick = fn; } },
        fetch: async (_url, options) => {
            if (options.method === "POST") { writes += 1; return sendReply; }
            reads += 1;
            return response({ messages: [incoming(reads)] });
        },
        URLSearchParams,
        FormData: class { *[Symbol.iterator]() { yield ["body", input.value]; } },
    };
    vm.runInNewContext(readFileSync(resolve(__dirname, "../static/events/conversation.js"), "utf8"), context);
    return { history, form, input, button, status, connection, tick: () => tick(), writes: () => writes };
}

test("conversation polls and renders incoming messages without a composer", async () => {
    const ui = conversation({ composer: false });
    await settle();
    assert.equal(ui.history.querySelectorAll(".message-row").length, 1);
    await ui.tick();
    assert.equal(ui.history.querySelectorAll(".message-row").length, 2);
    assert.equal(ui.connection.textContent, "");
    assert.equal(ui.writes(), 0);
});

test("a stale conversation displays the ban error, removes its composer, and keeps polling", async () => {
    const ui = conversation();
    await settle();
    ui.input.value = "Hello";
    await ui.form.events.submit({ preventDefault() {} });
    assert.equal(ui.status.textContent, banned.error);
    assert.equal(ui.form.removed, true);
    assert.equal(ui.button.disabled, true);
    await ui.tick();
    assert.equal(ui.history.querySelectorAll(".message-row").length, 2);
    assert.equal(ui.status.textContent, banned.error);
    await ui.form.events.submit({ preventDefault() {} });
    assert.equal(ui.writes(), 1);
});

test("unbanned conversations still submit and display messages", async () => {
    const ui = conversation({ sendReply: response({ message: { ...incoming(9), sender_id: 1 } }) });
    await settle();
    ui.input.value = "Hello";
    await ui.form.events.submit({ preventDefault() {} });
    assert.equal(ui.input.value, "");
    assert.equal(ui.button.disabled, false);
    assert.equal(ui.form.removed, undefined);
    assert.equal(ui.history.querySelectorAll(".is-own-message").length, 1);
});

test("a stale assistant displays the ban error and disables further submissions", async () => {
    const ids = Object.fromEntries([
        "parks-assistant", "pa-panel", "pa-launcher", "pa-log", "pa-welcome",
        "pa-form", "pa-input", "pa-send", "pa-mic", "pa-badge", "pa-call",
        "pa-call-state", "pa-call-time",
    ].map((id) => [id, new Element()]));
    const root = ids["parks-assistant"];
    root.dataset = { messageUrl: "/chat", historyUrl: "/history" };
    root.queries["input[name=csrfmiddlewaretoken]"] = { value: "csrf" };
    for (const action of ["close", "reset", "end-call"]) root.queries[`[data-pa-action=${action}]`] = new Element("button");
    let writes = 0;
    const context = {
        document: { getElementById: (id) => ids[id], createElement: (tag) => new Element(tag), documentElement: { lang: "en" } },
        navigator: {}, location: { pathname: "/" },
        window: { sessionStorage: { getItem: () => null }, localStorage: { getItem: () => null } },
        fetch: async (_url, options) => {
            if (options.method === "POST") { writes += 1; return response(banned, false); }
            return response({ messages: [], pending: false });
        },
    };
    vm.runInNewContext(readFileSync(resolve(__dirname, "../../chatbot/static/chatbot/chatbot.js"), "utf8"), context);
    ids["pa-input"].value = "Hi";
    ids["pa-form"].events.submit({ preventDefault() {} });
    await settle();
    assert.equal(ids["pa-send"].disabled, true);
    assert.equal(ids["pa-mic"].disabled, true);
    assert.equal(ids["pa-input"].disabled, true);
    const errors = ids["pa-log"].querySelectorAll(".is-error");
    assert.equal(errors.length, 1);
    assert.equal(errors[0].children[0].textContent, banned.error);
    ids["pa-input"].value = "Try again";
    ids["pa-form"].events.submit({ preventDefault() {} });
    await settle();
    assert.equal(writes, 1);
});
