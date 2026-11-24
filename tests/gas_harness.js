// Runs deploy/google-apps-script/stoplog-bridge.gs against an in-memory spreadsheet.
// Reads one JSON request per line on stdin, writes one JSON reply per line on stdout.
// The mock copies the Sheets behaviour that matters: in a cell not formatted as text,
// "2026-11-24" becomes a date and "=..." becomes a formula.
// Extra harness-only actions: {"action":"__dump","tab":..} and {"action":"__set_date",...}.
"use strict";
const fs = require("fs");
const vm = require("vm");
const readline = require("readline");

class Sheet {
  constructor(name) { this.name = name; this.cells = new Map(); this.fmt = new Map(); this.frozen = 0; }
  key(r, c) { return r + "," + c; }
  getRange(r, c, nr = 1, nc = 1) {
    if (r < 1 || c < 1 || nr < 1 || nc < 1) throw new Error("Range out of bounds: " + [r, c, nr, nc]);
    return new Range(this, r, c, nr, nc);
  }
  getLastRow() { let m = 0; for (const k of this.cells.keys()) m = Math.max(m, +k.split(",")[0]); return m; }
  getLastColumn() { let m = 0; for (const k of this.cells.keys()) m = Math.max(m, +k.split(",")[1]); return m; }
  setFrozenRows(n) { this.frozen = n; }
  clear() { this.cells.clear(); this.fmt.clear(); }
}

class Range {
  constructor(sheet, r, c, nr, nc) { Object.assign(this, { sheet, r, c, nr, nc }); }
  check(v) {
    if (v.length !== this.nr || v.some((row) => row.length !== this.nc))
      throw new Error("The number of rows or columns in the data does not match the range.");
  }
  setNumberFormats(f) { this.check(f); f.forEach((row, i) => row.forEach((x, j) => this.sheet.fmt.set(this.sheet.key(this.r + i, this.c + j), x))); return this; }
  setValues(v) {
    this.check(v);
    v.forEach((row, i) => row.forEach((x, j) => {
      const k = this.sheet.key(this.r + i, this.c + j);
      const text = this.sheet.fmt.get(k) === "@";
      let stored = x;
      if (!text && typeof x === "string" && /^\d{4}-\d{2}-\d{2}$/.test(x)) stored = new Date(x + "T00:00:00");
      if (!text && typeof x === "string" && x.startsWith("=")) stored = { formula: x };
      if (x === "" || x === null) this.sheet.cells.delete(k); else this.sheet.cells.set(k, stored);
    }));
    return this;
  }
  getValues() {
    const out = [];
    for (let i = 0; i < this.nr; i++) {
      const row = [];
      for (let j = 0; j < this.nc; j++) {
        const x = this.sheet.cells.get(this.sheet.key(this.r + i, this.c + j));
        row.push(x === undefined ? "" : x);
      }
      out.push(row);
    }
    return out;
  }
  setFontWeight() { return this; }
  setBackground() { return this; }
  setFontSize() { return this; }
}

const sheets = new Map([["Sheet1", new Sheet("Sheet1")]]);
const ss = {
  getSheetByName: (n) => sheets.get(n) || null,
  insertSheet: (n) => { const s = new Sheet(n); sheets.set(n, s); return s; },
  getUrl: () => "https://docs.google.com/spreadsheets/d/TEST/edit",
  getSpreadsheetTimeZone: () => "Asia/Kolkata",
};
const pad = (n) => String(n).padStart(2, "0");
const context = {
  SpreadsheetApp: { getActiveSpreadsheet: () => ss },
  LockService: { getScriptLock: () => ({ waitLock() {}, releaseLock() {} }) },
  ContentService: {
    MimeType: { JSON: "json" },
    createTextOutput: (s) => ({ content: s, setMimeType() { return this; } }),
  },
  Utilities: {
    formatDate: (d, tz, f) => f
      .replace("yyyy", d.getFullYear()).replace("MM", pad(d.getMonth() + 1)).replace("dd", pad(d.getDate()))
      .replace("'T'", "T").replace("HH", pad(d.getHours())).replace("mm", pad(d.getMinutes())),
  },
  JSON, String, Object, Math, Error,
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context);

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", (line) => {
  const req = JSON.parse(line);
  let out;
  if (req.action === "__dump") {
    const s = sheets.get(req.tab);
    out = { ok: true, result: s ? [...s.cells.entries()].map(([k, v]) => [k, v instanceof Date ? "Date" : typeof v === "object" ? "formula" : typeof v]) : null };
  } else if (req.action === "__set_date") {
    sheets.get(req.tab).cells.set(req.row + "," + req.col, new Date(req.iso));
    out = { ok: true };
  } else if (req.action === "__set") {
    if (!sheets.has(req.tab)) sheets.set(req.tab, new Sheet(req.tab));
    sheets.get(req.tab).cells.set(req.row + "," + req.col, req.value);
    out = { ok: true };
  } else {
    out = JSON.parse(context.doPost({ postData: { contents: line } }).content);
  }
  process.stdout.write(JSON.stringify(out) + "\n");
});
