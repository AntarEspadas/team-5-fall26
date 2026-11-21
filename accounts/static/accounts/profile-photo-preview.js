/** Preview the framing that the settings form will save. */
(() => {
  const input = document.getElementById("id_profile_photo");
  const clear = document.getElementById("profile_photo-clear_id");
  const preview = document.getElementById("profile-photo-preview");
  const status = document.getElementById("profile-photo-preview-status");
  const editor = document.getElementById("profile-photo-crop-editor");
  const frame = document.getElementById("profile-photo-crop-frame");
  const cropImage = document.getElementById("profile-photo-crop-image");
  const cropX = document.getElementById("id_profile_photo_crop_x");
  const cropY = document.getElementById("id_profile_photo_crop_y");
  const cropZoom = document.getElementById("id_profile_photo_crop_zoom");
  const zoom = document.getElementById("profile-photo-zoom");
  const savedPosition = [cropX.value || "0.5", cropY.value || "0.5", cropZoom.value || "1"];
  let selectedUrl = null;
  let loadVersion = 0;
  let source = null;
  let drag = null;

  function setStatus(message) {
    status.textContent = message;
    status.hidden = !message;
  }

  function cropGeometry() {
    const size = frame.clientWidth;
    const side = Math.min(source.naturalWidth, source.naturalHeight) / Number(zoom.value);
    const scale = size / side;
    return {
      width: source.naturalWidth * scale,
      height: source.naturalHeight * scale,
      travelX: Math.max(0, (source.naturalWidth - side) * scale),
      travelY: Math.max(0, (source.naturalHeight - side) * scale),
    };
  }

  function renderCrop() {
    if (editor.hidden) return;
    const { width, height, travelX, travelY } = cropGeometry();
    cropImage.style.width = `${width}px`;
    cropImage.style.height = `${height}px`;
    cropImage.style.left = `${-travelX * Number(cropX.value)}px`;
    cropImage.style.top = `${-travelY * Number(cropY.value)}px`;
  }

  function loadPhoto(url, position, isUpload = false) {
    const version = ++loadVersion;
    const image = new Image();
    image.onload = () => {
      if (version !== loadVersion) return;
      if (isUpload && Math.max(image.naturalWidth, image.naturalHeight) > 4096) {
        restoreSavedPhoto("Photos must be at most 4096 pixels per side.");
        return;
      }
      source = image;
      [cropX.value, cropY.value, zoom.value] = position;
      cropZoom.value = zoom.value;
      cropImage.src = url;
      editor.hidden = false;
      preview.hidden = true;
      renderCrop();
    };
    image.onerror = () => {
      if (version !== loadVersion) return;
      if (isUpload) {
        restoreSavedPhoto("Choose a readable JPEG, PNG, or WebP photo.");
      } else if (!status.textContent) {
        setStatus("The saved photo could not be loaded for adjustment.");
      }
    };
    image.src = url;
  }

  function resetEditor() {
    loadVersion++;
    drag = null;
    editor.hidden = true;
    cropImage.removeAttribute("src");
    if (selectedUrl) URL.revokeObjectURL(selectedUrl);
    selectedUrl = null;
    preview.hidden = !preview.hasAttribute("src") || Boolean(clear?.checked);
  }

  function restoreSavedPhoto(message = "") {
    resetEditor();
    input.value = "";
    [cropX.value, cropY.value, cropZoom.value] = savedPosition;
    setStatus(message);
    if (cropImage.dataset.source && !clear?.checked) {
      loadPhoto(cropImage.dataset.source, savedPosition);
    }
  }

  function moveCrop(startX, startY, dx, dy) {
    const { travelX, travelY } = cropGeometry();
    if (travelX > 0) cropX.value = Math.min(1, Math.max(0, startX - dx / travelX));
    if (travelY > 0) cropY.value = Math.min(1, Math.max(0, startY - dy / travelY));
    renderCrop();
  }

  input.addEventListener("change", () => {
    const file = input.files[0];
    if (!file) {
      restoreSavedPhoto();
      return;
    }
    if (clear) clear.checked = false;
    resetEditor();
    setStatus("");
    if (file.size > 5 * 1024 * 1024) {
      restoreSavedPhoto("Choose a photo no larger than 5 MB.");
      return;
    }
    [cropX.value, cropY.value, cropZoom.value] = ["0.5", "0.5", "1"];
    selectedUrl = URL.createObjectURL(file);
    loadPhoto(selectedUrl, ["0.5", "0.5", "1"], true);
  });
  zoom.addEventListener("input", () => {
    cropZoom.value = zoom.value;
    renderCrop();
  });
  frame.addEventListener("pointerdown", (event) => {
    if (editor.hidden || event.button !== 0 || drag) return;
    frame.setPointerCapture(event.pointerId);
    drag = {
      pointerId: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      cropX: Number(cropX.value),
      cropY: Number(cropY.value),
    };
  });
  frame.addEventListener("pointermove", (event) => {
    if (drag?.pointerId !== event.pointerId) return;
    moveCrop(drag.cropX, drag.cropY, event.clientX - drag.x, event.clientY - drag.y);
  });
  frame.addEventListener("lostpointercapture", (event) => {
    if (drag?.pointerId === event.pointerId) drag = null;
  });
  frame.addEventListener("keydown", (event) => {
    const directions = {
      ArrowLeft: [-8, 0],
      ArrowRight: [8, 0],
      ArrowUp: [0, -8],
      ArrowDown: [0, 8],
    };
    if (!directions[event.key] || editor.hidden) return;
    event.preventDefault();
    moveCrop(Number(cropX.value), Number(cropY.value), ...directions[event.key]);
  });
  window.addEventListener("resize", renderCrop);
  clear?.addEventListener("change", () => {
    restoreSavedPhoto(clear.checked ? "Save changes to remove your profile photo." : "");
  });
  restoreSavedPhoto();
})();
