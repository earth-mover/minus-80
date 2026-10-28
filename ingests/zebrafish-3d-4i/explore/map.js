// The interactive map of the S-BIAD3228 zip, written into map.html by zipmap.py.
//
// Four panels, all drawn on canvases from the JSON that zipmap.py embeds:
// the whole zip, its central directory, one well's bytes, and that well's
// chunk grid; plus a table of the well's runs and a decoder for member names.
// Positions are byte offsets in the zip; within a well they count from the
// well's first byte.
"use strict";

// ---------------------------------------------------------------------------
// Page data
// ---------------------------------------------------------------------------

const PAGE = JSON.parse(document.getElementById("data").textContent);

// zipmap.py sends each per-chunk column as base64 of a typed array, which is
// about a tenth the size of a JSON list for a well's 57,000 chunks.
function decodeColumn(base64, ArrayType) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index);
  return new ArrayType(bytes.buffer);
}

// Chunk flags, one byte per chunk, set by zipmap.py's well_page_data.
const FLAG_EMPTY = 1;
const FLAG_PADDING = 2;
const FLAG_TO_DECODE = 4;

const CHUNK_PIXELS = 1000;
const PLANES_PER_CHUNK = 10;

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

function formatBytes(count) {
  if (count >= 1e9) return (count / 1e9).toFixed(2) + " GB";
  if (count >= 1e6) return (count / 1e6).toFixed(1) + " MB";
  if (count >= 1e3) return (count / 1e3).toFixed(1) + " kB";
  return count + " B";
}

// Axis labels use as few decimals as the tick step allows.
function formatTick(offset, step) {
  if (step >= 1e9) return (offset / 1e9).toFixed(step >= 1e10 ? 0 : 1) + " GB";
  if (step >= 1e6) return (offset / 1e6).toFixed(step >= 1e7 ? 0 : 1) + " MB";
  if (step >= 1e3) return (offset / 1e3).toFixed(0) + " kB";
  return offset.toFixed(0) + " B";
}

// ---------------------------------------------------------------------------
// Colors
// ---------------------------------------------------------------------------

// Canvases cannot use CSS variables, so the colors are read from the page's
// tokens, and read again when the system switches between light and dark.
function cssToken(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function readColors() {
  return {
    contents: ["--round-0", "--round-1", "--round-2", "--round-3", "--labels"].map(cssToken),
    padding: cssToken("--padding"), untouched: cssToken("--untouched"), empty: cssToken("--empty"),
    rampLow: cssToken("--ramp-low"), rampHigh: cssToken("--ramp-high"),
    ink: cssToken("--ink"), muted: cssToken("--muted"), gridline: cssToken("--gridline"),
    axis: cssToken("--axis"), surface: cssToken("--surface"), highlight: cssToken("--highlight"),
  };
}

let colors = readColors();
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  colors = readColors();
  redraw();
});

function rampColor(fraction) {
  const channels = hex => [1, 3, 5].map(index => parseInt(hex.slice(index, index + 2), 16));
  const low = channels(colors.rampLow), high = channels(colors.rampHigh);
  return "rgb(" + low.map((value, index) => Math.round(value + (high[index] - value) * fraction)).join(",") + ")";
}

// The categorical slot for an image run's round, or 4 for labels.
const CONTENTS_LABELS = ["round 0", "round 1", "round 2", "round 3", "labels"];
const LABELS_SLOT = 4;

// ---------------------------------------------------------------------------
// Canvas sizing
// ---------------------------------------------------------------------------

// A canvas is drawn at devicePixelRatio times its CSS size so it stays sharp
// on high-density screens. Setting canvas.height to the device-pixel height
// also changes the height attribute, which is where the CSS height was first
// read from; without keeping the original in data-css-height, every redraw at
// a ratio of 2 doubled the canvas again.
function prepareCanvas(canvas) {
  if (!canvas.dataset.cssHeight) canvas.dataset.cssHeight = canvas.getAttribute("height");
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth, height = Number(canvas.dataset.cssHeight);
  canvas.style.height = height + "px";
  const deviceWidth = Math.round(width * ratio), deviceHeight = Math.round(height * ratio);
  if (canvas.width !== deviceWidth || canvas.height !== deviceHeight) {
    canvas.width = deviceWidth;
    canvas.height = deviceHeight;
  }
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  return { context, width, height };
}

// ---------------------------------------------------------------------------
// Tooltip
// ---------------------------------------------------------------------------

const tooltip = document.getElementById("tooltip");

// rows: [label, value, emphasized]. Text goes in with textContent, since
// names come from the archive.
function showTooltip(event, rows) {
  tooltip.replaceChildren();
  for (const [label, value, emphasized] of rows) {
    const line = document.createElement("div");
    if (label) {
      const labelElement = document.createElement("span");
      labelElement.className = "label";
      labelElement.textContent = label + " ";
      line.appendChild(labelElement);
    }
    const valueElement = document.createElement(emphasized ? "b" : "span");
    valueElement.textContent = value;
    line.appendChild(valueElement);
    tooltip.appendChild(line);
  }
  tooltip.style.display = "block";
  const box = tooltip.getBoundingClientRect();
  let left = event.clientX + 14, top = event.clientY + 14;
  if (left + box.width > innerWidth - 8) left = event.clientX - box.width - 14;
  if (top + box.height > innerHeight - 8) top = event.clientY - box.height - 14;
  tooltip.style.left = left + "px";
  tooltip.style.top = top + "px";
}

function hideTooltip() {
  tooltip.style.display = "none";
}

// ---------------------------------------------------------------------------
// Member names
// ---------------------------------------------------------------------------

function levelMeaning(level) {
  return "pyramid level " + level + (level > 0 ? ` (1/${2 ** level} size)` : " (full size)");
}

function zIndexMeaning(z) {
  return `z index: planes ${PLANES_PER_CHUNK * z}–${PLANES_PER_CHUNK * z + PLANES_PER_CHUNK - 1}`;
}

function gridMeaning(axis, index) {
  return `grid ${axis}: pixels ${CHUNK_PIXELS * index}–${CHUNK_PIXELS * index + CHUNK_PIXELS - 1}`;
}

// A member name split into its parts, each with what it means:
// [[part, meaning], ...]. Channel names come from the well's own metadata,
// because round 3's channels differ between wells.
function explainName(name) {
  const parts = name.split("/");
  if (parts.length < 3) return null;
  const well = parts[0] + "/" + parts[1];
  const explained = [[well, `well: plate row ${parts[0]}, column ${parts[1]}`]];
  const rest = parts.slice(2);
  const last = rest[rest.length - 1];
  if (last && last.startsWith(".")) {
    const documents = { ".zarray": "array metadata", ".zattrs": "attributes", ".zgroup": "group marker" };
    return [...explained, ...rest.slice(0, -1).map(part => [part, "group or array"]), [last, documents[last] || "metadata"]];
  }
  if (rest.includes("tables")) {
    return [...explained, [rest[0], "round"], ["tables", "AnnData tables"], ...rest.slice(2).map(part => [part, "table part"])];
  }
  if (rest[1] === "labels" && rest.length === 7) {
    const [round, , labelName, level, z, y, x] = rest;
    return [...explained, [round, `round ${round} (labels belong to round 1)`], ["labels", "labels group"],
      [labelName, "segmentation name"], [level, levelMeaning(Number(level))], [z, zIndexMeaning(Number(z))],
      [y, gridMeaning("row", y)], [x, gridMeaning("column", x)]];
  }
  if (rest.length === 6) {
    const [round, level, channel, z, y, x] = rest;
    const channelNames = ((PAGE.wells[well] || {}).channels || {})[round];
    const channelName = channelNames ? ": " + channelNames[Number(channel)] : "";
    return [...explained, [round, "round " + round], [level, levelMeaning(Number(level))],
      [channel, "channel " + channel + channelName], [z, zIndexMeaning(Number(z))],
      [y, gridMeaning("row", y)], [x, gridMeaning("column", x)]];
  }
  return [...explained, ...rest.map(part => [part, ""])];
}

function renderParts(container, parts, separator) {
  container.replaceChildren();
  parts.forEach(([text, meaning], index) => {
    if (index > 0) {
      const separatorElement = document.createElement("div");
      separatorElement.className = "key-separator";
      separatorElement.textContent = separator;
      container.appendChild(separatorElement);
    }
    const part = document.createElement("div");
    part.className = "key-part";
    const textElement = document.createElement("b");
    textElement.textContent = text;
    const meaningElement = document.createElement("span");
    meaningElement.textContent = meaning;
    part.append(textElement, meaningElement);
    container.appendChild(part);
  });
}

function renderName(container, name) {
  const parts = explainName(name);
  if (parts) renderParts(container, parts, "/");
  else container.replaceChildren();
}

// ---------------------------------------------------------------------------
// The selected well
// ---------------------------------------------------------------------------

let well = null;               // the decoded well being shown
let view = [0, 1];             // the byte range of the well panel, from the well's start
let hoveredChunk = -1;         // index into the well's chunk columns
let hoveredCell = null;        // [level, row, column] under the pointer in the grid panel
let selectedRun = -1;          // index into well.runs, after a click on a run

function loadWell(name) {
  const data = PAGE.wells[name];
  const columns = data.chunks;
  well = {
    name,
    start: data.start, size: data.size,
    arrays: data.arrays, arrayKinds: data.array_kinds,
    runs: data.runs, metadataMembers: data.metadata_members,
    fields: data.fields, grid: data.grid, channels: data.channels,
    chunkStart: decodeColumn(columns.start, Float64Array),
    chunkBytes: decodeColumn(columns.bytes, Uint32Array),
    chunkCompressed: decodeColumn(columns.compressed, Uint32Array),
    chunkFrame: decodeColumn(columns.frame, Uint32Array),
    chunkArray: decodeColumn(columns.array, Uint16Array),
    chunkChannel: decodeColumn(columns.c, Int8Array),
    chunkZ: decodeColumn(columns.z, Int16Array),
    chunkRow: decodeColumn(columns.y, Int16Array),
    chunkColumn: decodeColumn(columns.x, Int16Array),
    chunkFlags: decodeColumn(columns.flags, Uint8Array),
  };
  well.chunkCount = well.chunkStart.length;
  well.arrayLevel = well.arrays.map(array => Number(array.split("/").pop()));
  well.arrayRound = well.arrays.map(array => Number(array.split("/")[2]));
  view = [0, well.size];
  hoveredChunk = -1;
  hoveredCell = null;
  selectedRun = -1;
  document.getElementById("well-title").textContent =
    `Well ${name}: ${formatBytes(well.size)}, ${well.chunkCount.toLocaleString()} chunks, ${well.fields.length} fields`;
  buildRunTable();
  redraw();
}

function chunkName(index) {
  const array = well.arrays[well.chunkArray[index]];
  const position = `${well.chunkZ[index]}/${well.chunkRow[index]}/${well.chunkColumn[index]}`;
  return well.chunkChannel[index] >= 0 ? `${array}/${well.chunkChannel[index]}/${position}` : `${array}/${position}`;
}

function chunkLevel(index) {
  return well.arrayLevel[well.chunkArray[index]];
}

function isMetadataRun(run) {
  return run.kind === "meta" || run.kind === "table";
}

function cellsOfField(field, level) {
  const [rowStart, rowEnd, columnStart, columnEnd] = field.windows[level];
  const cells = [];
  for (let row = Math.floor(rowStart / CHUNK_PIXELS); row <= Math.floor((rowEnd - 1) / CHUNK_PIXELS); row++) {
    for (let column = Math.floor(columnStart / CHUNK_PIXELS); column <= Math.floor((columnEnd - 1) / CHUNK_PIXELS); column++) {
      cells.push([row, column]);
    }
  }
  return cells;
}

function fieldsInCell(level, row, column) {
  return well.fields
    .filter(field => cellsOfField(field, level).some(([fieldRow, fieldColumn]) => fieldRow === row && fieldColumn === column))
    .map(field => field.name);
}

// ---------------------------------------------------------------------------
// Chunk colors and the legend
// ---------------------------------------------------------------------------

// Level 0 is drawn darkest, because it holds most of the bytes.
function levelColor(level) {
  return rampColor((3 - level) / 3 * 0.85 + 0.15);
}

function chunkColor(index, mode) {
  const flags = well.chunkFlags[index];
  if (mode === "status") {
    if (flags & FLAG_EMPTY) return colors.empty;
    if (flags & FLAG_PADDING) return colors.padding;
    return flags & FLAG_TO_DECODE ? colors.contents[0] : colors.untouched;
  }
  if (flags & FLAG_EMPTY) return colors.empty;
  const arrayIndex = well.chunkArray[index];
  if (mode === "contents") return colors.contents[well.arrayKinds[arrayIndex] === "label" ? LABELS_SLOT : well.arrayRound[arrayIndex]];
  if (mode === "level") return levelColor(well.arrayLevel[arrayIndex]);
  if (mode === "channel") return well.chunkChannel[index] < 0 ? colors.muted : colors.contents[well.chunkChannel[index]];
  if (mode === "z") return rampColor(well.chunkZ[index] / 27);
  return colors.muted;
}

function buildLegend() {
  const mode = document.getElementById("color-by").value;
  const legend = document.getElementById("legend");
  legend.replaceChildren();
  function addItem(color, text, className) {
    const item = document.createElement("span");
    const swatch = document.createElement("i");
    swatch.style.background = color;
    if (className) swatch.className = className;
    item.append(swatch, text);
    legend.appendChild(item);
  }
  if (mode === "contents") CONTENTS_LABELS.forEach((text, slot) => addItem(colors.contents[slot], text));
  if (mode === "level") [0, 1, 2, 3].forEach(level => addItem(levelColor(level), "level " + level));
  if (mode === "channel") {
    [0, 1, 2, 3].forEach(channel => addItem(colors.contents[channel], "channel " + channel));
    addItem(colors.muted, "labels (no channel)");
  }
  if (mode === "z") addItem(`linear-gradient(90deg, ${colors.rampLow}, ${colors.rampHigh})`, "z index 0 → 27", "ramp");
  if (mode === "status") {
    addItem(colors.contents[0], "to decode: a field overlaps its cell");
    addItem(colors.untouched, "stored, no field overlaps it");
    addItem(colors.padding, "all-zero padding frame");
  }
  addItem(colors.empty, mode === "status" ? "empty file" : "empty file (shown in every mode)");
}

// ---------------------------------------------------------------------------
// Panel: the whole zip
// ---------------------------------------------------------------------------

const zipCanvas = document.getElementById("zip-canvas");

function zipOffsetAt(event) {
  const box = zipCanvas.getBoundingClientRect();
  return (event.clientX - box.left) / box.width * PAGE.zip.size;
}

function wellAtOffset(offset) {
  return PAGE.zip.wells.find(candidate => offset >= candidate.start && offset < candidate.end);
}

function drawZip() {
  const { context, width } = prepareCanvas(zipCanvas);
  const toX = offset => offset / PAGE.zip.size * width;
  context.font = "11px system-ui";
  for (const zipWell of PAGE.zip.wells) {
    const left = toX(zipWell.start), right = toX(zipWell.end);
    const isShown = well && zipWell.well === well.name;
    context.fillStyle = zipWell.embedded ? (isShown ? colors.axis : colors.gridline) : colors.surface;
    context.fillRect(left, 0, right - left - 1, 30);
    context.strokeStyle = colors.axis;
    context.strokeRect(left + 0.5, 0.5, right - left - 2, 29);
    context.fillStyle = zipWell.embedded ? colors.ink : colors.muted;
    context.textAlign = "center";
    if (right - left > 26) context.fillText(zipWell.well, (left + right) / 2, 19);
  }
  for (const [start, end, slot] of PAGE.zip.runs) {
    context.fillStyle = slot < 0 ? colors.muted : colors.contents[slot];
    context.fillRect(toX(start), 34, Math.max(toX(end) - toX(start), 0.6), 12);
  }
  context.fillStyle = colors.ink;
  for (const [, offset] of PAGE.zip.plate_members) context.fillRect(toX(offset) - 0.5, 48, 1.5, 10);
  const directoryX = toX(PAGE.directory.offset);
  context.fillRect(directoryX - 2, 0, 3, 46);
  context.font = "600 11px system-ui";
  context.textAlign = "right";
  context.fillText(`central directory, ${formatBytes(PAGE.directory.size)} ▸`, directoryX - 6, 68);
  context.fillStyle = colors.muted;
  context.font = "11px system-ui";
  context.textAlign = "left";
  context.fillText(`▲ plate-level metadata (.zattrs, .zgroup) sits between wells · the file is ${formatBytes(PAGE.zip.size)}`, 2, 68);
  if (well) {
    const left = toX(well.start + view[0]), right = toX(well.start + view[1]);
    context.strokeStyle = colors.highlight;
    context.lineWidth = 2;
    context.strokeRect(left, 32, Math.max(right - left, 2), 16);
    context.lineWidth = 1;
  }
}

zipCanvas.addEventListener("pointermove", event => {
  const offset = zipOffsetAt(event);
  const zipWell = wellAtOffset(offset);
  zipCanvas.style.cursor = zipWell && zipWell.embedded ? "pointer" : "default";
  if (zipWell) {
    showTooltip(event, [
      [null, zipWell.well, true],
      ["bytes", `${formatBytes(zipWell.start)} – ${formatBytes(zipWell.end)} (${formatBytes(zipWell.end - zipWell.start)})`],
      ["members", zipWell.members.toLocaleString()],
      ["fields", zipWell.fields == null ? "not fetched" : String(zipWell.fields)],
      [null, zipWell.embedded ? "click to open" : "not embedded: regenerate with --wells " + zipWell.well],
    ]);
  } else if (offset >= PAGE.directory.offset - PAGE.zip.size * 0.004) {
    // The directory is 0.03% of the file, so it gets a few pixels of slack.
    showTooltip(event, [[null, "central directory", true], ["at", formatBytes(PAGE.directory.offset)],
      ["size", formatBytes(PAGE.directory.size)], [null, "see the panel below"]]);
  } else {
    hideTooltip();
  }
});
zipCanvas.addEventListener("pointerleave", hideTooltip);
zipCanvas.addEventListener("click", event => {
  const zipWell = wellAtOffset(zipOffsetAt(event));
  if (zipWell && zipWell.embedded) loadWell(zipWell.well);
});

// ---------------------------------------------------------------------------
// Panel: the central directory
// ---------------------------------------------------------------------------

const directoryCanvas = document.getElementById("directory-canvas");

function drawDirectory() {
  const { context, width } = prepareCanvas(directoryCanvas);
  const directory = PAGE.directory;
  const toX = offset => (offset - directory.offset) / directory.size * width;
  directory.by_well.forEach((span, index) => {
    const left = toX(span.start), right = toX(span.end);
    const isPlate = span.well === "plate";
    context.fillStyle = isPlate ? colors.ink : index % 2 ? colors.axis : colors.gridline;
    context.fillRect(left, 0, Math.max(right - left - (right - left > 3 ? 1 : 0), isPlate ? 1.5 : 0.8), 30);
    if (!isPlate && right - left > 30) {
      context.fillStyle = colors.ink;
      context.font = "11px system-ui";
      context.textAlign = "center";
      context.fillText(span.well, (left + right) / 2, 19);
    }
  });
  context.fillStyle = colors.muted;
  context.font = "11px system-ui";
  context.textAlign = "left";
  context.fillText(`starts at byte ${directory.offset.toLocaleString()} (${formatBytes(directory.offset)})`, 0, 46);
  context.textAlign = "right";
  const end = directory.offset + directory.size;
  context.fillText(`ends at ${end.toLocaleString()}; the end records follow`, width, 46);
}

directoryCanvas.addEventListener("pointermove", event => {
  const directory = PAGE.directory;
  const box = directoryCanvas.getBoundingClientRect();
  const offset = directory.offset + (event.clientX - box.left) / box.width * directory.size;
  const span = directory.by_well.find(candidate => offset >= candidate.start && offset < candidate.end)
    || directory.by_well[directory.by_well.length - 1];
  const bytes = span.end - span.start;
  showTooltip(event, [
    [null, span.well === "plate" ? "plate-level entries" : "entries for well " + span.well, true],
    ["entries", span.entries.toLocaleString()],
    ["bytes", formatBytes(bytes)],
    ["average entry", (bytes / span.entries).toFixed(1) + " bytes"],
    ["at", `${span.start.toLocaleString()} – ${span.end.toLocaleString()}`],
  ]);
});
directoryCanvas.addEventListener("pointerleave", hideTooltip);

function renderEndOfFile() {
  const directory = PAGE.directory;
  renderParts(document.getElementById("end-of-file-key"), [
    ["… members …", `the last well ends where the directory starts, at ${formatBytes(directory.offset)}`],
    ["central directory", `${formatBytes(directory.size)}, ${directory.entries.toLocaleString()} entries`],
    ["ZIP64 end record", "56 bytes: entry count, offset, size"],
    ["ZIP64 locator", "20 bytes: where the ZIP64 record is"],
    ["end record", "22 bytes: the last bytes of the file"],
  ], "→");
}

// ---------------------------------------------------------------------------
// Panel: one well's bytes
// ---------------------------------------------------------------------------

const wellCanvas = document.getElementById("well-canvas");
// Vertical positions of the panel's lanes, in CSS pixels.
const LANES = { axisBottom: 18, runTop: 24, runHeight: 34, chunkTop: 74, chunkHeight: 70, markTop: 148, markHeight: 8, captionTop: 172 };
// The smallest byte span the panel zooms to: a few small members.
const MINIMUM_VIEW_BYTES = 2000;

function offsetToX(offset, width) {
  return (offset - view[0]) / (view[1] - view[0]) * width;
}

function offsetAtPointer(event) {
  const box = wellCanvas.getBoundingClientRect();
  return view[0] + (event.clientX - box.left) / box.width * (view[1] - view[0]);
}

// Chunks are sorted by start and tile the well's bytes, so a binary search
// finds the chunk that contains a byte exactly. Hit testing by byte rather
// than by drawn pixel is what makes a chunk narrower than a pixel hoverable.
function firstChunkEndingAfter(offset) {
  let low = 0, high = well.chunkCount;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (well.chunkStart[middle] + well.chunkBytes[middle] < offset) low = middle + 1;
    else high = middle;
  }
  return low;
}

function chunkAtOffset(offset) {
  const index = firstChunkEndingAfter(offset);
  return index < well.chunkCount && well.chunkStart[index] <= offset ? index : -1;
}

// A metadata run is a few hundred bytes, far less than a pixel when the
// whole well is shown, so a pointer within a few pixels of one picks it
// before the array run it touches.
function runAtOffset(offset, width) {
  const bytesPerPixel = (view[1] - view[0]) / width;
  const slack = 4 * bytesPerPixel;
  return well.runs.find(run => isMetadataRun(run) && offset >= run.start - slack && offset <= run.end + slack)
    || well.runs.find(run => !isMetadataRun(run) && offset >= run.start && offset <= run.end);
}

function runColor(run) {
  if (run.kind === "image") return colors.contents[run.round];
  return run.kind === "label" ? colors.contents[LABELS_SLOT] : colors.muted;
}

function runDescription(run) {
  if (run.kind === "image") return `round ${run.round} · level ${run.level}`;
  if (run.kind === "label") return `${run.array.split("/")[4]} · level ${run.level}`;
  return run.kind;
}

function drawAxis(context, width) {
  const span = view[1] - view[0];
  const roughStep = span / 8;
  const magnitude = Math.pow(10, Math.floor(Math.log10(roughStep)));
  const step = [1, 2, 5, 10].map(multiple => multiple * magnitude).find(candidate => candidate >= roughStep);
  context.font = "11px system-ui";
  context.fillStyle = colors.muted;
  context.strokeStyle = colors.gridline;
  context.textAlign = "center";
  for (let tick = Math.ceil(view[0] / step) * step; tick <= view[1]; tick += step) {
    const x = offsetToX(tick, width);
    context.beginPath();
    context.moveTo(x + 0.5, LANES.axisBottom);
    context.lineTo(x + 0.5, LANES.markTop + LANES.markHeight);
    context.stroke();
    context.fillText(formatTick(tick, step), x, 11);
  }
}

function drawRuns(context, width) {
  well.runs.forEach((run, index) => {
    if (run.end < view[0] || run.start > view[1]) return;
    const left = offsetToX(run.start, width), right = offsetToX(run.end, width);
    const metadata = isMetadataRun(run);
    // Metadata runs are drawn taller than array runs so they stay visible as ticks.
    const drawnWidth = Math.max(right - left - (right - left > 4 ? 2 : 0), metadata ? 1.5 : 0.8);
    context.fillStyle = runColor(run);
    context.globalAlpha = selectedRun >= 0 && selectedRun !== index ? 0.45 : 1;
    if (metadata) context.fillRect(left, LANES.runTop - 4, drawnWidth, LANES.runHeight + 8);
    else context.fillRect(left, LANES.runTop, drawnWidth, LANES.runHeight);
    context.globalAlpha = 1;
    if (!metadata && drawnWidth > 70) {
      context.save();
      context.beginPath();
      context.rect(Math.max(left, 0), LANES.runTop, Math.min(drawnWidth, width), LANES.runHeight);
      context.clip();
      // Round 3's yellow needs dark text; the other slots take white.
      context.fillStyle = run.kind === "image" && run.round === 3 ? "#0b0b0b" : "#ffffff";
      context.textAlign = "left";
      context.font = "600 11px system-ui";
      context.fillText(`${runDescription(run)} · ${formatBytes(run.end - run.start)}`, Math.max(left, 0) + 6, LANES.runTop + 21);
      context.restore();
    }
  });
}

function drawChunks(context, width) {
  const mode = document.getElementById("color-by").value;
  let shownCount = 0, shownBytes = 0;
  for (let index = firstChunkEndingAfter(view[0]); index < well.chunkCount && well.chunkStart[index] <= view[1]; index++) {
    const left = offsetToX(well.chunkStart[index], width);
    const right = offsetToX(well.chunkStart[index] + well.chunkBytes[index], width);
    // A one-pixel gap between chunks wide enough to show it, so neighbors stay distinct.
    const gap = right - left > 5 ? 1 : 0;
    context.fillStyle = chunkColor(index, mode);
    context.fillRect(left, LANES.chunkTop, Math.max(right - left - gap, 0.7), LANES.chunkHeight);
    shownCount++;
    shownBytes += well.chunkBytes[index];
    if (hoveredCell && chunkLevel(index) === hoveredCell[0]
        && well.chunkRow[index] === hoveredCell[1] && well.chunkColumn[index] === hoveredCell[2]) {
      context.fillStyle = colors.highlight;
      context.fillRect(left, LANES.markTop, Math.max(right - left, 1.5), LANES.markHeight);
    }
  }
  if (hoveredChunk >= 0) {
    const left = offsetToX(well.chunkStart[hoveredChunk], width);
    const right = offsetToX(well.chunkStart[hoveredChunk] + well.chunkBytes[hoveredChunk], width);
    context.strokeStyle = colors.highlight;
    context.lineWidth = 2;
    context.strokeRect(left - 1, LANES.chunkTop - 1, Math.max(right - left, 1) + 2, LANES.chunkHeight + 2);
    context.lineWidth = 1;
  }
  return { shownCount, shownBytes };
}

function drawWell() {
  const { context, width } = prepareCanvas(wellCanvas);
  drawAxis(context, width);
  drawRuns(context, width);
  const { shownCount, shownBytes } = drawChunks(context, width);
  context.fillStyle = colors.muted;
  context.font = "11px system-ui";
  context.textAlign = "left";
  context.fillText("runs: one array's chunks each · gray ticks are metadata (.zattrs, .zarray, .zgroup) and AnnData tables",
    0, LANES.runTop + LANES.runHeight + 13);
  context.fillText(hoveredCell
    ? `chunks of grid cell (${hoveredCell[1]}, ${hoveredCell[2]}) at level ${hoveredCell[0]}, every round`
    : "chunks, each as wide as its bytes in the zip: local header, data and data descriptor", 0, LANES.captionTop + 8);
  document.getElementById("view-status").textContent =
    `in view: ${formatBytes(view[0])} – ${formatBytes(view[1])} · ${shownCount.toLocaleString()} chunks · ${formatBytes(shownBytes)}`;
}

function showRunTooltip(event, run) {
  const rows = [[null, formatBytes(run.end - run.start), true], [null, run.array || "/"]];
  if (isMetadataRun(run)) {
    rows.push(["kind", run.kind === "meta" ? "Zarr metadata documents" : "AnnData table members"]);
    const documents = well.metadataMembers.filter(([, start]) => start >= run.start && start < run.end).map(([name]) => name);
    documents.slice(0, 6).forEach(name => rows.push(["", name]));
    if (documents.length > 6) rows.push(["", `… ${documents.length - 6} more`]);
  } else {
    rows.push(["kind", run.kind + " chunks"]);
  }
  rows.push(["starts at", formatBytes(run.start) + " into the well"], ["members", run.members.toLocaleString()],
    ["empty / padding", `${run.empty} / ${run.padding}`]);
  showTooltip(event, rows);
}

function showChunkTooltip(event, index) {
  const flags = well.chunkFlags[index];
  const status = flags & FLAG_EMPTY ? "an empty file: no bytes, reads as 0"
    : flags & FLAG_PADDING ? "an all-zero padding frame"
    : flags & FLAG_TO_DECODE ? "to decode: a field overlaps its cell" : "no field overlaps its cell";
  const fields = fieldsInCell(chunkLevel(index), well.chunkRow[index], well.chunkColumn[index]);
  const nameRows = (explainName(chunkName(index)) || []).slice(1).map(([part, meaning]) => [part.padEnd(4, " "), meaning]);
  showTooltip(event, [
    [null, formatBytes(well.chunkCompressed[index]) + " compressed", true], [null, chunkName(index)], ...nameRows, ["", ""],
    ["status", status], ["Blosc frame", formatBytes(well.chunkFrame[index])],
    ["at", formatBytes(well.chunkStart[index]) + " into the well"],
    ["fields using this cell", fields.length ? fields.join(", ") : "none"],
  ]);
}

function zoomToRun(index) {
  const run = well.runs[index];
  const margin = Math.max((run.end - run.start) * 0.03, 500);
  view = [Math.max(run.start - margin, 0), Math.min(run.end + margin, well.size)];
  selectedRun = index;
  document.querySelectorAll("tr.run").forEach(row => row.classList.toggle("active", Number(row.dataset.run) === index));
  redraw();
}

function fitWell() {
  view = [0, well.size];
  selectedRun = -1;
  redraw();
}

function pointerOnRuns(event) {
  const y = event.clientY - wellCanvas.getBoundingClientRect().top;
  return y >= LANES.runTop - 4 && y <= LANES.runTop + LANES.runHeight + 4;
}

function pointerOnChunks(event) {
  const y = event.clientY - wellCanvas.getBoundingClientRect().top;
  return y >= LANES.chunkTop - 4 && y <= LANES.markTop + LANES.markHeight;
}

let drag = null;  // { startX, startView, moved } while the pointer is down

wellCanvas.addEventListener("wheel", event => {
  event.preventDefault();
  const box = wellCanvas.getBoundingClientRect();
  const fraction = (event.clientX - box.left) / box.width;
  const anchor = view[0] + fraction * (view[1] - view[0]);
  const span = Math.min(Math.max((view[1] - view[0]) * Math.exp(event.deltaY * 0.0018), MINIMUM_VIEW_BYTES), well.size);
  const start = Math.min(Math.max(anchor - fraction * span, 0), well.size - span);
  view = [start, start + span];
  redraw();
}, { passive: false });

wellCanvas.addEventListener("pointerdown", event => {
  drag = { startX: event.clientX, startView: view.slice(), moved: false };
  wellCanvas.setPointerCapture(event.pointerId);
  wellCanvas.classList.add("dragging");
});

wellCanvas.addEventListener("pointerup", event => {
  wellCanvas.classList.remove("dragging");
  if (drag && !drag.moved && pointerOnRuns(event)) {
    const offset = offsetAtPointer(event);
    const index = well.runs.findIndex(run => !isMetadataRun(run) && offset >= run.start && offset <= run.end);
    if (index >= 0) zoomToRun(index);
  }
  drag = null;
});

wellCanvas.addEventListener("dblclick", fitWell);

wellCanvas.addEventListener("pointermove", event => {
  const box = wellCanvas.getBoundingClientRect();
  if (drag) {
    const span = drag.startView[1] - drag.startView[0];
    if (Math.abs(event.clientX - drag.startX) > 3) drag.moved = true;
    const shift = (event.clientX - drag.startX) / box.width * span;
    const start = Math.min(Math.max(drag.startView[0] - shift, 0), well.size - span);
    view = [start, start + span];
    hideTooltip();
    redraw();
    return;
  }
  const offset = offsetAtPointer(event);
  if (pointerOnRuns(event)) {
    const run = runAtOffset(offset, box.width);
    if (run) showRunTooltip(event, run);
    else hideTooltip();
    if (hoveredChunk !== -1) {
      hoveredChunk = -1;
      redraw();
    }
    return;
  }
  if (pointerOnChunks(event)) {
    const index = chunkAtOffset(offset);
    if (index !== hoveredChunk) {
      hoveredChunk = index;
      if (index >= 0) document.getElementById("grid-level").value = chunkLevel(index);
      redraw();
    }
    if (index >= 0) showChunkTooltip(event, index);
    else hideTooltip();
    return;
  }
  hideTooltip();
});

wellCanvas.addEventListener("pointerleave", () => {
  hideTooltip();
  if (hoveredChunk !== -1) {
    hoveredChunk = -1;
    redraw();
  }
});

document.getElementById("fit-well").addEventListener("click", fitWell);
document.getElementById("color-by").addEventListener("change", () => {
  buildLegend();
  redraw();
});

// ---------------------------------------------------------------------------
// Panel: the well's chunk grid
// ---------------------------------------------------------------------------

const gridCanvas = document.getElementById("grid-canvas");
let gridGeometry = null;  // { cellSize, level, rows, columns } from the last draw, for hit testing

function drawGrid() {
  const { context, width, height } = prepareCanvas(gridCanvas);
  const level = Number(document.getElementById("grid-level").value);
  const [rows, columns] = well.grid[level];
  const cellSize = Math.min((width - 2) / columns, (height - 2) / rows);
  const pixelScale = cellSize / CHUNK_PIXELS;
  gridGeometry = { cellSize, level, rows, columns };

  const stored = new Set(), toDecode = new Set();
  for (let index = 0; index < well.chunkCount; index++) {
    if (chunkLevel(index) !== level) continue;
    const cell = well.chunkRow[index] * CHUNK_PIXELS + well.chunkColumn[index];
    stored.add(cell);
    if (well.chunkFlags[index] & FLAG_TO_DECODE) toDecode.add(cell);
  }
  for (let row = 0; row < rows; row++) {
    for (let column = 0; column < columns; column++) {
      const cell = row * CHUNK_PIXELS + column;
      const left = column * cellSize, top = row * cellSize;
      if (toDecode.has(cell)) {
        context.fillStyle = colors.contents[0];
        context.globalAlpha = 0.35;
        context.fillRect(left + 1, top + 1, cellSize - 2, cellSize - 2);
        context.globalAlpha = 1;
      } else if (stored.has(cell)) {
        context.fillStyle = colors.padding;
        context.fillRect(left + 1, top + 1, cellSize - 2, cellSize - 2);
      } else {
        context.strokeStyle = colors.gridline;
        context.strokeRect(left + 1.5, top + 1.5, cellSize - 3, cellSize - 3);
      }
    }
  }
  context.font = "600 11px system-ui";
  context.textAlign = "left";
  for (const field of well.fields) {
    const [rowStart, rowEnd, columnStart, columnEnd] = field.windows[level];
    context.strokeStyle = colors.ink;
    context.lineWidth = 1.5;
    context.strokeRect(columnStart * pixelScale + 1, rowStart * pixelScale + 1,
      (columnEnd - columnStart) * pixelScale, (rowEnd - rowStart) * pixelScale);
    context.lineWidth = 1;
    context.fillStyle = colors.ink;
    context.fillText(field.name, columnStart * pixelScale + 4, rowStart * pixelScale + 13);
  }
  let marked = null;
  if (hoveredChunk >= 0 && chunkLevel(hoveredChunk) === level) marked = [well.chunkRow[hoveredChunk], well.chunkColumn[hoveredChunk]];
  else if (hoveredCell && hoveredCell[0] === level) marked = [hoveredCell[1], hoveredCell[2]];
  if (marked) {
    context.strokeStyle = colors.highlight;
    context.lineWidth = 3;
    context.strokeRect(marked[1] * cellSize + 1.5, marked[0] * cellSize + 1.5, cellSize - 3, cellSize - 3);
    context.lineWidth = 1;
  }
  document.getElementById("grid-title").textContent = `Grid cells at level ${level}: ${rows} × ${columns}`;
}

gridCanvas.addEventListener("pointermove", event => {
  if (!gridGeometry) return;
  const box = gridCanvas.getBoundingClientRect();
  const column = Math.floor((event.clientX - box.left) / gridGeometry.cellSize);
  const row = Math.floor((event.clientY - box.top) / gridGeometry.cellSize);
  const inside = column >= 0 && row >= 0 && column < gridGeometry.columns && row < gridGeometry.rows;
  if (!inside) {
    if (hoveredCell) {
      hoveredCell = null;
      redraw();
    }
    hideTooltip();
    return;
  }
  const level = gridGeometry.level;
  const unchanged = hoveredCell && hoveredCell[0] === level && hoveredCell[1] === row && hoveredCell[2] === column;
  if (!unchanged) {
    hoveredCell = [level, row, column];
    redraw();
  }
  let count = 0, bytes = 0, first = Infinity, last = -1;
  for (let index = 0; index < well.chunkCount; index++) {
    if (chunkLevel(index) !== level || well.chunkRow[index] !== row || well.chunkColumn[index] !== column) continue;
    count++;
    bytes += well.chunkBytes[index];
    first = Math.min(first, well.chunkStart[index]);
    last = Math.max(last, well.chunkStart[index]);
  }
  const fields = fieldsInCell(level, row, column);
  showTooltip(event, [
    [null, `cell (${row}, ${column}) at level ${level}`, true],
    ["stored chunks", count.toLocaleString()],
    ["bytes", formatBytes(bytes)],
    ["spread", count ? `${formatBytes(first)} – ${formatBytes(last)} into the well` : "—"],
    ["fields", fields.length ? fields.join(", ") : "none"],
    [null, "marked in black under the chunks above"],
  ]);
});

gridCanvas.addEventListener("pointerleave", () => {
  hideTooltip();
  if (hoveredCell) {
    hoveredCell = null;
    redraw();
  }
});
document.getElementById("grid-level").addEventListener("change", redraw);

// ---------------------------------------------------------------------------
// The runs table
// ---------------------------------------------------------------------------

function buildRunTable() {
  const body = document.getElementById("run-table").tBodies[0];
  body.replaceChildren();
  well.runs.forEach((run, index) => {
    const row = document.createElement("tr");
    row.className = isMetadataRun(run) ? "run metadata" : "run";
    row.dataset.run = index;
    const values = [index + 1, formatBytes(run.start), formatBytes(run.end - run.start),
      run.members.toLocaleString(), run.empty || "", run.padding || ""];
    for (const value of values) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.appendChild(cell);
    }
    const what = document.createElement("td");
    what.className = "what";
    const swatch = document.createElement("span");
    swatch.className = "swatch";
    swatch.style.background = runColor(run);
    what.append(swatch, isMetadataRun(run) ? `${run.kind}: ${run.array || "/"}` : `${run.array}  (${runDescription(run)})`);
    row.appendChild(what);
    row.addEventListener("click", () => zoomToRun(index));
    body.appendChild(row);
  });
}

// ---------------------------------------------------------------------------
// The name decoder
// ---------------------------------------------------------------------------

document.getElementById("decode-input").addEventListener("keydown", event => {
  if (event.key !== "Enter") return;
  const name = event.target.value.trim();
  const result = document.getElementById("decode-result");
  renderName(document.getElementById("decoded-key"), name);
  const wellName = name.split("/").slice(0, 2).join("/");
  if (!PAGE.wells[wellName]) {
    const known = PAGE.zip.wells.some(zipWell => zipWell.well === wellName);
    result.textContent = known ? `well ${wellName} is not embedded; regenerate with --wells ${wellName}` : "not a well in this plate";
    return;
  }
  if (!well || well.name !== wellName) loadWell(wellName);
  let found = -1;
  for (let index = 0; index < well.chunkCount; index++) {
    if (chunkName(index) === name) {
      found = index;
      break;
    }
  }
  if (found < 0) {
    result.textContent = "decoded above; no chunk has this name (it is metadata, a table, or a chunk the source never stored)";
    return;
  }
  const margin = Math.max(well.chunkBytes[found] * 20, 2e6);
  view = [Math.max(well.chunkStart[found] - margin, 0), Math.min(well.chunkStart[found] + well.chunkBytes[found] + margin, well.size)];
  hoveredChunk = found;
  document.getElementById("grid-level").value = chunkLevel(found);
  result.textContent = `found at ${formatBytes(well.chunkStart[found])} into the well, ${formatBytes(well.chunkCompressed[found])} compressed; outlined below`;
  redraw();
});

// ---------------------------------------------------------------------------
// Drawing everything, and the first view
// ---------------------------------------------------------------------------

// Every interaction redraws all four canvases on the next frame; they are
// cheap, and the panels share the hovered chunk and cell.
function redraw() {
  if (!well) return;
  requestAnimationFrame(() => {
    drawZip();
    drawDirectory();
    drawWell();
    drawGrid();
  });
}

addEventListener("resize", redraw);
renderEndOfFile();
renderName(document.getElementById("image-key"), "C/02/2/0/1/13/0/7");
renderName(document.getElementById("label-key"), "C/02/1/labels/cells/0/26/0/7");
buildLegend();
loadWell(PAGE.first_well);
