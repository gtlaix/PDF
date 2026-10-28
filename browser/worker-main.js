// Compression worker for the browser version. build.sh prepends Ghostscript's
// Emscripten loader (defines `Module`) and pdf-lib (defines `PDFLib`).
// Mirrors app/compressor.py; keep the two in step.

const READABLE_LADDER = [
  { dpi: 300, qfactor: 0.40, label: "High (300 DPI)" },
  { dpi: 250, qfactor: 0.50, label: "High (250 DPI)" },
  { dpi: 200, qfactor: 0.60, label: "Good (200 DPI)" },
  { dpi: 150, qfactor: 0.76, label: "Good (150 DPI)" },
  { dpi: 125, qfactor: 0.90, label: "Medium (125 DPI)" },
  { dpi: 100, qfactor: 1.00, label: "Medium (100 DPI)" },
];
const LAST_RESORT_LADDER = [
  { dpi: 85, qfactor: 1.30, label: "Low (85 DPI)" },
  { dpi: 72, qfactor: 1.50, label: "Low (72 DPI)" },
  { dpi: 60, qfactor: 1.80, label: "Very low (60 DPI)" },
  { dpi: 50, qfactor: 2.20, label: "Very low (50 DPI)" },
];
// Parts are packed to this fraction of the target, leaving headroom for the
// shared resources (fonts etc.) that get duplicated into every part.
const PACKING_HEADROOM = 0.92;

const createGhostscript = Module;
let wasmModule;

async function loadEngine() {
  if (!wasmModule) {
    wasmModule = (async () => {
      // The engine is published gzipped (to fit the host's per-file limit)
      // under a .wasm name, as .gz isn't served. Unpack it here unless the
      // host already did.
      const res = await fetch("gs-gzip.wasm");
      if (!res.ok) throw new Error(`Couldn't load the compression engine (HTTP ${res.status}).`);
      let bytes = new Uint8Array(await res.arrayBuffer());
      if (bytes[0] === 0x1f && bytes[1] === 0x8b) {
        const unpacked = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
        bytes = new Uint8Array(await new Response(unpacked).arrayBuffer());
      }
      return WebAssembly.compile(bytes);
    })();
    wasmModule.catch(() => { wasmModule = undefined; });
  }
  return wasmModule;
}

// Each run gets a fresh Ghostscript instance; the compiled module is reused so
// this is cheap, and memory from the previous run is released.
async function ghostscriptCompress(input, q) {
  const mod = await loadEngine();
  let log = "";
  const gs = await createGhostscript({
    instantiateWasm(imports, done) {
      WebAssembly.instantiate(mod, imports).then((inst) => done(inst));
      return {};
    },
    print: (s) => { log += s + "\n"; },
    printErr: (s) => { log += s + "\n"; },
  });
  gs.FS.writeFile("/in.pdf", input);
  const monoDpi = Math.max(q.dpi * 2, 300); // 1-bit scans compress well; keep them crisp
  const imageDict = `<< /QFactor ${q.qfactor} /Blend 1 /HSamples [2 1 1 2] /VSamples [2 1 1 2] >>`;
  let rc;
  try {
    rc = gs.callMain([
      "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.5",
      "-dNOPAUSE", "-dBATCH", "-dQUIET", "-dSAFER",
      "-dDetectDuplicateImages=true", "-dCompressFonts=true", "-dSubsetFonts=true",
      "-dPassThroughJPEGImages=false", "-dPassThroughJPXImages=false",
      "-dDownsampleColorImages=true", "-dColorImageDownsampleType=/Bicubic",
      `-dColorImageResolution=${q.dpi}`, "-dColorImageDownsampleThreshold=1.0",
      "-dAutoFilterColorImages=false", "-dColorImageFilter=/DCTEncode",
      "-dDownsampleGrayImages=true", "-dGrayImageDownsampleType=/Bicubic",
      `-dGrayImageResolution=${q.dpi}`, "-dGrayImageDownsampleThreshold=1.0",
      "-dAutoFilterGrayImages=false", "-dGrayImageFilter=/DCTEncode",
      "-dDownsampleMonoImages=true", "-dMonoImageDownsampleType=/Subsample",
      `-dMonoImageResolution=${monoDpi}`, "-dMonoImageDownsampleThreshold=1.0",
      "-sOutputFile=/out.pdf",
      "-c",
      `<< /ColorACSImageDict ${imageDict} /GrayACSImageDict ${imageDict} ` +
        `/ColorImageDict ${imageDict} /GrayImageDict ${imageDict} >> setdistillerparams`,
      "-f", "/in.pdf",
    ]);
  } catch (e) {
    rc = e && typeof e.status === "number" ? e.status : -1;
    if (rc === -1) log += String(e);
  }
  let out;
  try { out = gs.FS.readFile("/out.pdf"); } catch {}
  if (rc !== 0 || !out || !out.length) {
    throw new Error(`Ghostscript failed: ${log.trim().slice(0, 500) || "unknown error"}`);
  }
  return out;
}

async function pageSizes(doc) {
  // Approximate the byte cost of each page by writing it on its own.
  const sizes = [];
  for (let i = 0; i < doc.getPageCount(); i++) {
    const one = await PDFLib.PDFDocument.create();
    const [page] = await one.copyPages(doc, [i]);
    one.addPage(page);
    sizes.push((await one.save()).length);
  }
  return sizes;
}

function partition(sizes, capacity, maxParts) {
  // Greedily pack consecutive pages into at most maxParts chunks.
  const chunks = [];
  let start = 0, used = 0;
  for (let i = 0; i < sizes.length; i++) {
    if (sizes[i] > capacity) return null; // a single page can't fit
    if (used + sizes[i] > capacity && i > start) {
      chunks.push([start, i]);
      start = i;
      used = 0;
    }
    used += sizes[i];
  }
  chunks.push([start, sizes.length]);
  return chunks.length <= maxParts ? chunks : null;
}

async function writeParts(doc, chunks, stem) {
  const parts = [];
  for (let n = 0; n < chunks.length; n++) {
    const [from, to] = chunks[n];
    const out = await PDFLib.PDFDocument.create();
    const indices = Array.from({ length: to - from }, (_, k) => from + k);
    for (const page of await out.copyPages(doc, indices)) out.addPage(page);
    parts.push({ name: `${stem} - Part ${n + 1} of ${chunks.length}.pdf`, bytes: await out.save() });
  }
  return parts;
}

async function trySplit(compressed, target, maxParts, stem) {
  const size = compressed.length;
  if (Math.ceil(size / (target * PACKING_HEADROOM)) > maxParts) return null; // clearly can't fit
  const doc = await PDFLib.PDFDocument.load(compressed, { ignoreEncryption: true, updateMetadata: false });
  if (doc.getPageCount() < 2) return null;
  const sizes = await pageSizes(doc);
  // Per-page sizes include duplicated fonts, so scale them to the real total.
  const scale = size / Math.max(sizes.reduce((a, b) => a + b, 0), 1);
  const scaled = sizes.map((s) => s * scale);
  for (const headroom of [PACKING_HEADROOM, 0.80, 0.65]) {
    let chunks = partition(scaled, target * headroom, maxParts);
    if (!chunks) continue;
    if (chunks.length === 1) chunks = partition(scaled, size / 2 + 1, maxParts) || chunks;
    const parts = await writeParts(doc, chunks, stem);
    if (parts.every((p) => p.bytes.length <= target)) return parts;
  }
  return null;
}

async function compressPdf(input, target, stem, maxParts, progress) {
  const result = { status: "compressed", originalSize: input.length, parts: [], quality: null, warnings: [] };

  if (input.length <= target) {
    result.status = "unchanged";
    result.parts = [{ name: `${stem}.pdf`, bytes: input }];
    result.quality = "Original (already under target)";
    return result;
  }

  const cache = new Map();
  const run = async (q) => {
    if (!cache.has(q.dpi)) {
      progress(`Trying ${q.dpi} DPI…`);
      cache.set(q.dpi, await ghostscriptCompress(input, q));
    }
    return cache.get(q.dpi);
  };
  const single = (q, bytes) => {
    result.parts = [{ name: `${stem}.pdf`, bytes }];
    result.quality = q.label;
    return result;
  };

  // Highest readable quality that fits as one file. Size falls as DPI falls,
  // so binary search over the ladder.
  const ladder = READABLE_LADDER;
  if ((await run(ladder[ladder.length - 1])).length <= target) {
    let lo = 0, hi = ladder.length - 1; // hi is known to fit
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if ((await run(ladder[mid])).length <= target) hi = mid;
      else lo = mid + 1;
    }
    return single(ladder[hi], cache.get(ladder[hi].dpi));
  }

  // Split, best quality first, readable before last resort.
  for (const q of [...READABLE_LADDER, ...LAST_RESORT_LADDER]) {
    const belowFloor = LAST_RESORT_LADDER.includes(q);
    const bytes = await run(q);
    if (belowFloor && bytes.length <= target) {
      result.warnings.push("Compressed below 100 DPI to fit; check fine drawing detail is still legible.");
      return single(q, bytes);
    }
    progress(`Checking a split at ${q.dpi} DPI…`);
    const parts = await trySplit(bytes, target, maxParts, stem);
    if (parts) {
      result.status = "split";
      result.parts = parts;
      result.quality = q.label;
      if (belowFloor) {
        result.warnings.push(
          `Needed ${parts.length} parts and below-100 DPI quality to fit; check fine drawing detail is still legible.`
        );
      }
      return result;
    }
  }

  // Nothing fits. Return the smallest single file we made.
  let bestDpi = null;
  for (const [dpi, bytes] of cache) if (bestDpi === null || bytes.length < cache.get(bestDpi).length) bestDpi = dpi;
  result.status = "over_target";
  if (cache.get(bestDpi).length >= input.length) {
    result.parts = [{ name: `${stem}.pdf`, bytes: input }];
    result.quality = "Original (could not be reduced)";
  } else {
    const q = [...READABLE_LADDER, ...LAST_RESORT_LADDER].find((x) => x.dpi === bestDpi);
    single(q, cache.get(bestDpi));
  }
  result.warnings.push(
    `Could not get under the target, even split into ${maxParts} parts. ` +
      "This file is likely large vector drawings or many pages; consider splitting it by hand or raising the target size."
  );
  return result;
}

self.onmessage = async ({ data }) => {
  if (data.type === "warmup") {
    try {
      await loadEngine();
      self.postMessage({ type: "ready" });
    } catch (e) {
      self.postMessage({ type: "engine_error", message: e.message || String(e) });
    }
    return;
  }
  const { id, buffer, target, stem, maxParts } = data;
  try {
    const input = new Uint8Array(buffer);
    if (!new TextDecoder("latin1").decode(input.subarray(0, 1024)).includes("%PDF")) {
      throw new Error("That file doesn't look like a PDF.");
    }
    const progress = (text) => self.postMessage({ type: "progress", id, text });
    const result = await compressPdf(input, target, stem, maxParts || 3, progress);
    const parts = result.parts.map((p) => ({ name: p.name, size: p.bytes.length, bytes: p.bytes }));
    self.postMessage({ type: "done", id, result: { ...result, parts } });
  } catch (e) {
    self.postMessage({ type: "error", id, message: e.message || String(e) });
  }
};
