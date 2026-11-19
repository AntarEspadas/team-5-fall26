"use strict";

/* Copy the clean event URL; show a manual copy option if the browser refuses. */
(() => {
    const share = document.querySelector("[data-event-share]");
    if (!share) return;

    const button = share.querySelector("[data-share-button]");
    const status = share.querySelector("[data-share-status]");
    const fallback = share.querySelector("[data-share-fallback]");
    const input = share.querySelector("[data-share-url]");

    // Try the older copy command when the Clipboard API is unavailable.
    function tryLegacyCopy() {
        fallback.hidden = false;
        input.focus();
        input.select();
        input.setSelectionRange(0, input.value.length);
        try {
            return typeof document.execCommand === "function" &&
                document.execCommand("copy") === true;
        } catch {
            return false;
        }
    }

    button.hidden = false;
    fallback.hidden = true;

    button.addEventListener("click", async () => {
        if (button.disabled) return;
        button.disabled = true;
        status.textContent = "";
        status.classList.remove("is-error");

        let copied = false;
        try {
            if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
                await navigator.clipboard.writeText(input.value);
                copied = true;
            }
        } catch {
            // A denied or unsupported clipboard can still use the fallback.
        }

        if (!copied) copied = tryLegacyCopy();

        button.disabled = false;
        if (copied) {
            fallback.hidden = true;
            status.textContent = "Event link copied to clipboard!";
            button.focus();
        } else {
            status.classList.add("is-error");
            status.textContent = "Could not copy automatically. Copy the link below.";
        }
    });
})();
