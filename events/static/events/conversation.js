"use strict";

(() => {
    const history = document.querySelector(".chat-history");
    if (!history) return;
    const currentUserId = Number(history.dataset.currentUserId);
    const list = history.querySelector(".message-list");
    const composer = document.querySelector(".chat-composer");
    const messageField = composer?.querySelector("textarea");
    const sendButton = composer?.querySelector("button");
    const status = document.querySelector(".chat-status");
    const connectionStatus = document.querySelector(".chat-connection-status");
    let submissionsBlocked = !composer;
    let latestMessageId = Number(history.dataset.latestMessageId);
    let pollInProgress = false;

    function appendMessage(message) {
        if (history.querySelector(`[data-message-id="${message.id}"]`)) {
            latestMessageId = Math.max(latestMessageId, message.id);
            return;
        }

        const rows = list || history.querySelector(".message-list") || createMessageList();
        const item = document.createElement("li");
        item.className = "message-row";
        item.dataset.messageId = message.id;
        if (message.sender_id === currentUserId) {
            item.classList.add("is-own-message");
        }

        const bubble = document.createElement("div");
        bubble.className = "message-bubble";
        if (message.sender_id !== currentUserId) {
            const sender = document.createElement("span");
            sender.className = "message-sender";
            sender.textContent = message.sender_name;
            bubble.append(sender);
        }

        const body = document.createElement("p");
        body.className = "message-body";
        body.textContent = message.body;
        bubble.append(body);

        const time = document.createElement("time");
        time.className = "message-time";
        time.dateTime = message.created_at;
        time.textContent = new Intl.DateTimeFormat(undefined, {
            hour: "numeric",
            minute: "2-digit"
        }).format(new Date(message.created_at));
        bubble.append(time);
        item.append(bubble);
        rows.append(item);
        latestMessageId = Math.max(latestMessageId, message.id);
        history.scrollTop = history.scrollHeight;
    }

    function createMessageList() {
        const newList = document.createElement("ol");
        newList.className = "message-list";
        const empty = history.querySelector(".chat-empty");
        if (empty) empty.remove();
        history.append(newList);
        return newList;
    }

    async function pollMessages() {
        if (pollInProgress || document.hidden) return;
        pollInProgress = true;
        try {
            const response = await fetch(
                `${history.dataset.updatesUrl}?after=${latestMessageId}`,
                {
                    cache: "no-store",
                    headers: { "Accept": "application/json" }
                }
            );
            if (!response.ok) throw new Error("Message updates are unavailable.");
            const data = await response.json();
            data.messages.forEach(appendMessage);
            connectionStatus.textContent = "";
        } catch (error) {
            connectionStatus.textContent = "Trying to reconnect...";
        } finally {
            pollInProgress = false;
        }
    }

    composer?.addEventListener("submit", async function (event) {
        event.preventDefault();
        if (submissionsBlocked || !messageField.value.trim()) return;
        sendButton.disabled = true;
        status.textContent = "";
        try {
            const response = await fetch(window.location.href, {
                method: "POST",
                body: new URLSearchParams(new FormData(composer)),
                headers: {
                    "Accept": "application/json",
                    "X-Requested-With": "XMLHttpRequest"
                }
            });
            const data = await response.json();
            if (data.code === "user_banned") {
                submissionsBlocked = true;
                composer.remove();
            }
            if (!response.ok) throw new Error(data.error || "Message could not be sent.");
            appendMessage(data.message);
            messageField.value = "";
            messageField.focus();
        } catch (error) {
            status.textContent = error.message || "Message could not be sent.";
        } finally {
            sendButton.disabled = submissionsBlocked;
        }
    });

    history.scrollTop = history.scrollHeight;
    pollMessages();
    window.setInterval(pollMessages, 3000);
    document.addEventListener("visibilitychange", function () {
        if (!document.hidden) pollMessages();
    });
})();
