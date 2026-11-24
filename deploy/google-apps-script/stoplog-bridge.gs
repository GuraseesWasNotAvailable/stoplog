// stoplog bridge: lets the stoplog app read and write THIS spreadsheet as you, with no
// Google Cloud project or key file.
//
// Setup (once): in the sheet, Extensions > Apps Script, replace everything with this file,
// Save, then Deploy > New deployment > type "Web app", Execute as: Me, Who has access:
// Anyone > Deploy > Authorize. Copy the Web app URL into stoplog (STOPLOG_WEBAPP_URL).
// Treat that URL like a password: anyone who has it can read and add rows.

const TABS = {
  Log: ["event_id", "shift_date", "shift", "line", "machine", "sku", "start", "end", "start_at",
        "duration_min", "category", "reason_group", "reason_detail", "reason_as_written",
        "category_as_written", "action_taken", "recorded_by", "confidence", "needs_review",
        "review_note", "photo", "photo_sha", "logged_at"],
  Photos: ["photo_sha", "photo", "processed_at", "status", "sheet_date", "shift", "line",
           "stops_read", "stops_added", "already_logged", "needs_review", "model", "warnings"],
};
const INSIGHTS = "Insights";

function doPost(e) {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    const req = JSON.parse(e.postData.contents);
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    let result;
    if (req.action === "ping") result = { url: ss.getUrl() };
    else if (req.action === "ensure_ready") result = ensureReady(ss);
    else if (req.action === "append") result = append(ss, req.tab, req.rows);
    else if (req.action === "read") result = read(ss, req.tab);
    else if (req.action === "write_insights") result = writeInsights(ss, req);
    else throw new Error("unknown action " + req.action);
    return reply({ ok: true, result: result });
  } catch (err) {
    return reply({ ok: false, error: String((err && err.message) || err) });
  } finally {
    lock.releaseLock();
  }
}

function doGet() {
  return reply({ ok: true, result: "stoplog bridge is running" });
}

function reply(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function tab(ss, name) {
  return ss.getSheetByName(name) || ss.insertSheet(name);
}

function ensureReady(ss) {
  Object.keys(TABS).forEach(function (name) {
    const cols = TABS[name];
    const sh = tab(ss, name);
    const lastCol = sh.getLastColumn();
    const header = lastCol ? sh.getRange(1, 1, 1, lastCol).getValues()[0].map(String) : [];
    if (!header.some(function (h) { return h; })) {
      sh.getRange(1, 1, 1, cols.length).setValues([cols]).setFontWeight("bold").setBackground("#eeeeee");
      sh.setFrozenRows(1);
    } else if (cols.some(function (c, i) { return header[i] !== c; })) {
      throw new Error("Tab '" + name + "' already has a different header row. Rename that tab " +
                      "or use an empty sheet; stoplog will not overwrite it.");
    }
  });
  tab(ss, INSIGHTS);
  return { url: ss.getUrl() };
}

// Text cells are stored as plain text, so "2026-11-24" or "14:05" never turn into dates and
// nothing a photo says can become a formula. Numbers stay numbers.
function prepare(rows, width) {
  const values = [], formats = [];
  rows.forEach(function (r) {
    const v = [], f = [];
    for (let i = 0; i < width; i++) {
      let x = i < r.length && r[i] !== null && r[i] !== undefined ? r[i] : "";
      if (typeof x === "number") { v.push(x); f.push("General"); }
      else { v.push(String(x)); f.push("@"); }
    }
    values.push(v);
    formats.push(f);
  });
  return { values: values, formats: formats };
}

function append(ss, name, rows) {
  if (!TABS[name]) throw new Error("unknown tab " + name);
  if (!rows || !rows.length) return { added: 0 };
  const sh = tab(ss, name);
  const width = TABS[name].length;
  const p = prepare(rows, width);
  const range = sh.getRange(sh.getLastRow() + 1, 1, p.values.length, width);
  range.setNumberFormats(p.formats);
  range.setValues(p.values);
  return { added: p.values.length };
}

function read(ss, name) {
  const sh = ss.getSheetByName(name);
  if (!sh || sh.getLastRow() === 0) return [];
  const tz = ss.getSpreadsheetTimeZone();
  return sh.getRange(1, 1, sh.getLastRow(), Math.max(sh.getLastColumn(), 1)).getValues().map(function (row) {
    return row.map(function (x) {
      if (Object.prototype.toString.call(x) !== "[object Date]") return x;
      // a person typed a date or time into the sheet: send it back as text stoplog understands
      if (x.getFullYear() < 1900) return Utilities.formatDate(x, tz, "HH:mm");
      if (x.getHours() === 0 && x.getMinutes() === 0) return Utilities.formatDate(x, tz, "yyyy-MM-dd");
      return Utilities.formatDate(x, tz, "yyyy-MM-dd'T'HH:mm");
    });
  });
}

function writeInsights(ss, req) {
  const sh = tab(ss, INSIGHTS);
  sh.clear();
  const rows = req.rows || [];
  if (!rows.length) return { rows: 0 };
  const width = rows.reduce(function (m, r) { return Math.max(m, r.length); }, 1);
  const p = prepare(rows, width);
  const range = sh.getRange(1, 1, p.values.length, width);
  range.setNumberFormats(p.formats);
  range.setValues(p.values);
  (req.title_rows || []).forEach(function (r) { sh.getRange(r + 1, 1).setFontWeight("bold").setFontSize(14); });
  (req.section_rows || []).forEach(function (r) { sh.getRange(r + 1, 1, 1, width).setFontWeight("bold").setFontSize(11); });
  (req.header_rows || []).forEach(function (r) {
    sh.getRange(r + 1, 1, 1, width).setFontWeight("bold").setBackground("#eeeeee");
  });
  return { rows: p.values.length };
}
