"use strict";

/* Notification bell: load the latest notifications when the menu opens. */
(() => {
    const bell = document.querySelector("[data-bell-url]");
    if (!bell) return;

    const body = bell.querySelector("[data-bell-body]");
    const toggle = bell.querySelector(".bell-toggle");
    const token = document.querySelector(
        ".site-header [name=csrfmiddlewaretoken]"
    );

    function showUnread(count) {
        let badge = toggle.querySelector(".bell-badge");
        if (count > 0) {
            if (!badge) {
                badge = document.createElement("span");
                badge.className = "bell-badge notification-count-badge";
                toggle.append(badge);
            }
            badge.textContent = String(count);
        } else if (badge) {
            badge.remove();
        }
        toggle.setAttribute(
            "aria-label",
            count ? `Notifications, ${count} new` : "Notifications"
        );
        // Keep the phone tab bar's Alerts badge in step with the bell.
        document
            .querySelectorAll(".tabbar a[href$='/notifications/']")
            .forEach((tab) => {
                let tabBadge = tab.querySelector(".tab-badge");
                if (count > 0) {
                    if (!tabBadge) {
                        tabBadge = document.createElement("span");
                        tabBadge.className = "tab-badge";
                        tab.append(tabBadge);
                    }
                    tabBadge.textContent = String(count);
                } else if (tabBadge) {
                    tabBadge.remove();
                }
                tab.setAttribute(
                    "aria-label",
                    count ? `Notifications, ${count} new` : "Notifications"
                );
            });
    }

    async function load() {
        try {
            const response = await fetch(bell.dataset.bellUrl, {
                method: "POST",
                cache: "no-store",
                headers: {
                    Accept: "application/json",
                    "X-CSRFToken": token ? token.value : "",
                },
            });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data = await response.json();
            body.innerHTML = data.html;
            showUnread(Number(data.unread_count) || 0);
        } catch (error) {
            body.innerHTML =
                '<p class="bell-empty bell-error">Couldn’t load ' +
                "notifications. Use See all notifications instead.</p>";
        }
    }

    bell.addEventListener("toggle", () => {
        if (bell.open) load();
    });

    // Close header menus when clicking elsewhere or pressing Escape.
    const menus = document.querySelectorAll(".site-header details");
    document.addEventListener("click", (event) => {
        menus.forEach((menu) => {
            if (menu.open && !menu.contains(event.target)) menu.open = false;
        });
    });
    document.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") return;
        menus.forEach((menu) => {
            if (menu.open) {
                menu.open = false;
                menu.querySelector("summary").focus();
            }
        });
    });
})();
