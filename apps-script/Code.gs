/**
 * CS Agent Tracker — Apps Script (v3)
 * Bound to the "CS Agent Tracker" Google Sheet.
 *
 * v2 changes:
 *  - Columns are resolved BY HEADER NAME, never by fixed index. You can
 *    reorder, add or remove columns freely; the script only writes into
 *    columns whose headers it finds, and NEVER extends the grid.
 *  - Every agent-written field OVERWRITES the single cell for that customer
 *    on every run (Risk Rationale, Context, Next/Last Meeting, Summary,
 *    Outstanding Actions, Last Alert Sent). Nothing accumulates.
 *  - Ingests the new synthesis fields: next_meeting, last_meeting_date,
 *    last_meeting_summary, outstanding_actions.
 *
 * v3 changes:
 *  - syncConnectedAssets() pulls live charge-point counts from the
 *    "Real-time Charge Points" tab of the CS Delivery & KPI Dashboard
 *    (populated by the Grafana agent) into the Connected Assets column.
 *    Value-only: it overwrites the number and raises no Slack alerts.
 *  - Account names are matched by normalisation + token overlap, so
 *    "Evtec (EON)"→"EON", "JPL Stevie"→"JPL", "Evo"→"EVO EV" resolve
 *    without a hand-maintained alias table.
 *
 * Responsibilities:
 *  1. syncConnectedAssets() — live charge points per customer.
 *  2. ingestSynthesis()     — pull the newest cs-agent-synthesis.json from
 *     Drive (written each weekday by the Cowork agent) into the Accounts tab.
 *  3. checkRenewals()       — deterministic date-math alerts to Slack.
 *  4. main()                — run all three; attached to the daily 8-9am trigger.
 *
 * Setup (one-time): Script Property SLACK_WEBHOOK_URL; run setupTriggers().
 */

var SHEET_NAME = 'Accounts';
var SYNTHESIS_FILE = 'cs-agent-synthesis.json';
var RENEWAL_WINDOW_DAYS = 90;

/* Source of live charge-point counts: "Customer Success Delivery & KPI Dashboard" */
var CP_SOURCE_ID = '1C6jWAtzHbq-0dftz3yzrTx_LU3QIF2p4vryQvKUzqBQ';
var CP_SOURCE_TAB = 'Real-time Charge Points';
var CP_SOURCE_ACCOUNT_HDR = 'Account';
var CP_SOURCE_COUNT_HDR = 'Total No. CPs';

/** Header names as they appear in row 1 (case/space-insensitive match). */
var HDR = {
  CUSTOMER: 'Customer',
  HEALTH: 'Account Health',
  RISK_AGENT: 'Renewal Risk (Agent)',
  RISK_RATIONALE: 'Risk Rationale (Agent)',
  RENEWAL_DATE: 'Renewal Date',
  RENEWAL_STATUS: 'Renewal Status',
  NEXT_MEETING: 'Next Meeting',
  LAST_MEETING_DATE: 'Last Meeting Date',
  LAST_MEETING_SUMMARY: 'Last Meeting Summary',
  OUTSTANDING: 'Outstanding Actions',
  CONTEXT: 'Context - Current State (Agent, daily)',
  CONNECTED_ASSETS: 'Connected Assets',
  LAST_ALERT: 'Last Alert Sent (system)'
};

function main() {
  syncConnectedAssets();
  ingestSynthesis();
  checkRenewals();
}

/** Resolve {headerKey: columnIndex(1-based)} from row 1. Missing headers → absent. */
function resolveColumns_(sheet) {
  var headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  var norm = function (s) { return String(s).toLowerCase().replace(/\s+/g, ' ').trim(); };
  var map = {};
  Object.keys(HDR).forEach(function (key) {
    var target = norm(HDR[key]);
    for (var c = 0; c < headers.length; c++) {
      if (norm(headers[c]) === target) { map[key] = c + 1; return; }
    }
    Logger.log('Header not found (field skipped): ' + HDR[key]);
  });
  return map;
}

/** Write value into the customer's row under the named header — overwrite only. */
function setField_(sheet, col, rowNum, value) {
  if (!col) return;                      // header missing → skip, never extend grid
  sheet.getRange(rowNum, col).setValue(value);
}

/* ------------------------------------------------------------------ */
/* 1. Live charge points → Connected Assets (OVERWRITES, value only)     */
/* ------------------------------------------------------------------ */
/**
 * Pulls "Total No. CPs" per account from the Real-time Charge Points tab of
 * the CS Delivery & KPI Dashboard and overwrites the Connected Assets cell
 * for each matching customer. Never extends the grid, never alerts.
 */
function syncConnectedAssets() {
  var source = readChargePointSource_();
  if (!source.length) { Logger.log('Connected Assets: no source rows found — skipped'); return; }

  var sheet = SpreadsheetApp.getActive().getSheetByName(SHEET_NAME);
  var col = resolveColumns_(sheet);
  if (!col.CUSTOMER) { Logger.log('Customer column not found — aborting asset sync'); return; }
  if (!col.CONNECTED_ASSETS) { Logger.log('Connected Assets column not found — skipped'); return; }

  var lastRow = sheet.getLastRow();
  if (lastRow < 2) return;
  var names = sheet.getRange(2, col.CUSTOMER, lastRow - 1, 1).getValues();

  var targets = [];
  for (var r = 0; r < names.length; r++) {
    var name = String(names[r][0]).trim();
    if (name) targets.push({ name: name, rowNum: r + 2 });
  }

  var pairs = matchAccounts_(targets, source);
  var written = 0, missed = [];

  pairs.forEach(function (p) {
    if (p.match) {
      setField_(sheet, col.CONNECTED_ASSETS, p.target.rowNum, p.match.count);
      written++;
    } else {
      missed.push(p.target.name);
    }
  });

  Logger.log('Connected Assets updated for ' + written + '/' + targets.length + ' customers' +
    (missed.length ? ' — unmatched: ' + missed.join(', ') : ''));
}

/** Read [{account, count}] from the source tab, locating the header row by name. */
function readChargePointSource_() {
  var ss = SpreadsheetApp.openById(CP_SOURCE_ID);
  var tab = ss.getSheetByName(CP_SOURCE_TAB);
  if (!tab) { Logger.log('Source tab not found: ' + CP_SOURCE_TAB); return []; }

  var values = tab.getDataRange().getValues();
  var norm = normaliseName_;
  var accountCol = -1, countCol = -1, headerRow = -1;

  // Scan the first 20 rows for the header row — tolerates title/blank rows above it.
  for (var r = 0; r < Math.min(values.length, 20) && headerRow === -1; r++) {
    for (var c = 0; c < values[r].length; c++) {
      var cell = norm(values[r][c]);
      if (cell === norm(CP_SOURCE_ACCOUNT_HDR)) accountCol = c;
      if (cell === norm(CP_SOURCE_COUNT_HDR)) countCol = c;
    }
    if (accountCol > -1 && countCol > -1) headerRow = r;
    else { accountCol = -1; countCol = -1; }
  }
  if (headerRow === -1) {
    Logger.log('Could not locate "' + CP_SOURCE_ACCOUNT_HDR + '" / "' + CP_SOURCE_COUNT_HDR + '" headers in ' + CP_SOURCE_TAB);
    return [];
  }

  var out = [];
  for (var i = headerRow + 1; i < values.length; i++) {
    var account = String(values[i][accountCol] || '').trim();
    if (!account) continue;
    if (norm(account) === 'total' || norm(account) === 'grandtotal') continue;

    var count = parseCount_(values[i][countCol]);
    if (count === null) continue;
    out.push({ account: account, count: count, key: norm(account), tokens: tokenise_(account) });
  }
  return out;
}

/** "5,830" | 5830 | " 5830 " → 5830. Anything non-numeric → null (row skipped). */
function parseCount_(v) {
  if (v === '' || v === null || v === undefined) return null;
  if (typeof v === 'number') return isNaN(v) ? null : Math.round(v);
  var n = parseFloat(String(v).replace(/[,\s]/g, ''));
  return isNaN(n) ? null : Math.round(n);
}

/** Lowercase, strip everything that isn't a letter or digit. "Cord (RAC)" → "cordrac". */
function normaliseName_(s) {
  return String(s === null || s === undefined ? '' : s).toLowerCase().replace(/[^a-z0-9]/g, '');
}

/** "Evtec (EON)" → ["evtec","eon"] — words and bracketed parts as separate tokens. */
function tokenise_(s) {
  return String(s).toLowerCase().split(/[^a-z0-9]+/)
    .filter(function (t) { return t.length > 0; });
}

/**
 * Match tracker customers to source accounts with no alias table.
 * Pass 1: exact normalised equality (claims the source row).
 * Pass 2: best token-overlap score against remaining unclaimed source rows;
 *         ties are left unmatched rather than guessed.
 */
function matchAccounts_(targets, source) {
  var claimed = {}, results = [];

  targets.forEach(function (t) {
    t.key = normaliseName_(t.name);
    t.tokens = tokenise_(t.name);
  });

  // Pass 1 — exact normalised match.
  targets.forEach(function (t) {
    for (var i = 0; i < source.length; i++) {
      if (!claimed[i] && source[i].key === t.key) {
        claimed[i] = true;
        results.push({ target: t, match: source[i] });
        return;
      }
    }
    results.push({ target: t, match: null });
  });

  // Pass 2 — token overlap for whatever is still unmatched.
  results.forEach(function (res) {
    if (res.match) return;
    var best = null, bestScore = 0, tied = false;

    for (var i = 0; i < source.length; i++) {
      if (claimed[i]) continue;
      var score = scoreMatch_(res.target, source[i]);
      if (score > bestScore) { bestScore = score; best = i; tied = false; }
      else if (score === bestScore && score > 0) { tied = true; }
    }

    if (best !== null && bestScore > 0 && !tied) {
      claimed[best] = true;
      res.match = source[best];
    } else if (tied) {
      Logger.log('Ambiguous account match, left unmatched: ' + res.target.name);
    }
  });

  return results;
}

/** Shared tokens, +1 bonus when one normalised name is a prefix of the other. */
function scoreMatch_(target, src) {
  var shared = 0;
  target.tokens.forEach(function (tk) {
    if (src.tokens.indexOf(tk) > -1) shared++;
  });
  if (!shared) return 0;
  var prefix = (target.key.indexOf(src.key) === 0 || src.key.indexOf(target.key) === 0) ? 1 : 0;
  return shared * 2 + prefix;
}

/* ------------------------------------------------------------------ */
/* 2. Ingest the agent's daily synthesis (OVERWRITES, one row/customer) */
/* ------------------------------------------------------------------ */
function ingestSynthesis() {
  var files = DriveApp.getFilesByName(SYNTHESIS_FILE);
  var latest = null;
  while (files.hasNext()) {
    var f = files.next();
    if (!latest || f.getLastUpdated() > latest.getLastUpdated()) latest = f;
  }
  if (!latest) { Logger.log('No synthesis file found'); return; }

  var data = JSON.parse(latest.getBlob().getDataAsString());
  var sheet = SpreadsheetApp.getActive().getSheetByName(SHEET_NAME);
  var col = resolveColumns_(sheet);
  if (!col.CUSTOMER) { Logger.log('Customer column not found — aborting'); return; }

  var names = sheet.getRange(2, col.CUSTOMER, sheet.getLastRow() - 1, 1).getValues();

  (data.accounts || []).forEach(function (acc) {
    var rowNum = null;
    for (var r = 0; r < names.length; r++) {
      if (String(names[r][0]).trim().toLowerCase() === String(acc.customer).trim().toLowerCase()) {
        rowNum = r + 2; break;
      }
    }
    if (!rowNum) { Logger.log('Customer not in sheet (skipped): ' + acc.customer); return; }

    if (acc.renewal_risk)        setField_(sheet, col.RISK_AGENT, rowNum, acc.renewal_risk);
    if (acc.risk_rationale)      setField_(sheet, col.RISK_RATIONALE, rowNum, acc.risk_rationale);
    if (acc.context_bullets && acc.context_bullets.length)
      setField_(sheet, col.CONTEXT, rowNum, '• ' + acc.context_bullets.join('\n• '));
    if (acc.next_meeting)        setField_(sheet, col.NEXT_MEETING, rowNum, acc.next_meeting);
    if (acc.last_meeting_date)   setField_(sheet, col.LAST_MEETING_DATE, rowNum, acc.last_meeting_date);
    if (acc.last_meeting_summary) setField_(sheet, col.LAST_MEETING_SUMMARY, rowNum, acc.last_meeting_summary);
    if (acc.outstanding_actions && acc.outstanding_actions.length)
      setField_(sheet, col.OUTSTANDING, rowNum, '• ' + acc.outstanding_actions.join('\n• '));
  });
  Logger.log('Synthesis ingested: ' + (data.generated_at || 'no timestamp'));
}

/* ------------------------------------------------------------------ */
/* 3. Deterministic renewal / risk alerts                               */
/* ------------------------------------------------------------------ */
function checkRenewals() {
  var sheet = SpreadsheetApp.getActive().getSheetByName(SHEET_NAME);
  var col = resolveColumns_(sheet);
  if (!col.CUSTOMER) return;
  var rows = sheet.getDataRange().getValues();
  var today = new Date(); today.setHours(0, 0, 0, 0);
  var warnings = [], risks = [];

  var get = function (row, key) { return col[key] ? row[col[key] - 1] : ''; };

  for (var r = 1; r < rows.length; r++) {
    var row = rows[r];
    var customer = get(row, 'CUSTOMER');
    if (!customer) continue;

    var renewalDate = get(row, 'RENEWAL_DATE');
    var status = String(get(row, 'RENEWAL_STATUS') || '');
    var health = String(get(row, 'HEALTH') || '');
    var riskAgent = String(get(row, 'RISK_AGENT') || '');
    var alertState = parseAlertState(get(row, 'LAST_ALERT'));

    if (renewalDate instanceof Date) {
      var days = Math.round((renewalDate - today) / 86400000);
      if (days < 0 && status !== 'Signed' && status !== 'Churned') {
        if (shouldAlert(alertState, 'overdue', 7)) {
          warnings.push(':rotating_light: *' + customer + '* renewal is *OVERDUE* (' +
            fmt(renewalDate) + ', status: ' + status + ')');
          markAlert(sheet, r + 1, col.LAST_ALERT, alertState, 'overdue');
        }
      } else if (days <= RENEWAL_WINDOW_DAYS && days >= 0 && status === 'Not started') {
        if (shouldAlert(alertState, 'window90', 7)) {
          warnings.push(':hourglass_flowing_sand: *' + customer + '* renews in *' + days +
            ' days* (' + fmt(renewalDate) + ') and the renewal is *Not started*');
          markAlert(sheet, r + 1, col.LAST_ALERT, alertState, 'window90');
        }
      }
    } else if (!renewalDate && status !== 'On Hold' && status !== 'Churned') {
      if (shouldAlert(alertState, 'tbcDate', 14)) {
        warnings.push(':grey_question: *' + customer + '* has *no renewal date* on record — check the contract position');
        markAlert(sheet, r + 1, col.LAST_ALERT, alertState, 'tbcDate');
      }
    }

    if (health === 'Red' || riskAgent === 'Red') {
      risks.push('*' + customer + '*' +
        (get(row, 'RISK_RATIONALE') ? ' — ' + get(row, 'RISK_RATIONALE') : ''));
    }
  }

  var blocks = [];
  if (warnings.length) blocks.push('*:calendar: Renewal warnings*\n' + warnings.join('\n'));
  if (risks.length) blocks.push('*:red_circle: At-risk accounts*\n' + risks.join('\n'));
  if (blocks.length) postToSlack(blocks.join('\n\n'));
}

/* ------------------------------------------------------------------ */
/* Helpers                                                              */
/* ------------------------------------------------------------------ */
function parseAlertState(v) {
  try { return v ? JSON.parse(v) : {}; } catch (e) { return {}; }
}
function shouldAlert(state, key, cooldownDays) {
  if (!state[key]) return true;
  return (new Date() - new Date(state[key])) / 86400000 >= cooldownDays;
}
function markAlert(sheet, rowNum, colIndex, state, key) {
  if (!colIndex) return;                 // header missing → do not extend grid
  state[key] = new Date().toISOString();
  sheet.getRange(rowNum, colIndex).setValue(JSON.stringify(state));
}
function fmt(d) {
  return Utilities.formatDate(d, Session.getScriptTimeZone(), 'dd MMM yyyy');
}
function postToSlack(text) {
  var url = PropertiesService.getScriptProperties().getProperty('SLACK_WEBHOOK_URL');
  if (!url) throw new Error('SLACK_WEBHOOK_URL script property not set');
  UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify({ text: text })
  });
}

/* One-time trigger setup */
function setupTriggers() {
  ScriptApp.getProjectTriggers().forEach(function (t) { ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger('main').timeBased().atHour(8).everyDays(1).create();
}
