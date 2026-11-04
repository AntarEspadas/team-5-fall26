"use strict";

/*
 * Parks Assistant chat widget.
 *
 * Typed messages go to the assistant as JSON. The mic button starts a live
 * voice call with Gemini's Live API: the server hands out a single-use token
 * (locked to the assistant's voice, instructions, and tools), the browser
 * streams 16 kHz microphone audio over a WebSocket and plays the 24 kHz
 * spoken replies, and both sides are transcribed into the chat as they talk.
 * Tools run on the server, and each finished exchange is saved there.
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
    const callBar = $("pa-call");
    const callState = $("pa-call-state");
    const callTime = $("pa-call-time");
    const csrfToken = root.querySelector("input[name=csrfmiddlewaretoken]").value;
    const urls = root.dataset;

    const OPEN_KEY = "pa-open";
    const PENDING_KEY = "pa-pending";
    const INPUT_RATE = 16000;
    const OUTPUT_RATE = 24000;
    const SITE_PATHS = "events|accounts|search|messages|notifications|assistant";

    let busy = false;
    let historyRequest = null;
    let call = null;
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

        if (options.live) message.classList.add("is-live");
        if (role === "model" && !options.error && !options.live && speech) {
            const meta = document.createElement("div");
            meta.className = "pa-meta";
            const listen = document.createElement("button");
            listen.type = "button";
            listen.className = "pa-listen";
            listen.textContent = "▶ Listen";
            listen.setAttribute("aria-label", "Read this reply aloud");
            listen.addEventListener("click", () => speak(text, "", listen));
            meta.append(listen);
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
        micButton.disabled = !voiceSupported || (value && !call);
    }

    async function sendText(text) {
        text = text.trim();
        if (call && text) {
            input.value = "";
            autosize();
            sendCallText(text);
            return;
        }
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
            addMessage("model", data.reply);
        } catch (error) {
            typing.remove();
            addMessage("model", error.message, { error: true });
        } finally {
            setBusy(false);
            input.focus();
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
        endCall();
        if (speech) speech.cancel();
        try {
            await request(urls.resetUrl, { method: "POST" });
            clearConversation();
        } catch (error) {
            addMessage("model", error.message, { error: true });
        }
        input.focus();
    }

    /* ---------- Voice conversation ---------- */

    const voiceSupported = Boolean(
        window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia
        && window.AudioContext && window.AudioWorkletNode && window.WebSocket
    );

    /* Runs on the audio thread: averages the microphone down to 16 kHz and
       posts 100 ms chunks of 16-bit PCM along with how loud they were. */
    const CAPTURE_PROCESSOR = `
        class PaCapture extends AudioWorkletProcessor {
            constructor() {
                super();
                this.step = Math.max(1, sampleRate / ${INPUT_RATE});
                this.chunk = new Int16Array(${INPUT_RATE / 10});
                this.length = 0;
                this.phase = 0;
                this.sum = 0;
                this.count = 0;
                this.energy = 0;
            }
            process(inputs) {
                const channel = inputs[0] && inputs[0][0];
                if (!channel) return true;
                for (let i = 0; i < channel.length; i += 1) {
                    this.sum += channel[i];
                    this.count += 1;
                    this.phase += 1;
                    if (this.phase < this.step) continue;
                    this.phase -= this.step;
                    const s = Math.max(-1, Math.min(1, this.sum / this.count));
                    this.sum = 0;
                    this.count = 0;
                    this.energy += s * s;
                    this.chunk[this.length] = s < 0 ? s * 0x8000 : s * 0x7fff;
                    this.length += 1;
                    if (this.length === this.chunk.length) {
                        const level = Math.sqrt(this.energy / this.length);
                        this.port.postMessage({ pcm: this.chunk.buffer.slice(0), level });
                        this.length = 0;
                        this.energy = 0;
                    }
                }
                return true;
            }
        }
        registerProcessor("pa-capture", PaCapture);
    `;

    function toBase64(buffer) {
        const bytes = new Uint8Array(buffer);
        let binary = "";
        for (let i = 0; i < bytes.length; i += 0x8000) {
            binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
        }
        return btoa(binary);
    }

    /* Decode little-endian 16-bit PCM into samples the speakers can play. */
    function pcmSamples(base64) {
        const binary = atob(base64);
        const samples = new Float32Array(binary.length >> 1);
        for (let i = 0; i < samples.length; i += 1) {
            let value = binary.charCodeAt(2 * i) | (binary.charCodeAt(2 * i + 1) << 8);
            if (value >= 0x8000) value -= 0x10000;
            samples[i] = value / 0x8000;
        }
        return samples;
    }

    function newTurn() {
        return {
            userText: "", userEl: null,
            modelText: "", shownText: "", modelEl: null,
            interrupted: false, awaitingAnswer: false, timers: [],
        };
    }

    function setCallState(current, state, label) {
        if (call !== current) return;
        callBar.dataset.state = state;
        callState.textContent = label;
    }

    function listening(current) {
        setCallState(current, "listening", "Listening…");
    }

    function sendToCall(current, message) {
        if (current.socket && current.socket.readyState === WebSocket.OPEN) {
            current.socket.send(JSON.stringify(message));
        }
    }

    function setBubble(element, text) {
        element.querySelector(".pa-bubble").textContent = text.trim();
        scrollToEnd();
    }

    async function startCall() {
        if (call || busy) return;
        if (speech) speech.cancel();
        // Create the audio contexts during the click so the browser lets them play.
        const micContext = new AudioContext();
        const speaker = new AudioContext({ sampleRate: OUTPUT_RATE });
        const analyser = speaker.createAnalyser();
        analyser.fftSize = 512;
        analyser.connect(speaker.destination);
        const current = {
            micContext, speaker, analyser, socket: null, stream: null,
            turn: newTurn(), sources: new Set(), playhead: 0, lastChunkAt: 0,
            toolsRunning: 0, cancelled: new Set(), micLevel: 0, timer: null, meter: 0,
        };
        call = current;
        micContext.resume().catch(() => {});
        speaker.resume().catch(() => {});

        callTime.textContent = "0:00";
        callBar.hidden = false;
        setCallState(current, "connecting", "Connecting…");
        micButton.setAttribute("aria-pressed", "true");
        micButton.setAttribute("aria-label", "End the voice conversation");
        micButton.title = "End call";

        await loadHistory();
        try {
            current.stream = await navigator.mediaDevices.getUserMedia({
                audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
            });
        } catch (error) {
            if (call === current) {
                endCall("I couldn't use your microphone. Check that the browser has permission, or type your question instead.");
            }
            return;
        }
        if (call !== current) {
            current.stream.getTracks().forEach((track) => track.stop());
            return;
        }
        try {
            const moduleUrl = URL.createObjectURL(new Blob([CAPTURE_PROCESSOR], { type: "application/javascript" }));
            await micContext.audioWorklet.addModule(moduleUrl);
            URL.revokeObjectURL(moduleUrl);
            const session = await request(urls.liveUrl, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ page: location.pathname }),
            });
            if (call === current) openSocket(current, session);
        } catch (error) {
            if (call === current) endCall(error.message || "The voice call couldn't start. Please try again.");
        }
    }

    function openSocket(current, session) {
        const socket = new WebSocket(`${session.url}?access_token=${encodeURIComponent(session.token)}`);
        socket.binaryType = "arraybuffer";
        current.socket = socket;
        const decoder = new TextDecoder();
        socket.onopen = () => socket.send(JSON.stringify({ setup: {} }));
        socket.onmessage = (event) => {
            if (call !== current) return;
            let message;
            try {
                message = JSON.parse(typeof event.data === "string" ? event.data : decoder.decode(event.data));
            } catch (error) {
                return;
            }
            handleCallMessage(current, message);
        };
        socket.onclose = (event) => {
            if (call !== current) return;
            if (event.reason) console.warn("Voice call closed:", event.code, event.reason);
            endCall(current.started
                ? "The voice call ended. Tap the mic to talk again."
                : "The voice call couldn't connect. Please try again in a moment.");
        };
    }

    function handleCallMessage(current, message) {
        if (message.setupComplete) {
            beginStreaming(current);
            return;
        }
        if (message.toolCall) runTools(current, message.toolCall.functionCalls || []);
        if (message.toolCallCancellation) {
            (message.toolCallCancellation.ids || []).forEach((id) => current.cancelled.add(id));
        }
        if (message.voiceActivity && message.voiceActivity.type === "ACTIVITY_END"
            && !current.sources.size && !current.toolsRunning) {
            setCallState(current, "thinking", "Thinking…");
        }
        const content = message.serverContent;
        if (!content) return;
        if (content.interrupted) interrupted(current);
        if (content.inputTranscription && content.inputTranscription.text) {
            heardVisitor(current, content.inputTranscription.text);
        }
        ((content.modelTurn && content.modelTurn.parts) || []).forEach((part) => {
            const audio = part.inlineData;
            if (audio && audio.data && /^audio\/pcm/.test(audio.mimeType || "")) play(current, audio.data);
        });
        if (content.outputTranscription && content.outputTranscription.text) {
            spoke(current, content.outputTranscription.text);
        }
        if (content.turnComplete) finishTurn(current);
    }

    function beginStreaming(current) {
        const { micContext } = current;
        const source = micContext.createMediaStreamSource(current.stream);
        const node = new AudioWorkletNode(micContext, "pa-capture");
        node.port.onmessage = ({ data }) => {
            if (call !== current) return;
            current.micLevel = data.level;
            sendToCall(current, {
                realtimeInput: { audio: { data: toBase64(data.pcm), mimeType: `audio/pcm;rate=${INPUT_RATE}` } },
            });
        };
        // Route through a silent gain so the browser keeps the worklet running.
        const silent = micContext.createGain();
        silent.gain.value = 0;
        source.connect(node);
        node.connect(silent).connect(micContext.destination);

        current.started = Date.now();
        current.timer = window.setInterval(() => {
            const seconds = Math.floor((Date.now() - current.started) / 1000);
            callTime.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
        }, 500);
        listening(current);
        startMeter(current);
    }

    /* Show how loud the visitor (or the assistant, while she talks) is. */
    function startMeter(current) {
        const samples = new Uint8Array(current.analyser.fftSize);
        const tick = () => {
            if (call !== current) return;
            let level = current.micLevel;
            if (current.sources.size) {
                current.analyser.getByteTimeDomainData(samples);
                let sum = 0;
                samples.forEach((value) => { sum += ((value - 128) / 128) ** 2; });
                level = Math.sqrt(sum / samples.length);
            }
            callBar.style.setProperty("--pa-level", Math.min(1, level * 5).toFixed(2));
            current.meter = window.requestAnimationFrame(tick);
        };
        tick();
    }

    function heardVisitor(current, text) {
        // The visitor spoke after the assistant's reply: that reply is done.
        if (current.turn.modelEl) finishTurn(current, true);
        const turn = current.turn;
        if (!turn.userEl) turn.userEl = addMessage("user", "", { live: true });
        turn.userText += text;
        setBubble(turn.userEl, turn.userText);
    }

    function play(current, base64) {
        const samples = pcmSamples(base64);
        if (!samples.length) return;
        const { speaker } = current;
        const buffer = speaker.createBuffer(1, samples.length, OUTPUT_RATE);
        buffer.copyToChannel(samples, 0);
        const source = speaker.createBufferSource();
        source.buffer = buffer;
        source.connect(current.analyser);
        const startAt = Math.max(speaker.currentTime + 0.05, current.playhead);
        source.start(startAt);
        current.playhead = startAt + buffer.duration;
        current.lastChunkAt = startAt;
        current.sources.add(source);
        setCallState(current, "speaking", "Speaking…");
        source.onended = () => {
            current.sources.delete(source);
            if (!current.sources.size && !current.toolsRunning) listening(current);
        };
    }

    /* Add the assistant's words to her bubble as they are heard, not before. */
    function spoke(current, text) {
        const turn = current.turn;
        if (turn.userEl) turn.userEl.classList.remove("is-live");
        if (!turn.modelEl) turn.modelEl = addMessage("model", "", { live: true });
        if (turn.awaitingAnswer && !current.toolsRunning) {
            // The answer after a lookup continues the same reply.
            turn.awaitingAnswer = false;
            if (turn.modelText && !/\s$/.test(turn.modelText) && !/^\s/.test(text)) text = ` ${text}`;
        }
        turn.modelText += text;
        const element = turn.modelEl;
        const reveal = () => {
            turn.shownText += text;
            setBubble(element, turn.shownText);
        };
        const delay = (current.lastChunkAt - current.speaker.currentTime) * 1000;
        if (delay > 30) turn.timers.push(window.setTimeout(reveal, delay));
        else reveal();
    }

    /* The visitor talked over the assistant: stop speaking straight away. */
    function interrupted(current) {
        current.sources.forEach((source) => {
            try { source.stop(); } catch (error) { /* Already stopped. */ }
        });
        current.sources.clear();
        current.playhead = 0;
        const turn = current.turn;
        turn.timers.forEach((timer) => window.clearTimeout(timer));
        turn.timers = [];
        turn.interrupted = true;
        listening(current);
    }

    /* Close the current exchange in the transcript and save it. */
    function finishTurn(current, force = false) {
        const turn = current.turn;
        const reply = (turn.interrupted ? turn.shownText : turn.modelText).trim();
        const message = turn.userText.trim();
        // Wait while a lookup runs or its answer is still to come.
        if (!force && (current.toolsRunning || turn.awaitingAnswer)) return;
        if (!reply && !force && !turn.modelEl) return;
        if (!reply) {
            // Nothing was said back: keep the visitor's words for the next reply.
            if (turn.modelEl) turn.modelEl.remove();
            turn.modelEl = null;
            turn.modelText = turn.shownText = "";
            turn.interrupted = false;
            return;
        }
        current.turn = newTurn();
        if (turn.userEl) turn.userEl.classList.remove("is-live");
        const element = turn.modelEl;
        const settle = () => {
            if (turn.interrupted) setBubble(element, reply);
            element.classList.remove("is-live");
        };
        const lag = turn.interrupted ? 0 : (current.playhead - current.speaker.currentTime) * 1000;
        if (lag > 30) window.setTimeout(settle, lag);
        else settle();
        saveTurn(message, reply);
    }

    function saveTurn(message, reply) {
        request(urls.liveTurnUrl, {
            method: "POST",
            keepalive: true,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message, reply, page: location.pathname }),
        }).catch(() => { /* The transcript is still on screen. */ });
    }

    async function runTools(current, calls) {
        current.toolsRunning += 1;
        current.turn.awaitingAnswer = true;
        setCallState(current, "thinking", "Looking that up…");
        const responses = await Promise.all(calls.map(async (fc) => {
            let result;
            try {
                const data = await request(urls.liveToolUrl, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ name: fc.name, args: fc.args || {} }),
                });
                result = data.result;
            } catch (error) {
                result = { error: error.message };
            }
            return { id: fc.id, name: fc.name, response: { result } };
        }));
        current.toolsRunning -= 1;
        if (call !== current) return;
        const wanted = responses.filter((response) => !current.cancelled.has(response.id));
        if (wanted.length) sendToCall(current, { toolResponse: { functionResponses: wanted } });
        if (!current.toolsRunning && !current.sources.size) setCallState(current, "thinking", "Thinking…");
    }

    /* Typing during a call sends the words into the conversation. */
    function sendCallText(text) {
        const current = call;
        if (current.turn.modelEl) finishTurn(current, true);
        const turn = current.turn;
        turn.userText += `${turn.userText ? " " : ""}${text}`;
        if (!turn.userEl) turn.userEl = addMessage("user", "");
        setBubble(turn.userEl, turn.userText);
        sendToCall(current, { realtimeInput: { text } });
    }

    function endCall(notice) {
        const current = call;
        if (!current) return;
        // Save what was said so far, then hang up.
        current.turn.interrupted = true;
        current.turn.timers.forEach((timer) => window.clearTimeout(timer));
        if (current.turn.shownText.trim()) {
            current.playhead = 0;
            finishTurn(current, true);
        } else if (current.turn.userText.trim()) {
            saveTurn(current.turn.userText.trim(), "");
        }
        call = null;
        window.clearInterval(current.timer);
        window.cancelAnimationFrame(current.meter);
        current.sources.forEach((source) => {
            try { source.stop(); } catch (error) { /* Already stopped. */ }
        });
        if (current.socket) current.socket.close(1000);
        if (current.stream) current.stream.getTracks().forEach((track) => track.stop());
        current.micContext.close().catch(() => {});
        current.speaker.close().catch(() => {});
        log.querySelectorAll(".pa-message.is-live").forEach((node) => {
            node.classList.remove("is-live");
            if (!node.textContent.trim()) node.remove();
        });
        callBar.hidden = true;
        micButton.setAttribute("aria-pressed", "false");
        micButton.setAttribute("aria-label", "Start a voice conversation");
        micButton.title = "Talk";
        if (notice) addMessage("model", notice, { error: true });
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
        endCall();
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
    root.querySelector("[data-pa-action=end-call]").addEventListener("click", () => endCall());
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
            if (call) endCall();
            else closePanel();
        }
    });

    if (voiceSupported) {
        micButton.addEventListener("click", () => {
            if (call) endCall();
            else startCall();
        });
        window.addEventListener("pagehide", () => endCall());
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
