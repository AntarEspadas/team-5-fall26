/** Preview a selected photo locally; only submitting the form uploads it. */
(() => {
  const input = document.getElementById("id_profile_photo");
  const clear = document.getElementById("profile_photo-clear_id");
  const preview = document.getElementById("profile-photo-preview");
  const status = document.getElementById("profile-photo-preview-status");
  const originalSource = preview.getAttribute("src");
  let selectedUrl = null;

  /** Release the previous selection and restore the saved photo, if present. */
  function resetPreview() {
    if (selectedUrl) {
      URL.revokeObjectURL(selectedUrl);
      selectedUrl = null;
    }
    if (originalSource) {
      preview.src = originalSource;
    } else {
      preview.removeAttribute("src");
    }
    preview.hidden = !originalSource;
    preview.alt = "Your current profile photo";
    status.textContent = "";
    status.hidden = true;
  }

  /** Announce feedback without inserting filenames or other user-supplied HTML. */
  function showStatus(message) {
    status.textContent = message;
    status.hidden = false;
  }

  /** Decode the selection before replacing the saved photo's preview. */
  function previewSelection() {
    const file = input.files[0];
    if (file && clear) clear.checked = false;
    resetPreview();
    if (!file) {
      if (clear && clear.checked) previewClear();
      return;
    }
    if (file.size > 5 * 1024 * 1024) {
      showStatus("Choose a photo no larger than 5 MB.");
      return;
    }
    const url = URL.createObjectURL(file);
    selectedUrl = url;
    const image = new Image();

    /** Ignore outdated loads and display the latest valid selection. */
    image.onload = function showLoadedPhoto() {
      if (selectedUrl !== url) return;
      if (Math.max(image.naturalWidth, image.naturalHeight) > 4096) {
        resetPreview();
        showStatus("Photos must be at most 4096 pixels per side.");
        return;
      }
      preview.src = url;
      preview.alt = "Preview of your selected profile photo";
      preview.hidden = false;
      showStatus("Photo preview. Select Save changes to upload it.");
    };

    /** Keep the saved photo visible when the selection cannot be decoded. */
    image.onerror = function showPreviewError() {
      if (selectedUrl !== url) return;
      resetPreview();
      showStatus("This file could not be previewed. Choose a JPEG, PNG, or WebP photo.");
    };
    image.src = url;
  }

  /** Preview removal on save and discard any conflicting file selection. */
  function previewClear() {
    resetPreview();
    if (clear.checked) {
      input.value = "";
      preview.hidden = true;
      showStatus("Your profile photo will be removed when you select Save changes.");
    }
  }

  input.addEventListener("change", previewSelection);
  if (clear) {
    clear.addEventListener("change", previewClear);
    if (clear.checked) previewClear();
  }
})();
