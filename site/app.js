"use strict";

// ---------------------------------------------------------------------------
// Config — update these if the repo/branch this site targets ever changes.
// The branch MUST be whichever branch the scheduled price-check workflow
// reads booked-dates.txt from (see .github/workflows/price-check.yml),
// or ticking a box here won't actually stop that date being checked.
// ---------------------------------------------------------------------------
const OWNER = "rosjo99";
const REPO = "Train-prices";
const BRANCH = "main";
const BOOKED_DATES_PATH = "booked-dates.txt";
const PRICE_HISTORY_PATH = "price-history.csv";
const TOKEN_STORAGE_KEY = "nre_booked_dates_pat";

const API_BASE = "https://api.github.com";
// The repo is public, so both of these can be read with no token at
// all via GitHub's raw content host — used for read-only display
// (the price history, and the booked-dates table before a token is
// added). Writing booked-dates.txt still goes through the authenticated
// Contents API below, which is the only path that needs a token.
const RAW_BASE = `https://raw.githubusercontent.com/${OWNER}/${REPO}/${BRANCH}`;

// ---------------------------------------------------------------------------
// Term-date logic — a JS port of src/term_dates.py's is_checkable_day()/
// checkable_dates(), operating on plain ISO "YYYY-MM-DD" strings (which
// compare correctly with plain string comparison, sidestepping JS Date's
// local-timezone footguns entirely). Data comes from terms.json, exported
// fresh from the real Python source of truth by scripts/export_terms.py
// on every deploy — see that script's docstring for why it's not just
// hand-duplicated here.
// ---------------------------------------------------------------------------

function addDaysISO(iso, days) {
  const [y, m, d] = iso.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  dt.setUTCDate(dt.getUTCDate() + days);
  return dt.toISOString().slice(0, 10);
}

function weekdayMon0(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  return (dt.getUTCDay() + 6) % 7; // JS: Sun=0..Sat=6 -> Mon=0..Sun=6
}

function weekdayName(iso) {
  const names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  return names[weekdayMon0(iso)];
}

function termFor(iso, terms) {
  for (const term of terms) {
    if (iso >= term.start && iso <= term.end) return term;
  }
  return null;
}

function isCheckableDay(iso, termsData) {
  if (!termsData.check_weekdays.includes(weekdayMon0(iso))) return false;
  const term = termFor(iso, termsData.terms);
  if (!term) return false;
  if (term.excluded_days.includes(iso)) return false;
  for (const [start, end] of term.excluded_ranges) {
    if (iso >= start && iso <= end) return false;
  }
  return true;
}

function checkableDates(startIso, endIso, termsData) {
  const result = [];
  let cur = startIso;
  while (cur <= endIso) {
    if (isCheckableDay(cur, termsData)) result.push(cur);
    cur = addDaysISO(cur, 1);
  }
  return result;
}

function todayLocalISO() {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

// ---------------------------------------------------------------------------
// booked-dates.txt parsing/rendering — mirrors src/booked_dates.py's
// rules (blank lines and #-comments ignored, one YYYY-MM-DD per line),
// preserving any leading comment block when rewriting the file so it
// still reads sensibly if someone opens the raw file on github.com.
// ---------------------------------------------------------------------------

function parseBookedContent(text) {
  const headerLines = [];
  const dates = new Set();
  let sawDate = false;
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;
    if (line.startsWith("#")) {
      if (!sawDate) headerLines.push(rawLine);
      continue;
    }
    if (/^\d{4}-\d{2}-\d{2}$/.test(line)) {
      dates.add(line);
      sawDate = true;
    }
  }
  return { headerLines, dates };
}

function renderBookedContent(headerLines, datesSet) {
  const sorted = Array.from(datesSet).sort();
  const parts = [...headerLines];
  if (parts.length > 0) parts.push("");
  parts.push(...sorted);
  return parts.join("\n") + "\n";
}

// ---------------------------------------------------------------------------
// price-history.csv parsing — mirrors src/price_log.py's columns
// (checked_at, travel_date, target_departure, actual_departure,
// arrival_time, price_gbp, railcard_applied, sold_out, fare_name). Only
// a minimal CSV parser is needed: every field this project ever writes
// is comma/quote-free except possibly fare_name, so quoted fields are
// still handled for robustness in case that ever changes.
// ---------------------------------------------------------------------------

function parseCsvLine(line) {
  const fields = [];
  let current = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (inQuotes) {
      if (ch === '"' && line[i + 1] === '"') {
        current += '"';
        i++;
      } else if (ch === '"') {
        inQuotes = false;
      } else {
        current += ch;
      }
    } else if (ch === '"') {
      inQuotes = true;
    } else if (ch === ",") {
      fields.push(current);
      current = "";
    } else {
      current += ch;
    }
  }
  fields.push(current);
  return fields;
}

function parseCsv(text) {
  const lines = text.split(/\r?\n/).filter((l) => l.length > 0);
  if (lines.length === 0) return [];
  const header = parseCsvLine(lines[0]);
  return lines.slice(1).map((line) => {
    const values = parseCsvLine(line);
    const row = {};
    header.forEach((key, i) => {
      row[key] = values[i] ?? "";
    });
    return row;
  });
}

// Returns latest[travel_date][target_departure] = row, keeping only the
// most-recently-checked (max checked_at) row for each pair — the CSV is
// append-only, so a date checked on many different days has many rows.
function latestPriceByDateAndTarget(rows) {
  const latest = {};
  for (const row of rows) {
    const byDate = (latest[row.travel_date] ??= {});
    const existing = byDate[row.target_departure];
    if (!existing || row.checked_at > existing.checked_at) {
      byDate[row.target_departure] = row;
    }
  }
  return latest;
}

// Python's `datetime.now(timezone.utc).isoformat()` (see src/price_log.py)
// writes checked_at as "YYYY-MM-DDTHH:MM:SS.ffffff+00:00" — 6-digit
// microseconds, which some JS engines fail to parse via `new Date()`.
// Trimming to millisecond precision first keeps this reliably parseable.
function parseCheckedAt(iso) {
  if (!iso) return null;
  const trimmed = iso.replace(/(\.\d{3})\d*/, "$1");
  const d = new Date(trimmed);
  return isNaN(d.getTime()) ? null : d;
}

function formatLastUpdated(iso) {
  const d = parseCheckedAt(iso);
  if (!d) return null;
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

// The single most recent checked_at across every row — i.e. when the
// price-check job last actually ran and wrote a result, regardless of
// which date/target it was for. Plain string comparison is safe here
// since checked_at is always this fixed-width ISO 8601 format.
function latestCheckedAt(rows) {
  let latest = "";
  for (const row of rows) {
    if (row.checked_at > latest) latest = row.checked_at;
  }
  return latest;
}

function formatLatestCell(row) {
  if (!row) return "–";
  if (row.sold_out === "True") return "sold out";
  if (!row.actual_departure) return "not found";
  if (!row.price_gbp) return "–";
  return `£${Number(row.price_gbp).toFixed(2)}`;
}

// True if any target departure's last-recorded price on this date beats
// the alert threshold — mirrors src.main.evaluate()'s own price check
// (price present, and strictly below threshold), used only to decide
// this row's highlight, never to send an alert (the CSV is just a log).
function rowHasCheapFare(byTarget, targetDepartures, threshold) {
  return targetDepartures.some((target) => {
    const row = byTarget[target];
    if (!row || row.sold_out === "True" || !row.actual_departure || !row.price_gbp) return false;
    return Number(row.price_gbp) < threshold;
  });
}

// ---------------------------------------------------------------------------
// GitHub Contents API — every call goes straight from this browser to
// api.github.com using the token pasted into the setup box, stored only
// in this browser's localStorage. Nothing here talks to any other
// server.
// ---------------------------------------------------------------------------

function utf8ToBase64(str) {
  return btoa(unescape(encodeURIComponent(str)));
}

function base64ToUtf8(b64) {
  return decodeURIComponent(escape(atob(b64.replace(/\n/g, ""))));
}

async function githubGetFile(token) {
  const url = `${API_BASE}/repos/${OWNER}/${REPO}/contents/${encodeURIComponent(BOOKED_DATES_PATH)}?ref=${encodeURIComponent(BRANCH)}`;
  const res = await fetch(url, {
    // Without this, the browser can serve a cached response for this
    // authenticated GET and hand back a stale sha — the write below
    // would then be rejected by GitHub with a 409 even though nothing
    // else actually changed the file recently.
    cache: "no-store",
    headers: { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json" },
  });
  if (res.status === 404) {
    return { content: "", sha: null };
  }
  if (!res.ok) {
    throw new Error(`GitHub API error ${res.status} reading the file: ${await res.text()}`);
  }
  const data = await res.json();
  return { content: base64ToUtf8(data.content), sha: data.sha };
}

async function fetchBookedDatesContent(token) {
  // Reading booked-dates.txt through the same authenticated Contents
  // API toggleDate() writes through avoids raw.githubusercontent.com's
  // CDN, which can lag several minutes behind a very recent commit —
  // enough that a save you just made can look like it "disappeared" on
  // the next refresh, even though it landed correctly (this exact
  // Contents API read is already known-fresh: see githubGetFile's
  // cache: "no-store"). Anonymous viewers (no token yet) still use the
  // CDN-backed raw endpoint below, which is fine for read-only browsing
  // and needs no auth. A broken/expired token falls back to that same
  // public copy rather than failing the whole page load.
  if (token) {
    try {
      const { content } = await githubGetFile(token);
      return content;
    } catch (err) {
      console.error("authenticated read of booked-dates.txt failed, falling back to the public copy:", err);
    }
  }
  return (await fetchRawFile(BOOKED_DATES_PATH)) || "";
}

async function fetchRawFile(path) {
  const res = await fetch(`${RAW_BASE}/${path}`, { cache: "no-store" });
  if (res.status === 404) return null; // e.g. price-history.csv before the first successful run ever commits it
  if (!res.ok) {
    throw new Error(`Could not read ${path} (HTTP ${res.status})`);
  }
  return res.text();
}

async function githubPutFile(token, content, sha, message) {
  const body = { message, content: utf8ToBase64(content), branch: BRANCH };
  if (sha) body.sha = sha;
  const res = await fetch(`${API_BASE}/repos/${OWNER}/${REPO}/contents/${encodeURIComponent(BOOKED_DATES_PATH)}`, {
    method: "PUT",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = new Error(`GitHub API error ${res.status} saving the file: ${await res.text()}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// UI
// ---------------------------------------------------------------------------

const statusEl = document.getElementById("status");
const containerEl = document.getElementById("dates-container");
const tokenMissingEl = document.getElementById("token-missing");
const tokenPresentEl = document.getElementById("token-present");
const tokenInputEl = document.getElementById("token-input");

function getToken() {
  return localStorage.getItem(TOKEN_STORAGE_KEY) || "";
}

function setToken(token) {
  localStorage.setItem(TOKEN_STORAGE_KEY, token);
}

function clearToken() {
  localStorage.removeItem(TOKEN_STORAGE_KEY);
}

function showStatus(message, kind) {
  statusEl.textContent = message;
  statusEl.className = kind || "";
}

function refreshTokenUI() {
  const hasToken = !!getToken();
  tokenMissingEl.hidden = hasToken;
  tokenPresentEl.hidden = !hasToken;
}

document.getElementById("token-save").addEventListener("click", () => {
  const value = tokenInputEl.value.trim();
  if (!value) return;
  setToken(value);
  tokenInputEl.value = "";
  refreshTokenUI();
  showStatus("Token saved. Loading your dates…", "ok");
  loadAndRender();
});

document.getElementById("token-clear").addEventListener("click", () => {
  clearToken();
  refreshTokenUI();
  showStatus("Token removed from this browser.", "ok");
  loadAndRender();
});

// rowEl/isCheap let a successful save update this row's highlight
// immediately, without waiting for the next full reload — isCheap is
// fixed at render time (from the last-recorded price), so it doesn't
// change just because the booked state did.
async function toggleDate(iso, checkboxEl, rowEl, isCheap) {
  const token = getToken();
  if (!token) {
    showStatus("Add your token above first.", "error");
    checkboxEl.checked = !checkboxEl.checked;
    return;
  }
  checkboxEl.disabled = true;
  showStatus(`Saving ${iso}…`);
  const nowBooking = checkboxEl.checked;
  const MAX_SAVE_ATTEMPTS = 3;
  try {
    for (let attempt = 1; attempt <= MAX_SAVE_ATTEMPTS; attempt++) {
      // Re-fetch fresh right before writing, so a change made from
      // another device/tab (or a previous attempt in this same retry
      // loop) in the meantime isn't clobbered.
      const { content, sha } = await githubGetFile(token);
      const { headerLines, dates } = parseBookedContent(content);
      if (nowBooking) {
        dates.add(iso);
      } else {
        dates.delete(iso);
      }
      const newContent = renderBookedContent(headerLines, dates);
      const message = nowBooking
        ? `Mark ${iso} as booked (via booked-dates site)`
        : `Unmark ${iso} as booked (via booked-dates site)`;
      try {
        await githubPutFile(token, newContent, sha, message);
      } catch (err) {
        // 409 = the file changed between our GET and PUT above (another
        // tab/device, or a previous attempt in this loop) — GitHub
        // correctly refused to overwrite it blindly. Re-fetch and retry
        // with the now-current content rather than surfacing this as a
        // failure the user has to notice and manually retry themselves.
        if (err.status === 409 && attempt < MAX_SAVE_ATTEMPTS) {
          showStatus(`Saving ${iso}… (file changed, retrying)`);
          continue;
        }
        throw err;
      }
      showStatus(`Saved — ${iso} is now ${nowBooking ? "booked" : "not booked"}.`, "ok");
      if (rowEl) {
        rowEl.classList.toggle("row-booked", nowBooking);
        rowEl.classList.toggle("row-cheap", !nowBooking && isCheap);
      }
      return;
    }
  } catch (err) {
    console.error(err);
    checkboxEl.checked = !nowBooking;
    if (String(err).includes("401") || String(err).includes("403")) {
      showStatus("Your token was rejected — it may have expired. See the setup instructions to create a new one.", "error");
    } else {
      showStatus(`Could not save: ${err.message || err}`, "error");
    }
  } finally {
    checkboxEl.disabled = false;
  }
}

function renderTable(dates, bookedSet, termsData, latestByDate, hasToken) {
  containerEl.innerHTML = "";

  if (dates.length === 0) {
    containerEl.innerHTML = "<p>No checkable dates remain this school year.</p>";
    return;
  }

  const targetDepartures = termsData.target_departures || [];
  const threshold = Number(termsData.price_threshold);
  let currentTermName = null;
  let tbody = null;

  for (const iso of dates) {
    const term = termFor(iso, termsData.terms);
    const termName = term ? term.name : "Unknown term";
    if (termName !== currentTermName) {
      currentTermName = termName;
      const section = document.createElement("div");
      section.className = "term-group";
      const heading = document.createElement("h2");
      heading.textContent = termName;
      section.appendChild(heading);
      const table = document.createElement("table");
      const priceHeaders = targetDepartures.map((t) => `<th>${t}</th>`).join("");
      table.innerHTML = `<thead><tr><th>Date</th><th>Day</th>${priceHeaders}<th>Booked?</th></tr></thead>`;
      tbody = document.createElement("tbody");
      table.appendChild(tbody);
      section.appendChild(table);
      containerEl.appendChild(section);
    }

    const row = document.createElement("tr");
    const dateCell = document.createElement("td");
    dateCell.textContent = iso;
    const dayCell = document.createElement("td");
    dayCell.textContent = weekdayName(iso);
    row.appendChild(dateCell);
    row.appendChild(dayCell);

    const byTarget = latestByDate[iso] || {};
    for (const target of targetDepartures) {
      const priceCell = document.createElement("td");
      priceCell.textContent = formatLatestCell(byTarget[target]);
      row.appendChild(priceCell);
    }

    const isBooked = bookedSet.has(iso);
    const isCheap = rowHasCheapFare(byTarget, targetDepartures, threshold);
    // Booked wins over cheap: once a ticket is actually bought, that's a
    // more useful signal to see at a glance than "this was cheap".
    row.classList.toggle("row-booked", isBooked);
    row.classList.toggle("row-cheap", !isBooked && isCheap);

    const checkboxCell = document.createElement("td");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = isBooked;
    checkbox.disabled = !hasToken;
    checkbox.title = hasToken ? "" : "Add your token above to edit";
    checkbox.setAttribute("aria-label", `Mark ${iso} as booked`);
    checkbox.addEventListener("change", () => toggleDate(iso, checkbox, row, isCheap));
    checkboxCell.appendChild(checkbox);
    row.appendChild(checkboxCell);

    tbody.appendChild(row);
  }
}

async function loadAndRender() {
  // The repo is public, so the date list, current booked state, and
  // price history are all readable with no token at all — a token is
  // only ever needed to actually save a change (see toggleDate). This
  // means the table (including prices) shows up immediately for anyone
  // with the link, with editing simply disabled until a token is added.
  // Once a token IS present, booked state is read through it too (see
  // fetchBookedDatesContent) so your own edits show up immediately
  // instead of however long raw.githubusercontent.com's CDN takes to
  // catch up.
  const token = getToken();

  try {
    showStatus("Loading…");
    const [termsResponse, bookedContent, priceHistoryText] = await Promise.all([
      fetch("terms.json").then((r) => r.json()),
      fetchBookedDatesContent(token),
      fetchRawFile(PRICE_HISTORY_PATH),
    ]);

    const { dates: bookedSet } = parseBookedContent(bookedContent);
    const priceRows = priceHistoryText ? parseCsv(priceHistoryText) : [];
    const latestByDate = latestPriceByDateAndTarget(priceRows);

    const pricesUpdatedEl = document.getElementById("prices-updated");
    if (pricesUpdatedEl) {
      const formatted = formatLastUpdated(latestCheckedAt(priceRows));
      pricesUpdatedEl.textContent = formatted
        ? `Prices last updated: ${formatted} (your local time).`
        : "Prices last updated: never — no successful run yet.";
    }

    const start = addDaysISO(todayLocalISO(), 1);
    const dates = checkableDates(start, termsResponse.last_known_date, termsResponse);

    renderTable(dates, bookedSet, termsResponse, latestByDate, !!token);
    showStatus(
      priceHistoryText ? "" : "No prices recorded yet — they'll appear here after the first successful run.",
      priceHistoryText ? "" : "ok"
    );

    const generatedAtEl = document.getElementById("generated-at");
    if (generatedAtEl && termsResponse.generated_at) {
      generatedAtEl.textContent = `Date list generated ${termsResponse.generated_at}.`;
    }
  } catch (err) {
    console.error(err);
    showStatus(`Could not load dates: ${err.message || err}`, "error");
    const pricesUpdatedEl = document.getElementById("prices-updated");
    if (pricesUpdatedEl) pricesUpdatedEl.textContent = "";
  }
}

refreshTokenUI();
loadAndRender();
