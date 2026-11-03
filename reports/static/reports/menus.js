document.addEventListener("click", function (event) {
    document.querySelectorAll(".report-menu[open]").forEach(function (menu) {
        if (!menu.contains(event.target)) menu.open = false;
    });
});

document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    const menu = event.target.closest(".report-menu[open]");
    if (menu) {
        menu.open = false;
        menu.querySelector("summary").focus();
        event.preventDefault();
    }
});
