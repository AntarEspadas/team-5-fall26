// Close the "Add to calendar" menu after a choice, an outside click, or Escape.
document.addEventListener("click", function (event) {
    const choseOption = event.target.closest(".calendar-menu-options a");
    document.querySelectorAll(".calendar-menu[open]").forEach(function (menu) {
        if (choseOption || !menu.contains(event.target)) menu.open = false;
    });
});

document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    const menu = event.target.closest(".calendar-menu[open]");
    if (menu) {
        menu.open = false;
        menu.querySelector("summary").focus();
        event.preventDefault();
    }
});
