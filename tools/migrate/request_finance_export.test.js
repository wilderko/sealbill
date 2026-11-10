// node --test tools/migrate/   (Node 20 or newer; no packages)
// Synthetic data only: testdata/rf_invoices.json uses example.test addresses and "Fixture" names.
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const nodeCrypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const rf = require('./request_finance_export.js');
const FIXTURE = JSON.parse(fs.readFileSync(path.join(__dirname, 'testdata', 'rf_invoices.json'), 'utf8'));

const byId = (list) => Object.fromEntries(list.map((x) => [x.source_id || x.id, x]));

test('amounts convert exactly or not at all', () => {
  assert.equal(rf.toMinor('9900000', 8, 8), 9900000n);
  assert.equal(rf.toMinor('123450000000000000', 18, 8), 12345000n);
  assert.equal(rf.toMinor('1500000000000000000000', 18, 2), 150000n);
  assert.equal(rf.toMinor('25', 2, 4), 2500n);
  assert.throws(() => rf.toMinor('1000000000000000001', 18, 8), /convert exactly/);
  assert.throws(() => rf.toMinor('12.5', 2, 2), /whole number/);
});

test('line totals follow apps/invoices/money.py: gross, discount off the line, tax rounded half up', () => {
  assert.equal(rf.lineTotal({ quantity: '2', unit_price_minor: 50000, discount_amount_minor: 1000, tax_rate_bps: 2000 }), 118800n);
  assert.equal(rf.lineTotal({ quantity: '1.5', unit_price_minor: 333, tax_rate_bps: 0 }), 500n);  // 499.5 -> 500
  assert.equal(rf.lineTotal({ quantity: '1', unit_price_minor: 1005, tax_rate_bps: 50 }), 1010n);  // tax 5.025 -> 5
  assert.equal(rf.divHalfUp(-5n, 2n), -3n);  // half away from zero, as Decimal's ROUND_HALF_UP
});

test('the fixture maps to contacts, invoices and named refusals', async () => {
  const mapped = await rf.mapInvoices(FIXTURE.result);
  const inv = byId(mapped.invoices);
  assert.deepEqual(Object.keys(inv).sort(), ['rf0001', 'rf0002', 'rf0003', 'rf0004', 'rf0005', 'rf0006', 'rf0011', 'rf0012']);
  assert.deepEqual(mapped.leftOut.map((x) => [x.id, x.reason]), [
    ['rf0007', 'currency XYZ-mainnet is not in RF_CURRENCIES'],
    ['rf0008', 'an amount does not convert exactly to 8 decimals'],
    ['rf0009', 'a line has a fixed tax amount, which Sealbill cannot carry'],
    ['rf0010', 'the lines are in more than one currency'],
  ]);

  const one = inv.rf0001;
  assert.deepEqual(one.lines, [{ name: 'Consulting', quantity: '2', unit_price_minor: 50000, tax_rate_bps: 2000, discount_amount_minor: 1000 }]);
  assert.equal(one.total_minor, 118800);
  assert.equal(one.currency, 'EUR');
  assert.equal(one.issue_date, '2025-03-01');
  assert.equal(one.due_days, 14);
  assert.equal(one.paid_date, '2025-03-06');  // the latest declareReceivedPayment
  assert.equal(one.footer, 'Thank you, fixture footer');
  assert.equal(one.source_url, 'https://app.request.finance/req0001');
  assert.equal(one.kind, 'invoice');

  assert.equal(inv.rf0002.currency, 'BTC');
  assert.equal(inv.rf0002.total_minor, 9900000);
  assert.equal(inv.rf0002.paid_date, '2025-04-02');  // the tracked transaction of a paid invoice
  assert.equal(inv.rf0003.currency, 'ETH');
  assert.equal(inv.rf0003.total_minor, 12345000);  // 18 decimals shown at 8
  assert.equal(inv.rf0003.paid_date, '2025-05-03');  // declareSentPayment when nothing was received
  assert.equal(inv.rf0004.total_minor, 225000);  // 1.5 x 1500.00 DAI
  assert.equal(inv.rf0004.paid_date, null);
  assert.equal(inv.rf0005.source_status, 'canceled');
  assert.equal(inv.rf0006.source_status, 'draft');
  assert.equal(inv.rf0011.currency, 'USDT');
  assert.equal(inv.rf0011.total_minor, 25000000);
  assert.equal(inv.rf0011.source_url, 'https://app.request.finance/req0011');
});

test('one contact per buyer block, written before the invoices', async () => {
  const mapped = await rf.mapInvoices(FIXTURE.result);
  const inv = byId(mapped.invoices);
  assert.equal(mapped.contacts.length, 3);  // the buyer of only a draft and a canceled invoice gets none
  for (const c of mapped.contacts) assert.match(c.source_id, /^b:[0-9a-f]{16}$/);
  // the same buyer typed with another case and a trailing space is the same block
  assert.equal(inv.rf0001.contact_source_id, inv.rf0002.contact_source_id);
  assert.equal(inv.rf0001.contact_source_id, inv.rf0012.contact_source_id);
  assert.equal(inv.rf0003.contact_source_id, inv.rf0004.contact_source_id);
  const contacts = byId(mapped.contacts);
  const buyer = contacts[inv.rf0001.contact_source_id];
  assert.deepEqual(buyer, {
    type: 'contact', email: 'Buyer.One@Example.test', legal_name: 'Ján Fixtúra', display_name: 'Ján Fixtúra',
    address: { line1: 'Fixture Street 1', city: 'Bratislava', zip: '81101', country: 'SK' }, tax_id: 'SK0000000001',
    source_id: buyer.source_id,
  });
  // the id is SHA-1 over exactly this block, first 16 hex digits
  const block = JSON.stringify(['buyer.one@example.test', 'Ján Fixtúra', 'Ján Fixtúra', buyer.address, 'SK0000000001']);
  assert.equal(buyer.source_id, 'b:' + nodeCrypto.createHash('sha1').update(block).digest('hex').slice(0, 16));
  const labs = contacts[inv.rf0003.contact_source_id];
  assert.equal(labs.legal_name, 'Fixture Labs Ltd');
  assert.equal(labs.display_name, 'Fixture Labs Ltd');
  const nomail = contacts[inv.rf0011.contact_source_id];
  assert.equal(nomail.email, null);
  assert.deepEqual(nomail.address, { city: 'Košice' });  // "Slovakia" is not an alpha-2 code
  assert.equal(mapped.notes['buyer country is not an ISO alpha-2 code, left off the address'], 1);

  const lines = rf.toNdjson(mapped).trimEnd().split('\n').map((l) => JSON.parse(l));
  const firstInvoice = lines.findIndex((r) => r.type === 'invoice');
  assert.equal(firstInvoice, 3);
  assert.ok(lines.slice(firstInvoice).every((r) => r.type === 'invoice'));
});

test('the summary holds counts and no personal data', async () => {
  const mapped = await rf.mapInvoices(FIXTURE.result);
  const s = rf.summary(FIXTURE.result, mapped);
  assert.equal(s.invoicesRead, 12);
  assert.equal(s.invoicesWritten, 8);
  assert.equal(s.willBeSkippedByTheImporter, 2);
  assert.deepEqual(s.byCurrency, { EUR: 4, BTC: 1, ETH: 1, DAI: 1, USDT: 1 });
  const text = JSON.stringify(s) + JSON.stringify(mapped.leftOut);
  for (const personal of ['Fixt', 'example.test', 'Bratislava', '118800', 'SK0000']) assert.ok(!text.includes(personal), personal);
});

test('a currency table that disagrees with RF\'s own list is caught', () => {
  const list = [{ id: 'ETH-mainnet', decimals: 18 }, { id: 'USDT-mainnet', decimals: 6 }, { id: 'DAI', decimals: 6 }];
  assert.deepEqual(rf.decimalMismatches(list, rf.RF_CURRENCIES), ['DAI: RF says 6 decimals, the table says 18']);
  assert.deepEqual(rf.decimalMismatches({ data: [{ id: 'EUR', decimals: 2 }] }, rf.RF_CURRENCIES), []);
  assert.equal(rf.decimalMismatches({ unexpected: true }, rf.RF_CURRENCIES), null);
});

// A stand-in for the RF web app: on every popstate whose ?f= changed it asks the API through
// XMLHttpRequest, the way the real page does, and the API answers from a list of invoices.
function fakeApp(all, { onOpen } = {}) {
  const listeners = {};
  const w = {
    location: new URL('https://app.request.finance/get-paid/sent'),
    history: { pushState(_s, _t, p) { w.location = new URL(p, w.location.href); } },
    PopStateEvent: class { constructor(type) { this.type = type; } },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    dispatchEvent(ev) { (listeners[ev.type] || []).forEach((fn) => fn(ev)); },
  };
  class XHR {
    open(method, url) { this.url = url; if (onOpen) onOpen(url); }
    addEventListener(type, fn) { this.onload = fn; }
    send() {
      const u = new URL(this.url);
      const skip = Number(u.searchParams.get('skip'));
      const take = Number(u.searchParams.get('take'));
      setTimeout(() => {
        this.status = 200;
        this.responseText = JSON.stringify({ count: all.length, result: all.slice(skip, skip + take) });
        this.onload();
      }, 1);
    }
  }
  w.XMLHttpRequest = XHR;
  let lastF = null;
  w.addEventListener('popstate', () => {
    const f = w.location.searchParams.get('f');
    if (f === lastF) return;  // the real page does not refetch the same filter
    lastF = f;
    const x = new w.XMLHttpRequest();
    x.open('GET', 'https://api.request.finance/invoices?format=paginated&filterBy=sent&variant=rnf_invoice&skip=0&take=10&status=' + f);
    x.send();
  });
  return w;
}

test('collect turns every page through the page\'s own request and puts XMLHttpRequest back', async () => {
  const all = Array.from({ length: 450 }, (_, k) => ({ id: 'inv' + k }));
  const opened = [];
  const w = fakeApp(all, { onOpen: (url) => opened.push(new URL(url)) });
  const original = w.XMLHttpRequest.prototype.open;
  const got = await rf.collect(w, { pollMs: 2, settleMs: 1 });
  assert.equal(got.length, 450);
  assert.equal(w.XMLHttpRequest.prototype.open, original);
  assert.deepEqual(opened.map((u) => [u.searchParams.get('skip'), u.searchParams.get('take'), u.searchParams.get('status')]),
    [['0', '200', null], ['200', '200', null], ['400', '200', null]]);
  for (const u of opened) assert.equal(u.searchParams.get('variant'), 'rnf_invoice');
});

test('collect stops with a message when no page arrives, and still restores XMLHttpRequest', async () => {
  const w = fakeApp([]);
  w.dispatchEvent = () => {};  // a page that never asks
  const original = w.XMLHttpRequest.prototype.open;
  await assert.rejects(rf.collect(w, { pollMs: 2, timeoutMs: 30, settleMs: 1 }), /no page of invoices arrived/);
  assert.equal(w.XMLHttpRequest.prototype.open, original);
});
