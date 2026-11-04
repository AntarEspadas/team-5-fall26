"use strict";

(() => {
    const link = document.querySelector("[data-message-count-url]");
    if (!link) return;

    let badge = link.querySelector(".message-count-badge");
    let requestInProgress = false;

    async function refreshMessageCount() {
        if (document.hidden || requestInProgress) return;
        requestInProgress = true;

        try {
            const response = await fetch(link.dataset.messageCountUrl, {
                cache: "no-store",
                headers: { Accept: "application/json" }
            });
            if (!response.ok) return;

            const data = await response.json();
            const count = Number(data.count);
            if (!Number.isInteger(count) || count < 0) return;

            if (count > 0) {
                if (!badge) {
                    badge = document.createElement("span");
                    badge.className = "count-pill message-count-badge";
                    link.append(" ");
                    link.append(badge);
                }
                badge.textContent = `${count} new`;
            } else if (badge) {
                badge.remove();
                badge = null;
            }
            link.setAttribute(
                "aria-label",
                count ? `Messages, ${count} new` : "Messages"
            );
        } catch (error) {
            console.error("Unable to refresh unread message count.", error);
        } finally {
            requestInProgress = false;
        }
    }

    refreshMessageCount();
    window.setInterval(refreshMessageCount, 3000);
    document.addEventListener("visibilitychange", refreshMessageCount);
})();
