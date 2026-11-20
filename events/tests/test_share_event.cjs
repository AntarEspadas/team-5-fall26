// Run with: node --test events/tests/test_share_event.cjs
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const vm = require("node:vm");

const source = readFileSync(resolve(__dirname, "../static/events/share-event.js"), "utf8");
const url = "https://parks.example/events/42/";

function setup({ clipboard, legacyCopy, selectionError = false } = {}) {
    const classes = new Set();
    const button = {
        hidden: true, disabled: false, focus() { this.focused = true; },
        addEventListener(name, handler) { this[name] = handler; },
    };
    const status = {
        textContent: "",
        classList: { add: (name) => classes.add(name), remove: (name) => classes.delete(name) },
    };
    const fallback = { hidden: false };
    const input = {
        value: url, focus() { this.focused = true; },
        select() {
            if (selectionError) throw new Error("Selection unsupported");
            this.selected = true;
        },
        setSelectionRange(start, end) { this.range = [start, end]; },
    };
    const elements = {
        "[data-share-button]": button, "[data-share-status]": status,
        "[data-share-fallback]": fallback, "[data-share-url]": input,
    };
    const share = { querySelector: (selector) => elements[selector] };
    vm.runInNewContext(source, {
        document: { querySelector: () => share, execCommand: legacyCopy },
        navigator: { clipboard },
    });
    return { button, status, fallback, input, classes };
}

test("copies the canonical event URL and confirms success", async () => {
    let copied;
    const ui = setup({ clipboard: { writeText: async (value) => { copied = value; } } });
    assert.equal(ui.button.hidden, false);
    assert.equal(ui.fallback.hidden, true);
    await ui.button.click();
    assert.equal(copied, url);
    assert.equal(ui.status.textContent, "Event link copied to clipboard!");
    assert.equal(ui.fallback.hidden, true);
    assert.equal(ui.button.disabled, false);
});

test("uses legacy copy when the Clipboard API is absent", async () => {
    let command;
    const ui = setup({ legacyCopy: (value) => { command = value; return true; } });
    await ui.button.click();
    assert.equal(command, "copy");
    assert.equal(ui.input.selected, true);
    assert.equal(ui.fallback.hidden, true);
    assert.match(ui.status.textContent, /copied to clipboard/);
});

test("denied clipboard access can still use legacy copy", async () => {
    const ui = setup({
        clipboard: { writeText: async () => { throw new Error("Denied"); } },
        legacyCopy: () => true,
    });
    await ui.button.click();
    assert.match(ui.status.textContent, /copied to clipboard/);
});

test("reports failure honestly and selects the manual link", async () => {
    const ui = setup({
        clipboard: { writeText: async () => { throw new Error("Denied"); } },
        legacyCopy: () => false,
    });
    await ui.button.click();
    assert.equal(ui.fallback.hidden, false);
    assert.equal(ui.input.focused, true);
    assert.deepEqual(ui.input.range, [0, url.length]);
    assert.equal(ui.classes.has("is-error"), true);
    assert.match(ui.status.textContent, /Could not copy automatically/);
    assert.equal(ui.button.disabled, false);
});

test("handles missing or throwing legacy copy without claiming success", async () => {
    for (const legacyCopy of [undefined, () => { throw new Error("Unsupported"); }]) {
        const ui = setup({ legacyCopy });
        await ui.button.click();
        assert.equal(ui.fallback.hidden, false);
        assert.match(ui.status.textContent, /Could not copy automatically/);
    }
});

test("prevents overlapping copy attempts and allows retry", async () => {
    let finish;
    let attempts = 0;
    const ui = setup({
        clipboard: { writeText: () => { attempts++; return new Promise((done) => { finish = done; }); } },
    });
    const first = ui.button.click();
    await ui.button.click();
    assert.equal(attempts, 1);
    finish();
    await first;
    const second = ui.button.click();
    finish();
    await second;
    assert.equal(attempts, 2);
    assert.equal(ui.button.disabled, false);
});

test("selection failure leaves a manual link and permits retry", async () => {
    const ui = setup({ selectionError: true, legacyCopy: () => true });
    await ui.button.click();
    assert.equal(ui.fallback.hidden, false);
    assert.equal(ui.button.disabled, false);
    assert.equal(ui.classes.has("is-error"), true);
    assert.match(ui.status.textContent, /Could not copy automatically/);
    await ui.button.click();
    assert.equal(ui.button.disabled, false);
});

test("a page without the share component does not throw", () => {
    assert.doesNotThrow(() => vm.runInNewContext(source, {
        document: { querySelector: () => null },
    }));
});
