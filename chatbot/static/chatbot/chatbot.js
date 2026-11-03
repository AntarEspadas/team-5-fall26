"use strict";

/*
 * Parks Assistant chat widget.
 *
 * Typed messages go to the assistant as JSON. Voice messages are recorded in
 * the browser, encoded as 16 kHz mono WAV, and sent to the server, which
 * transcribes them in whatever language was spoken. Replies to voice messages
 * are read aloud in the same language with the browser's speech synthesis.
 */
(() => {
    const root = document.getElementById("parks-assistant");
    if (!root) return;

    const $ = (id) => document.getElementById(id);
    const panel = $("pa-panel");
    const launcher = $("pa-launcher");
    const log = $("pa-log");
    const welcome = $("pa-welcome");
    const form = $("pa-form");
    const input = $("pa-input");
    const sendButton = $("pa-send");
    const micButton = $("pa-mic");
    const badge = $("pa-badge");
    const recordingBar = $("pa-recording");
    const recordingTime = $("pa-recording-time");
    const csrfToken = root.querySelector("input[name=csrfmiddlewaretoken]").value;
    const urls = root.dataset;

    const OPEN_KEY = "pa-open";
    const PENDING_KEY = "pa-pending";
    const MAX_RECORDING_SECONDS = 60;
    const TARGET_SAMPLE_RATE = 16000;
    const SITE_PATHS = "events|accounts|search|messages|notifications|assistant";

    let busy = false;
    let historyRequest = null;
    let recorder = null;
    let voiceLanguage = "";
    const speech = "speechSynthesis" in window ? window.speechSynthesis : null;

    /* Storage can be unavailable (private windows, blocked cookies). */
    const store = {
        get(area, key) {
            try { return window[area].getItem(key); } catch (e) { return null; }
        },
        set(area, key, value) {
            try {
                if (value === null) window[area].removeItem(key);
                else window[area].setItem(key, value);
            } catch (e) { /* Ignore: the widget works without storage. */ }
        },
    };

    /* ---------- Rendering ---------- */

    function escapeHtml(text) {
        return text.replace(/[&<>"']/g, (c) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
        })[c]);
    }

    function link(href, label) {
        const external = /^https?:/i.test(href);
        const target = external ? ' target="_blank" rel="noopener noreferrer"' : "";
        return `<a href="${href}"${target}>${label}</a>`;
    }

    const LINK_PATTERN = new RegExp(
        "\\[([^\\]]+)\\]\\(((?:https?://|/)[^\\s)]+)\\)" +
        "|(https?://[^\\s<]+)" +
        `|(^|[\\s(])(/(?:${SITE_PATHS})\\b[^\\s<)]*)`,
        "gi"
    );

    function trimPunctuation(url) {
        const match = url.match(/^(.*?)([.,;:!?]*)$/);
        return [match[1], match[2]];
    }

    function inline(text) {
        let html = escapeHtml(text).replace(
            LINK_PATTERN,
            (whole, label, href, url, lead, path) => {
                if (href) return link(href, label);
                if (url) {
                    const [clean, tail] = trimPunctuation(url);
                    return link(clean, clean) + tail;
                }
                const [clean, tail] = trimPunctuation(path);
                return lead + link(clean, clean) + tail;
            }
        );
        html = html.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
        return html.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
    }

    /* A small, safe subset of Markdown: bullets, bold, italics, links. */
    function renderMarkdown(text) {
        const blocks = [];
        let lines = [];
        let items = [];
        const flushLines = () => {
            if (lines.length) blocks.push(lines.join("\n"));
            lines = [];
        };
        const flushItems = () => {
            if (items.length) {
                blocks.push(`<ul>${items.map((i) => `<li>${i}</li>`).join("")}</ul>`);
            }
            items = [];
        };
        text.replace(/\r/g, "").split("\n").forEach((raw) => {
            const bullet = raw.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)$/);
            if (bullet) {
                flushLines();
                items.push(inline(bullet[1]));
                return;
            }
            flushItems();
            const heading = raw.match(/^\s*#{1,6}\s+(.*)$/);
            lines.push(heading ? `<strong>${inline(heading[1])}</strong>` : inline(raw));
        });
        flushLines();
        flushItems();
        return blocks.join("").replace(/\n{3,}/g, "\n\n").trim();
    }

    function plainText(text) {
        return text
            .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
            .replace(/[*#_`]/g, "")
            .replace(/https?:\/\/\S+/g, "");
    }

    function scrollToEnd() {
        log.scrollTop = log.scrollHeight;
    }

    function addMessage(role, text, options = {}) {
        welcome.hidden = true;
        const message = document.createElement("div");
        message.className = `pa-message is-${role}${options.error ? " is-error" : ""}`;
        const bubble = document.createElement("div");
        bubble.className = "pa-bubble";
        if (role === "model" && !options.error) bubble.innerHTML = renderMarkdown(text);
        else bubble.textContent = text;
        message.append(bubble);

        if (role === "model" && !options.error && (speech || options.escalated)) {
            const meta = document.createElement("div");
            meta.className = "pa-meta";
            if (options.escalated) {
                const note = document.createElement("span");
                note.className = "pa-escalated";
                note.textContent = "Sent to event manager";
                meta.append(note);
            }
            if (speech) {
                const listen = document.createElement("button");
                listen.type = "button";
                listen.className = "pa-listen";
                listen.textContent = "▶ Listen";
                listen.setAttribute("aria-label", "Read this reply aloud");
                listen.addEventListener("click", () => speak(text, voiceLanguage, listen));
                meta.append(listen);
            }
            message.append(meta);
        }
        log.append(message);
        scrollToEnd();
        return message;
    }

    function showTyping() {
        const typing = document.createElement("div");
        typing.className = "pa-message is-model pa-typing";
        typing.setAttribute("aria-label", "The assistant is typing");
        typing.innerHTML = '<div class="pa-bubble"><span></span><span></span><span></span></div>';
        log.append(typing);
        scrollToEnd();
        return typing;
    }

    function clearConversation() {
        log.querySelectorAll(".pa-message").forEach((node) => node.remove());
        welcome.hidden = false;
    }

    /* ---------- Speech output ---------- */

    function speak(text, language, button) {
        if (!speech) return;
        const wasSpeaking = button && button.classList.contains("is-speaking");
        speech.cancel();
        log.querySelectorAll(".pa-listen.is-speaking").forEach((b) => {
            b.classList.remove("is-speaking");
            b.textContent = "▶ Listen";
        });
        if (wasSpeaking) return;

        const utterance = new SpeechSynthesisUtterance(plainText(text));
        const lang = language || document.documentElement.lang || navigator.language;
        utterance.lang = lang;
        const base = lang.toLowerCase().split("-")[0];
        const voice = speech.getVoices().find((v) => v.lang.toLowerCase() === lang.toLowerCase())
            || speech.getVoices().find((v) => v.lang.toLowerCase().startsWith(base));
        if (voice) utterance.voice = voice;
        if (button) {
            button.classList.add("is-speaking");
            button.textContent = "■ Stop";
            utterance.onend = utterance.onerror = () => {
                button.classList.remove("is-speaking");
                button.textContent = "▶ Listen";
            };
        }
        speech.speak(utterance);
    }

    /* ---------- Server calls ---------- */

    async function request(url, options = {}) {
        const response = await fetch(url, {
            credentials: "same-origin",
            ...options,
            headers: { "X-CSRFToken": csrfToken, Accept: "application/json", ...(options.headers || {}) },
        });
        let data = {};
        try { data = await response.json(); } catch (e) { /* Non-JSON error page. */ }
        if (!response.ok) {
            throw new Error(data.error || "Something went wrong. Please try again.");
        }
        return data;
    }

    function setBusy(value) {
        busy = value;
        sendButton.disabled = value;
        micButton.disabled = !voiceSupported || (value && !recorder);
    }

    function rememberPending(escalated) {
        if (escalated) store.set("localStorage", PENDING_KEY, "1");
    }

    function showReply(data, speakIt) {
        const message = addMessage("model", data.reply, { escalated: data.escalated });
        rememberPending(data.escalated);
        if (speakIt) speak(data.reply, voiceLanguage, message.querySelector(".pa-listen"));
    }

    async function sendText(text) {
        text = text.trim();
        if (!text || busy) return;
        setBusy(true);
        input.value = "";
        autosize();
        await loadHistory();
        addMessage("user", text);
        const typing = showTyping();
        try {
            const data = await request(urls.messageUrl, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ message: text, page: location.pathname }),
            });
            typing.remove();
            showReply(data, false);
        } catch (error) {
            typing.remove();
            addMessage("model", error.message, { error: true });
        } finally {
            setBusy(false);
            input.focus();
        }
    }

    async function sendVoice(blob) {
        setBusy(true);
        await loadHistory();
        const placeholder = addMessage("user", "🎤 Voice message…");
        const typing = showTyping();
        const body = new FormData();
        body.append("audio", blob, "voice.wav");
        body.append("page", location.pathname);
        try {
            const data = await request(urls.voiceUrl, { method: "POST", body });
            placeholder.querySelector(".pa-bubble").textContent = data.transcript;
            if (data.language) voiceLanguage = data.language;
            typing.remove();
            showReply(data, true);
        } catch (error) {
            placeholder.remove();
            typing.remove();
            addMessage("model", error.message, { error: true });
        } finally {
            setBusy(false);
        }
    }

    async function fetchHistory() {
        try {
            const data = await request(urls.historyUrl);
            clearConversation();
            data.messages.forEach((m) => addMessage(m.role === "user" ? "user" : "model", m.text));
            store.set("localStorage", PENDING_KEY, data.pending ? "1" : null);
            return data;
        } catch (error) {
            historyRequest = null;
            return null;
        }
    }

    /* Load the saved conversation once, before anything new is shown. */
    function loadHistory() {
        if (!historyRequest) historyRequest = fetchHistory();
        return historyRequest;
    }

    async function resetConversation() {
        if (busy) return;
        if (speech) speech.cancel();
        try {
            await request(urls.resetUrl, { method: "POST" });
            clearConversation();
        } catch (error) {
            addMessage("model", error.message, { error: true });
        }
        input.focus();
    }

    /* ---------- Voice recording ---------- */

    function encodeWav(samples, sampleRate) {
        const buffer = new ArrayBuffer(44 + samples.length * 2);
        const view = new DataView(buffer);
        const writeString = (offset, text) => {
            for (let i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i));
        };
        writeString(0, "RIFF");
        view.setUint32(4, 36 + samples.length * 2, true);
        writeString(8, "WAVE");
        writeString(12, "fmt ");
        view.setUint32(16, 16, true);
        view.setUint16(20, 1, true);
        view.setUint16(22, 1, true);
        view.setUint32(24, sampleRate, true);
        view.setUint32(28, sampleRate * 2, true);
        view.setUint16(32, 2, true);
        view.setUint16(34, 16, true);
        writeString(36, "data");
        view.setUint32(40, samples.length * 2, true);
        samples.forEach((sample, i) => {
            const s = Math.max(-1, Math.min(1, sample));
            view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
        });
        return new Blob([view], { type: "audio/wav" });
    }

    function downsample(chunks, fromRate) {
        const length = chunks.reduce((sum, c) => sum + c.length, 0);
        const merged = new Float32Array(length);
        let offset = 0;
        chunks.forEach((c) => { merged.set(c, offset); offset += c.length; });
        if (fromRate <= TARGET_SAMPLE_RATE) return { samples: merged, rate: fromRate };
        const ratio = fromRate / TARGET_SAMPLE_RATE;
        const result = new Float32Array(Math.floor(length / ratio));
        for (let i = 0; i < result.length; i += 1) {
            const start = Math.floor(i * ratio);
            const end = Math.min(Math.floor((i + 1) * ratio), length);
            let sum = 0;
            for (let j = start; j < end; j += 1) sum += merged[j];
            result[i] = sum / Math.max(1, end - start);
        }
        return { samples: result, rate: TARGET_SAMPLE_RATE };
    }

    const voiceSupported = Boolean(
        window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia
        && (window.AudioContext || window.webkitAudioContext)
    );

    async function startRecording() {
        if (speech) speech.cancel();
        let stream;
        try {
            stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        } catch (error) {
            addMessage("model", "I couldn't use your microphone. Check that the browser has permission, or type your question instead.", { error: true });
            return;
        }
        const Context = window.AudioContext || window.webkitAudioContext;
        const context = new Context();
        const source = context.createMediaStreamSource(stream);
        const processor = context.createScriptProcessor(4096, 1, 1);
        const chunks = [];
        processor.onaudioprocess = (event) => {
            chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
        };
        source.connect(processor);
        processor.connect(context.destination);

        const started = Date.now();
        const timer = window.setInterval(() => {
            const seconds = Math.floor((Date.now() - started) / 1000);
            recordingTime.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
            if (seconds >= MAX_RECORDING_SECONDS) stopRecording(true);
        }, 250);

        recorder = { stream, context, processor, source, chunks, timer, started };
        recordingTime.textContent = "0:00";
        recordingBar.hidden = false;
        micButton.setAttribute("aria-pressed", "true");
        micButton.setAttribute("aria-label", "Stop and send voice message");
        input.disabled = true;
        sendButton.disabled = true;
    }

    function stopRecording(send) {
        if (!recorder) return;
        const { stream, context, processor, source, chunks, timer, started } = recorder;
        recorder = null;
        window.clearInterval(timer);
        processor.disconnect();
        source.disconnect();
        stream.getTracks().forEach((track) => track.stop());
        const rate = context.sampleRate;
        context.close();
        recordingBar.hidden = true;
        micButton.setAttribute("aria-pressed", "false");
        micButton.setAttribute("aria-label", "Speak your question");
        input.disabled = false;
        sendButton.disabled = false;
        if (!send) return;
        if (Date.now() - started < 600) {
            addMessage("model", "That was too short. Tap the mic, speak, then tap again to send.", { error: true });
            return;
        }
        const { samples, rate: outputRate } = downsample(chunks, rate);
        sendVoice(encodeWav(samples, outputRate));
    }

    /* ---------- Panel ---------- */

    function openPanel() {
        panel.hidden = false;
        root.classList.add("is-open");
        launcher.setAttribute("aria-expanded", "true");
        launcher.setAttribute("aria-label", "Close the Parks Assistant");
        badge.hidden = true;
        store.set("sessionStorage", OPEN_KEY, "1");
        loadHistory().then(scrollToEnd);
        window.setTimeout(() => input.focus(), 50);
    }

    function closePanel() {
        stopRecording(false);
        panel.hidden = true;
        root.classList.remove("is-open");
        launcher.setAttribute("aria-expanded", "false");
        launcher.setAttribute("aria-label", "Open the Parks Assistant");
        store.set("sessionStorage", OPEN_KEY, null);
        launcher.focus();
    }

    function autosize() {
        input.style.height = "auto";
        input.style.height = `${Math.min(input.scrollHeight, 120)}px`;
    }

    launcher.addEventListener("click", () => (panel.hidden ? openPanel() : closePanel()));
    root.querySelector("[data-pa-action=close]").addEventListener("click", closePanel);
    root.querySelector("[data-pa-action=reset]").addEventListener("click", resetConversation);
    root.querySelector("[data-pa-action=cancel-recording]").addEventListener("click", () => stopRecording(false));
    root.querySelectorAll(".pa-chip").forEach((chip) => {
        chip.addEventListener("click", () => sendText(chip.textContent));
    });

    form.addEventListener("submit", (event) => {
        event.preventDefault();
        sendText(input.value);
    });
    input.addEventListener("input", autosize);
    input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
            event.preventDefault();
            sendText(input.value);
        }
    });
    panel.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            if (recorder) stopRecording(false);
            else closePanel();
        }
    });

    if (voiceSupported) {
        micButton.addEventListener("click", () => {
            if (recorder) stopRecording(true);
            else if (!busy) startRecording();
        });
    } else {
        micButton.disabled = true;
        micButton.title = "Voice input needs a secure (HTTPS) connection and a supported browser.";
    }

    /* Restore the panel across pages, and check for answers from managers. */
    if (store.get("sessionStorage", OPEN_KEY)) {
        openPanel();
    } else if (store.get("localStorage", PENDING_KEY)) {
        loadHistory().then((data) => {
            if (data && data.updates) badge.hidden = false;
        });
    }
})();
