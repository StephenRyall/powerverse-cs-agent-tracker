/**
 * CS Agent Tracker — Apps Script (v2)
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
 * Responsibilities:
 *  1. ingestSynthesis() — pull the newest cs-agent-synthesis.json from Drive
 *     (written each weekday by the Cowork agent) into the Accounts tab.
 *  2. checkRenewals()   — deterministic date-math alerts to Slack.
 *  3. main()            — run both; attach to the daily 8-9am trigger.
 *
 * Setup (one-time): Script Property SLACK_WEBHOOK_URL; run setupTriggers().
 */

var SHEET_NAME = 'Accounts';
var SYNTHESIS_FILE = 'cs-agent-synthesis.json';
var RENEWAL_WINDOW_DAYS = 90;

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
  LAST_ALERT: 'Last Alert Sent (system)'
};

function main() {
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
/* 1. Ingest the agent's daily synthesis (OVERWRITES, one row/customer) */
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
/* 2. Deterministic renewal / risk alerts                               */
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
