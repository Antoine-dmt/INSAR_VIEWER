/* Shared normalized raster view: paired maps retain the same center and zoom. */
class RasterViewport {
  constructor(frame, image, group, redraw, sample) {
    this.frame = frame;
    this.image = image;
    this.group = group;
    this.redraw = redraw;
    this.sample = sample;
    this.pointers = new Map();
    this.dragged = false;
    this.events = new AbortController();
    group.views.add(this);
    frame.tabIndex = 0;
    image.draggable = false;
    image.className = "map-raster";
    const controls = document.createElement("div");
    controls.className = "map-navigation";
    for (const [label, title, action] of [
      ["+", "Zoom in", () => this.zoomAt(2)],
      ["-", "Zoom out", () => this.zoomAt(0.5)],
      ["Fit AOI", "Reset zoom to displayed area", () => {
        Object.assign(group, { zoom: 1, x: 0.5, y: 0.5 });
        this.update();
      }],
    ]) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = label;
      button.title = title;
      button.setAttribute("aria-label", title);
      button.addEventListener("click", action);
      controls.appendChild(button);
    }
    frame.appendChild(controls);
    const listen = (name, fn, options = {}) => frame.addEventListener(name, fn, {
      ...options, signal: this.events.signal,
    });
    listen("wheel", (event) => {
      if (event.target.closest(".map-navigation")) return;
      event.preventDefault();
      const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? frame.clientHeight : 1);
      this.zoomAt(Math.exp(-Math.max(-300, Math.min(300, delta)) * 0.003), this.local(event));
    }, { passive: false });
    listen("pointerdown", (event) => {
      if (event.button !== 0 || event.target.closest(".map-navigation")) return;
      event.preventDefault();
      frame.focus({ preventScroll: true });
      frame.setPointerCapture(event.pointerId);
      if (!this.pointers.size) {
        this.start = this.local(event);
        this.dragged = false;
      } else this.dragged = true;
      this.pointers.set(event.pointerId, this.local(event));
    });
    listen("pointermove", (event) => {
      if (!this.pointers.has(event.pointerId)) return;
      const before = this.gesture();
      this.pointers.set(event.pointerId, this.local(event));
      const after = this.gesture();
      if (this.pointers.size > 1 || Math.hypot(after.x - this.start.x, after.y - this.start.y) > 4) {
        this.dragged = true;
      }
      if (!this.dragged) return;
      frame.classList.add("panning");
      if (before.distance && after.distance) this.zoomAt(after.distance / before.distance, before, false);
      const size = this.size();
      group.x -= (after.x - before.x) / (size.width * group.zoom);
      group.y -= (after.y - before.y) / (size.height * group.zoom);
      this.update();
    });
    listen("pointerup", (event) => {
      if (!this.pointers.has(event.pointerId)) return;
      const clicked = !this.dragged && this.pointers.size === 1;
      this.pointers.delete(event.pointerId);
      if (!this.pointers.size) frame.classList.remove("panning");
      if (clicked) {
        const rect = image.getBoundingClientRect();
        const x = (event.clientX - rect.left) / rect.width;
        const y = (event.clientY - rect.top) / rect.height;
        if (x >= 0 && x < 1 && y >= 0 && y < 1) sample(x, y);
      }
    });
    const cancel = (event) => {
      this.pointers.delete(event.pointerId);
      this.dragged = true;
      if (!this.pointers.size) frame.classList.remove("panning");
    };
    listen("pointercancel", cancel);
    listen("lostpointercapture", cancel);
    listen("keydown", (event) => {
      if (event.target !== frame) return;
      if (["+", "=", "-", "Home"].includes(event.key)) {
        event.preventDefault();
        if (event.key === "Home") Object.assign(group, { zoom: 1, x: 0.5, y: 0.5 });
        else this.zoomAt(event.key === "-" ? 0.5 : 2, undefined, false);
        this.update();
      } else if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) {
        event.preventDefault();
        const size = this.size();
        group.x += ({ ArrowLeft: -40, ArrowRight: 40 }[event.key] || 0) / (size.width * group.zoom);
        group.y += ({ ArrowUp: -40, ArrowDown: 40 }[event.key] || 0) / (size.height * group.zoom);
        this.update();
      }
    });
    this.observer = new ResizeObserver(() => this.layout());
    this.observer.observe(frame);
    this.layout();
  }

  local(event) {
    const rect = this.frame.getBoundingClientRect();
    return { x: event.clientX - rect.left - this.frame.clientLeft,
      y: event.clientY - rect.top - this.frame.clientTop };
  }

  gesture() {
    const [a, b] = [...this.pointers.values()];
    return b ? { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2,
      distance: Math.hypot(a.x - b.x, a.y - b.y) } : a;
  }

  size() {
    const scale = Math.min(this.frame.clientWidth / this.image.naturalWidth,
      this.frame.clientHeight / this.image.naturalHeight);
    return { width: this.image.naturalWidth * scale, height: this.image.naturalHeight * scale };
  }

  zoomAt(factor, anchor, update = true) {
    const { group, frame } = this;
    const size = this.size();
    const next = Math.max(1, Math.min(16, group.zoom * factor));
    anchor ||= { x: frame.clientWidth / 2, y: frame.clientHeight / 2 };
    group.x += (anchor.x - frame.clientWidth / 2) / size.width * (1 / group.zoom - 1 / next);
    group.y += (anchor.y - frame.clientHeight / 2) / size.height * (1 / group.zoom - 1 / next);
    group.zoom = next;
    if (update) this.update();
  }

  update() {
    const size = this.size();
    // Keep the image covering the view where possible, center letterboxed axes.
    for (const [key, dimension, frameSize] of [
      ["x", size.width, this.frame.clientWidth], ["y", size.height, this.frame.clientHeight],
    ]) {
      const margin = Math.min(0.5, frameSize / (2 * dimension * this.group.zoom));
      this.group[key] = Math.max(margin, Math.min(1 - margin, this.group[key]));
    }
    for (const view of this.group.views) view.layout();
  }

  layout() {
    if (!this.image.naturalWidth || !this.frame.clientWidth) return;
    const { width, height } = this.size();
    Object.assign(this.image.style, {
      width: `${width * this.group.zoom}px`, height: `${height * this.group.zoom}px`,
      left: `${this.frame.clientWidth / 2 - this.group.x * width * this.group.zoom}px`,
      top: `${this.frame.clientHeight / 2 - this.group.y * height * this.group.zoom}px`,
    });
    this.redraw();
  }

  destroy() {
    this.events.abort();
    this.observer.disconnect();
    this.group.views.delete(this);
    this.frame.classList.remove("panning");
  }
}
