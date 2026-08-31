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
const TOKEN_STORAGE_KEY = "nre_booked_dates_pat";

const API_BASE = "https://api.github.com";

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
    throw new Error(`GitHub API error ${res.status} saving the file: ${await res.text()}`);
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
});

async function toggleDate(iso, checkboxEl) {
  const token = getToken();
  if (!token) {
    showStatus("Add your token above first.", "error");
    checkboxEl.checked = !checkboxEl.checked;
    return;
  }
  checkboxEl.disabled = true;
  showStatus(`Saving ${iso}…`);
  try {
    // Re-fetch fresh right before writing, so a change made from another
    // device/tab in the meantime isn't clobbered.
    const { content, sha } = await githubGetFile(token);
    const { headerLines, dates } = parseBookedContent(content);
    const nowBooking = checkboxEl.checked;
    if (nowBooking) {
      dates.add(iso);
    } else {
      dates.delete(iso);
    }
    const newContent = renderBookedContent(headerLines, dates);
    const message = nowBooking
      ? `Mark ${iso} as booked (via booked-dates site)`
      : `Unmark ${iso} as booked (via booked-dates site)`;
    await githubPutFile(token, newContent, sha, message);
    showStatus(`Saved — ${iso} is now ${nowBooking ? "booked" : "not booked"}.`, "ok");
  } catch (err) {
    console.error(err);
    checkboxEl.checked = !checkboxEl.checked;
    if (String(err).includes("401") || String(err).includes("403")) {
      showStatus("Your token was rejected — it may have expired. See the setup instructions to create a new one.", "error");
    } else {
      showStatus(`Could not save: ${err.message || err}`, "error");
    }
  } finally {
    checkboxEl.disabled = false;
  }
}

function renderTable(dates, bookedSet, termsData) {
  containerEl.innerHTML = "";

  if (dates.length === 0) {
    containerEl.innerHTML = "<p>No checkable dates remain this school year.</p>";
    return;
  }

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
      table.innerHTML = "<thead><tr><th>Date</th><th>Day</th><th>Booked?</th></tr></thead>";
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
    const checkboxCell = document.createElement("td");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = bookedSet.has(iso);
    checkbox.setAttribute("aria-label", `Mark ${iso} as booked`);
    checkbox.addEventListener("change", () => toggleDate(iso, checkbox));
    checkboxCell.appendChild(checkbox);

    row.appendChild(dateCell);
    row.appendChild(dayCell);
    row.appendChild(checkboxCell);
    tbody.appendChild(row);
  }
}

async function loadAndRender() {
  const token = getToken();
  if (!token) {
    containerEl.innerHTML = "";
    return;
  }

  try {
    showStatus("Loading…");
    const [termsResponse, fileResult] = await Promise.all([
      fetch("terms.json").then((r) => r.json()),
      githubGetFile(token),
    ]);

    const { dates: bookedSet } = parseBookedContent(fileResult.content);
    const start = addDaysISO(todayLocalISO(), 1);
    const dates = checkableDates(start, termsResponse.last_known_date, termsResponse);

    renderTable(dates, bookedSet, termsResponse);
    showStatus("");

    const generatedAtEl = document.getElementById("generated-at");
    if (generatedAtEl && termsResponse.generated_at) {
      generatedAtEl.textContent = `Date list generated ${termsResponse.generated_at}.`;
    }
  } catch (err) {
    console.error(err);
    if (String(err).includes("401") || String(err).includes("403")) {
      showStatus("Your token was rejected — it may have expired. See the setup instructions to create a new one.", "error");
    } else {
      showStatus(`Could not load dates: ${err.message || err}`, "error");
    }
  }
}

refreshTokenUI();
loadAndRender();
