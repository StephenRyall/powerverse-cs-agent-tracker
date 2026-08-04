/**
 * CS Agent Tracker — Apps Script
 * Bound to the "CS Agent Tracker" Google Sheet.
 *
 * Responsibilities:
 *  1. ingestSynthesis()  — pull the latest cs-agent-synthesis.json from Drive
 *                          (written daily by the Cowork/Claude agent) and write
 *                          Renewal Risk (Agent), Risk Rationale, Context and
 *                          Context Updated into the Accounts tab.
 *  2. checkRenewals()    — deterministic date-math alerts to Slack:
 *                          - renewal <= 90 days & status "Not started"
 *                          - renewal date overdue or TBC on an in-life account
 *                          - Red health / At Risk digest
 *  3. main()             — run both; attach to a daily 8-9am trigger.
 *
 * Setup (one-time):
 *  - Project Settings > Script Properties: add SLACK_WEBHOOK_URL
 *    (create at api.slack.com > Your App > Incoming Webhooks > #cs-agent-alerts)
 *  - Run setupTriggers() once, authorise scopes.
 */

var SHEET_NAME = 'Accounts';
var SYNTHESIS_FILE = 'cs-agent-synthesis.json';
var RENEWAL_WINDOW_DAYS = 90;

// Column indexes (1-based) — keep in sync with the sheet
var COL = {
  CUSTOMER: 1, TYPE: 2, OWNER: 3, VALUE: 4, EFFECTIVE: 5, TERM: 6,
  EXPANSION: 7, SOW: 8, HEALTH: 9, RISK_AGENT: 10, RISK_RATIONALE: 11,
  LAST_QBR: 12, RENEWAL_DATE: 13, DAYS_TO_RENEWAL: 14, TRIGGER_90: 15,
  RENEWAL_STATUS: 16, ASSETS: 17, ASSETS_LAST_RENEWAL: 18, FLEX_ELIGIBLE: 19,
  FLEX_STATUS: 20, NEXT_MEETING: 21, LAST_MEETING_DATE: 22,
  LAST_MEETING_SUMMARY: 23, OUTSTANDING: 24, CONTEXT: 25, CONTEXT_UPDATED: 26,
  NOTES: 27, LAST_ALERT: 28
};

function main() {
  ingestSynthesis();
  checkRenewals();
}

/* ------------------------------------------------------------------ */
/* 1. Ingest the agent's daily synthesis                                */
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
  var rows = sheet.getDataRange().getValues();

  (data.accounts || []).forEach(function (acc) {
    for (var r = 1; r < rows.length; r++) {
      if (String(rows[r][COL.CUSTOMER - 1]).trim().toLowerCase() ===
          String(acc.customer).trim().toLowerCase()) {
        if (acc.renewal_risk) sheet.getRange(r + 1, COL.RISK_AGENT).setValue(acc.renewal_risk);
        if (acc.risk_rationale) sheet.getRange(r + 1, COL.RISK_RATIONALE).setValue(acc.risk_rationale);
        if (acc.context_bullets && acc.context_bullets.length) {
          sheet.getRange(r + 1, COL.CONTEXT).setValue('• ' + acc.context_bullets.join('\n• '));
          sheet.getRange(r + 1, COL.CONTEXT_UPDATED).setValue(new Date());
        }
        break;
      }
    }
  });
  Logger.log('Synthesis ingested: ' + (data.generated_at || 'no timestamp'));
}

/* ------------------------------------------------------------------ */
/* 2. Deterministic renewal / risk alerts                               */
/* ------------------------------------------------------------------ */
function checkRenewals() {
  var sheet = SpreadsheetApp.getActive().getSheetByName(SHEET_NAME);
  var rows = sheet.getDataRange().getValues();
  var today = new Date(); today.setHours(0, 0, 0, 0);
  var warnings = [], risks = [];

  for (var r = 1; r < rows.length; r++) {
    var row = rows[r];
    var customer = row[COL.CUSTOMER - 1];
    if (!customer) continue;

    var renewalDate = row[COL.RENEWAL_DATE - 1];
    var status = String(row[COL.RENEWAL_STATUS - 1] || '');
    var health = String(row[COL.HEALTH - 1] || '');
    var riskAgent = String(row[COL.RISK_AGENT - 1] || '');
    var alertState = parseAlertState(row[COL.LAST_ALERT - 1]);

    if (renewalDate instanceof Date) {
      var days = Math.round((renewalDate - today) / 86400000);
      if (days < 0 && status !== 'Signed' && status !== 'Churned') {
        if (shouldAlert(alertState, 'overdue', 7)) {
          warnings.push(':rotating_light: *' + customer + '* renewal is *OVERDUE* (' +
            fmt(renewalDate) + ', status: ' + status + ')');
          markAlert(sheet, r + 1, alertState, 'overdue');
        }
      } else if (days <= RENEWAL_WINDOW_DAYS && days >= 0 && status === 'Not started') {
        if (shouldAlert(alertState, 'window90', 7)) {
          warnings.push(':hourglass_flowing_sand: *' + customer + '* renews in *' + days +
            ' days* (' + fmt(renewalDate) + ') and the renewal is *Not started*');
          markAlert(sheet, r + 1, alertState, 'window90');
        }
      }
    } else if (!renewalDate && status !== 'On Hold' && status !== 'Churned') {
      if (shouldAlert(alertState, 'tbcDate', 14)) {
        warnings.push(':grey_question: *' + customer + '* has *no renewal date* on record — check the contract position');
        markAlert(sheet, r + 1, alertState, 'tbcDate');
      }
    }

    if (health === 'Red' || riskAgent === 'Red') {
      risks.push('*' + customer + '*' +
        (row[COL.RISK_RATIONALE - 1] ? ' — ' + row[COL.RISK_RATIONALE - 1] : '') +
        (row[COL.OUTSTANDING - 1] ? '\n   Outstanding: ' + row[COL.OUTSTANDING - 1] : ''));
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
function markAlert(sheet, rowNum, state, key) {
  state[key] = new Date().toISOString();
  sheet.getRange(rowNum, COL.LAST_ALERT).setValue(JSON.stringify(state));
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
