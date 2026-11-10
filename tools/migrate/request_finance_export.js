/*
 * Sealbill: export your Request Finance invoices as NDJSON for
 *     manage.py import_invoices --source rf --org <slug>
 *
 * HOW TO USE (step by step in docs/migration.md)
 *   1. Log in at https://app.request.finance and open "Get paid" > "Sent".
 *   2. Open the browser's developer console (F12, "Console" tab). Chrome asks you to type
 *      "allow pasting" before the first paste; read this file before you do.
 *   3. Paste this whole file and press Enter. Leave the tab in the foreground: the script
 *      moves the invoice list through its pages (about 6 seconds each) and then offers the
 *      file request-finance-export-<date>.ndjson as a download.
 *
 * WHAT IT DOES
 *   Request Finance's own page asks its API for the sent invoices
 *   (GET https://api.request.finance/invoices?format=paginated&filterBy=sent&variant=rnf_invoice).
 *   The page's login token cannot be read by a script, so this script does not call the API
 *   itself: it wraps XMLHttpRequest.prototype.open so that the page's own request asks for 200
 *   invoices from a given offset, with no status filter, and keeps the answers. It turns the
 *   pages by changing the list's filter (history.pushState plus a popstate event); each page
 *   uses a different filter value from the one before it, or the page would not ask again.
 *   When it is done it puts XMLHttpRequest back, maps the invoices to NDJSON (below) and
 *   hands you the file. Nothing is sent anywhere: the data stays in this tab and in the file.
 *   The console shows counts, Request Finance invoice ids and reasons, never a name, an
 *   address, an e-mail or an amount.
 *
 * THE MAPPING
 *   - one `contact` record per distinct buyer block (e-mail, names, address, tax id). Its
 *     source id is "b:" plus the first 16 hex digits of the SHA-1 of that block, so every
 *     invoice keeps the buyer it was issued to; the importer merges contacts by e-mail.
 *   - one `invoice` record per invoice, in the invoice's own currency. RF stores amounts in
 *     the currency's smallest unit with its own number of decimals; they are converted
 *     exactly to Sealbill's (RF_CURRENCIES below), and an invoice whose amounts do not
 *     convert exactly, or whose currency is not in the table, is left out and named.
 *   - the discount is per line (`discount_amount_minor`); a percentage tax becomes
 *     `tax_rate_bps`. A fixed tax amount has no field in Sealbill: such an invoice is left
 *     out and named. The total is computed from the lines the way Sealbill computes it
 *     (Request Finance's list carries no total of its own), so compare a few totals with
 *     the RF pages before the real import.
 *   - the paid date is the latest declareReceivedPayment event, else the latest
 *     declareSentPayment event, else (for a paid invoice) its first tracked transaction.
 *   - drafts and canceled or rejected invoices are passed through with their status: the
 *     importer skips them and counts them. A buyer whom only such invoices name gets no
 *     contact record.
 *
 * Written against the Request Finance web app as it was in November 2026. If RF changes its
 * page, the script stops with a message rather than writing a partial file.
 */
(function () {
  'use strict';

  // Request Finance currency id -> [Sealbill invoice currency, RF decimals, Sealbill decimals].
  // Sealbill's decimals are its minor units (apps/invoices/currencies.py: ISO 4217 for fiat,
  // MIGRATED_ONLY for the carried-over crypto currencies). RF's decimals are the token's own.
  // "seen" = met in a real export in November 2026 and checked against RF's pages;
  // "list" = from RF's public currency list (https://api.request.finance/currency/list/invoicing?meta=true).
  // To add a currency: Sealbill must know it, and RF's decimals must come from that list.
  const RF_CURRENCIES = {
    'EUR': ['EUR', 2, 2],              // seen
    'USD': ['USD', 2, 2],              // seen
    'BTC': ['BTC', 8, 8],              // seen
    'BTC-mainnet': ['BTC', 8, 8],      // seen
    'USDT-mainnet': ['USDT', 6, 6],    // seen
    'USDC-mainnet': ['USDC', 6, 6],    // seen
    'USDC-tron': ['USDC', 6, 6],       // seen
    'USDT-tron': ['USDT', 6, 6],       // list
    'TUSD-tron': ['TUSD', 6, 2],       // list
    'TRX-tron': ['TRX', 6, 6],         // seen
    'DAI': ['DAI', 18, 2],             // seen
    'TUSD-mainnet': ['TUSD', 18, 2],   // seen
    'ETH': ['ETH', 18, 8],             // seen
    'ETH-mainnet': ['ETH', 18, 8],     // seen
    'WETH-mainnet': ['WETH', 18, 8],   // seen
    'WBTC-mainnet': ['WBTC', 8, 8],    // seen
    'WBTC-fantom': ['WBTC', 8, 8],     // seen
    'BNB-bsc': ['BNB', 18, 8],         // seen
    'MATIC': ['MATIC', 18, 8],         // seen
    'SOL-solana': ['SOL', 9, 9],       // seen
  };
  const SKIPPED_STATUSES = ['draft', 'canceled', 'cancelled', 'rejected'];  // the importer skips these
  const PAGE_SIZE = 200;
  const FILTERS = ['paid', 'paid,declaredPaid', 'declaredPaid'];  // values the list accepts in ?f=
  const LIST_PATH = '/get-paid/sent';
  const CURRENCY_LIST_URL = 'https://api.request.finance/currency/list/invoicing?meta=true';

  class LeftOut extends Error {}

  // ---- exact arithmetic ---------------------------------------------------------------------

  // A non-negative decimal string as a BigInt scaled by 10^scale, or null when it is not one or
  // has more decimals than `scale`.
  function scaled(value, scale) {
    const m = /^(\d+)(?:\.(\d+))?$/.exec(String(value).trim());
    if (!m) return null;
    const frac = (m[2] || '').replace(/0+$/, '');
    if (frac.length > scale) return null;
    return BigInt(m[1] + frac.padEnd(scale, '0'));
  }

  // An RF amount (an integer string in RF's smallest unit) in Sealbill's minor units, exactly.
  function toMinor(value, rfDecimals, decimals) {
    const text = String(value).trim();
    if (!/^-?\d+$/.test(text)) throw new LeftOut('an amount is not a whole number of RF units');
    const v = BigInt(text);
    if (decimals >= rfDecimals) return v * 10n ** BigInt(decimals - rfDecimals);
    const d = 10n ** BigInt(rfDecimals - decimals);
    if (v % d !== 0n) throw new LeftOut('an amount does not convert exactly to ' + decimals + ' decimals');
    return v / d;
  }

  function safeNumber(big) {
    if (big > BigInt(Number.MAX_SAFE_INTEGER) || big < -BigInt(Number.MAX_SAFE_INTEGER)) {
      throw new LeftOut('an amount is too large');
    }
    return Number(big);
  }

  // Python's ROUND_HALF_UP (half away from zero) of n / d, d > 0.
  function divHalfUp(n, d) {
    const negative = n < 0n;
    const a = negative ? -n : n;
    let q = a / d;
    if ((a % d) * 2n >= d) q += 1n;
    return negative ? -q : q;
  }

  // apps/invoices/money.py, line by line: gross rounded, discount off the line, tax on the rest.
  function lineTotal(line) {
    const gross = divHalfUp(scaled(line.quantity, 2) * BigInt(line.unit_price_minor), 100n);
    const discount = BigInt(line.discount_amount_minor || 0);
    const tax = divHalfUp((gross - discount) * BigInt(line.tax_rate_bps || 0), 10000n);
    return gross - discount + tax;
  }

  // ---- mapping ------------------------------------------------------------------------------

  const text = (v) => (typeof v === 'string' ? v.trim() : '') || null;
  const day = (iso) => (iso ? String(iso).slice(0, 10) : null);

  async function sha1hex16(s) {
    const digest = await crypto.subtle.digest('SHA-1', new TextEncoder().encode(s));
    return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('').slice(0, 16);
  }

  async function contactOf(buyerInfo, notes) {
    const b = buyerInfo || {};
    const a = b.address || {};
    const person = [text(b.firstName), text(b.lastName)].filter(Boolean).join(' ') || null;
    const address = {
      line1: text(a.streetAddress) || text(a['street-address']),
      line2: text(a.extendedAddress) || text(a['extended-address']),
      city: text(a.city) || text(a.locality),
      zip: text(a.postalCode) || text(a['postal-code']),
      state: text(a.region),
      country: (text(a.country) || '').toUpperCase() || null,
    };
    if (address.country && !/^[A-Z]{2}$/.test(address.country)) {
      address.country = null;  // the importer takes ISO alpha-2 codes only
      notes['buyer country is not an ISO alpha-2 code, left off the address'] =
        (notes['buyer country is not an ISO alpha-2 code, left off the address'] || 0) + 1;
    }
    Object.keys(address).forEach((k) => { if (!address[k]) delete address[k]; });
    const contact = {
      type: 'contact',
      email: text(b.email),
      legal_name: text(b.businessName) || person,
      display_name: person || text(b.businessName),
      address: Object.keys(address).length ? address : null,
      tax_id: text(b.taxRegistration),
    };
    const id = 'b:' + await sha1hex16(JSON.stringify([
      (contact.email || '').toLowerCase(), contact.legal_name, contact.display_name, contact.address, contact.tax_id,
    ]));
    return { ...contact, source_id: id };
  }

  function taxBps(tax) {
    const t = tax || {};
    if (!t.amount || Number(t.amount) === 0) return 0;
    if (t.type !== 'percentage') throw new LeftOut('a line has a fixed tax amount, which Sealbill cannot carry');
    const bps = scaled(t.amount, 2);
    if (bps === null || bps > 10000n) throw new LeftOut('a line has a tax rate that is not a percentage with at most two decimals');
    return Number(bps);
  }

  function linesOf(items, rfCurrency, table) {
    const [, rfDecimals, decimals] = table[rfCurrency];
    return items.map((x) => {
      if (x.currency !== rfCurrency) throw new LeftOut('the lines are in more than one currency');
      if (scaled(x.quantity, 2) === null) throw new LeftOut('a line has a quantity with more than two decimals');
      const line = {
        name: String(x.name || '').slice(0, 500) || 'Item',
        quantity: String(x.quantity).trim(),
        unit_price_minor: safeNumber(toMinor(x.unitPrice, rfDecimals, decimals)),
        tax_rate_bps: taxBps(x.tax),
      };
      if (x.discount && String(x.discount).trim() !== '0') {
        line.discount_amount_minor = safeNumber(toMinor(x.discount, rfDecimals, decimals));
        if (line.discount_amount_minor < 0) throw new LeftOut('a line has a negative discount');
      }
      return line;
    });
  }

  function paidDate(invoice, status) {
    const latest = (name) => (invoice.events || []).filter((e) => e && e.name === name)
      .map((e) => e.date).filter(Boolean).sort().slice(-1)[0];
    const tracked = (invoice.trackedTransactions || [])[0];
    return day(latest('declareReceivedPayment') || latest('declareSentPayment')
      || (status === 'paid' && tracked && tracked.createdAt) || null);
  }

  // RF list items -> {contacts, invoices, leftOut: [{id, status, reason}], notes}
  async function mapInvoices(items, options) {
    const table = (options && options.currencies) || RF_CURRENCIES;
    const contacts = new Map();
    const invoices = [];
    const leftOut = [];
    const notes = {};
    for (const i of items) {
      const status = i.status || 'draft';
      try {
        const rfItems = i.invoiceItems || [];
        if (!rfItems.length) throw new LeftOut('no invoice lines');
        const rfCurrency = rfItems[0].currency;
        if (!table[rfCurrency]) throw new LeftOut('currency ' + String(rfCurrency).slice(0, 40) + ' is not in RF_CURRENCIES');
        const lines = linesOf(rfItems, rfCurrency, table);
        const total = safeNumber(lines.reduce((sum, line) => sum + lineTotal(line), 0n));
        const issued = day(i.creationDate || i.createdAt);
        if (!issued) throw new LeftOut('no creation date');
        const contact = await contactOf(i.buyerInfo, notes);
        const due = i.paymentTerms && i.paymentTerms.dueDate
          ? Math.max(0, Math.round((new Date(i.paymentTerms.dueDate) - new Date(issued)) / 86400000)) : null;
        // A buyer only skipped invoices name is never written: the importer skips those invoices before
        // it looks for their contact, and a contact for someone never billed has no place in the books.
        if (!SKIPPED_STATUSES.includes(status.toLowerCase()) && !contacts.has(contact.source_id)) {
          contacts.set(contact.source_id, contact);
        }
        invoices.push({
          type: 'invoice', source_id: i.id, number: text(i.invoiceNumber), kind: 'invoice',
          contact_source_id: contact.source_id, currency: table[rfCurrency][0], issue_date: issued,
          paid_date: paidDate(i, status), due_days: Number.isFinite(due) ? due : null, title: null,
          footer: text(i.note), lines, total_minor: total, source_status: status,
          source_url: i.requestId ? 'https://app.request.finance/' + i.requestId : '',
        });
      } catch (e) {
        if (!(e instanceof LeftOut)) throw e;
        leftOut.push({ id: String(i.id), status, reason: e.message });
      }
    }
    return { contacts: [...contacts.values()], invoices, leftOut, notes };
  }

  function toNdjson(mapped) {
    return mapped.contacts.concat(mapped.invoices).map((r) => JSON.stringify(r)).join('\n') + '\n';
  }

  function summary(items, mapped) {
    const count = (list, key) => list.reduce((o, x) => { o[key(x)] = (o[key(x)] || 0) + 1; return o; }, {});
    return {
      invoicesRead: items.length,
      invoicesWritten: mapped.invoices.length,
      contactsWritten: mapped.contacts.length,
      byStatus: count(mapped.invoices, (v) => v.source_status),
      byCurrency: count(mapped.invoices, (v) => v.currency),
      willBeSkippedByTheImporter: mapped.invoices.filter((v) => SKIPPED_STATUSES.includes(v.source_status.toLowerCase())).length,
      leftOut: mapped.leftOut.length,
      notes: mapped.notes,
    };
  }

  // RF's own decimals for every currency in the table, when its public list can be read. A
  // mismatch stops the run: a wrong number of decimals is a wrong amount by a power of ten.
  function decimalMismatches(list, table) {
    const entries = Array.isArray(list) ? list : (list && (list.data || list.result || list.currencies)) || [];
    const known = {};
    entries.forEach((c) => { if (c && typeof c.id === 'string' && Number.isInteger(c.decimals)) known[c.id] = c.decimals; });
    if (!Object.keys(known).length) return null;  // a shape we do not recognise: no check
    return Object.keys(table).filter((id) => id in known && known[id] !== table[id][1])
      .map((id) => id + ': RF says ' + known[id] + ' decimals, the table says ' + table[id][1]);
  }

  // ---- in the browser -----------------------------------------------------------------------

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function waitFor(check, timeoutMs, pollMs) {
    const end = Date.now() + timeoutMs;
    for (;;) {
      const value = check();
      if (value) return value;
      if (Date.now() > end) return null;
      await sleep(pollMs);
    }
  }

  // Turns the page's own invoice list through every page and returns the invoices it received.
  async function collect(w, opts) {
    const o = { take: PAGE_SIZE, timeoutMs: 30000, pollMs: 250, settleMs: 4000, log: () => {}, ...(opts || {}) };
    const proto = w.XMLHttpRequest.prototype;
    const original = proto.open;
    if (original.__sealbill) throw new Error('the export is already running in this tab; reload the page to start again');
    const pages = new Map();
    let skip = 0;
    let failure = null;
    const open = function (method, url, ...rest) {
      if (typeof url === 'string' && url.includes('/invoices?') && url.includes('format=paginated')
          && url.includes('filterBy=sent')) {
        const u = new URL(url, w.location.href);
        u.searchParams.set('take', String(o.take));
        u.searchParams.set('skip', String(skip));
        u.searchParams.delete('status');
        const mine = skip;
        url = u.toString();
        this.addEventListener('load', () => {
          if (this.status !== 200) { failure = 'Request Finance answered HTTP ' + this.status + ' (are you still logged in?)'; return; }
          try { pages.set(mine, JSON.parse(this.responseText)); } catch (e) { failure = 'Request Finance sent an answer that is not JSON'; }
        });
      }
      return original.call(this, method, url, ...rest);
    };
    open.__sealbill = true;
    proto.open = open;
    const go = (path) => { w.history.pushState({}, '', path); w.dispatchEvent(new w.PopStateEvent('popstate', { state: {} })); };
    try {
      if (!w.location.pathname.startsWith(LIST_PATH)) { go(LIST_PATH); await sleep(o.settleMs); }
      let last = new URL(w.location.href).searchParams.get('f');
      const invoices = new Map();
      let total = null;
      for (let n = 0; ; n += 1) {
        skip = n * o.take;
        let f = FILTERS[n % FILTERS.length];
        if (f === last) f = FILTERS[(n + 1) % FILTERS.length];  // the same value twice: the page would not ask
        last = f;
        go(LIST_PATH + '?f=' + f);
        const body = await waitFor(() => failure || pages.get(skip), o.timeoutMs, o.pollMs);
        if (failure) throw new Error(failure);
        if (!body) throw new Error('no page of invoices arrived for offset ' + skip + ' within ' + (o.timeoutMs / 1000)
          + ' s. Open Get paid > Sent, check you are logged in, and run the script again.');
        if (!Array.isArray(body.result)) throw new Error('Request Finance answered in a shape this script does not know');
        if (total === null) total = Number(body.count);
        body.result.forEach((i) => invoices.set(i.id, i));
        o.log('page ' + (n + 1) + ': ' + body.result.length + ' invoices (' + invoices.size + ' of ' + total + ')');
        if (body.result.length < o.take || skip + o.take >= total) break;
      }
      if (Number.isFinite(total) && invoices.size !== total) {
        throw new Error('Request Finance counted ' + total + ' sent invoices but ' + invoices.size + ' arrived; run the script again');
      }
      return [...invoices.values()];
    } finally {
      proto.open = original;
    }
  }

  function download(w, body, filename) {
    const url = w.URL.createObjectURL(new w.Blob([body], { type: 'application/x-ndjson' }));
    const a = w.document.createElement('a');
    a.href = url;
    a.download = filename;
    a.style.display = 'none';
    w.document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => w.URL.revokeObjectURL(url), 60000);
  }

  async function run(w) {
    if (w.location.hostname !== 'app.request.finance') {
      throw new Error('run this on https://app.request.finance, logged in, on Get paid > Sent');
    }
    const log = (m) => console.log('[sealbill] ' + m);
    try {
      const res = await fetch(CURRENCY_LIST_URL, { credentials: 'omit' });
      const problems = res.ok ? decimalMismatches(await res.json(), RF_CURRENCIES) : null;
      if (problems && problems.length) throw new Error('RF_CURRENCIES disagrees with Request Finance: ' + problems.join('; '));
      log(problems ? 'currency decimals checked against Request Finance\'s list' : 'currency decimals could not be checked (unknown answer)');
    } catch (e) {
      if (String(e.message).startsWith('RF_CURRENCIES')) throw e;
      log('currency decimals could not be checked against Request Finance\'s list (' + e.message + ')');
    }
    log('reading the sent invoices, about 6 seconds a page; keep this tab in front');
    const items = await collect(w, { log });
    const mapped = await mapInvoices(items);
    const name = 'request-finance-export-' + new Date().toISOString().slice(0, 10) + '.ndjson';
    download(w, toNdjson(mapped), name);
    log('offered ' + name + ' for download. Summary (no personal data):');
    console.log(summary(items, mapped));
    if (mapped.leftOut.length) {
      log('left out, by Request Finance invoice id (open them in RF to decide what to do):');
      console.table(mapped.leftOut);
    }
    return summary(items, mapped);
  }

  const api = {
    RF_CURRENCIES, SKIPPED_STATUSES, FILTERS, scaled, toMinor, divHalfUp, lineTotal, sha1hex16, contactOf,
    mapInvoices, toNdjson, summary, decimalMismatches, collect, download, run,
  };
  if (typeof window !== 'undefined' && typeof document !== 'undefined') {
    run(window).catch((e) => console.error('[sealbill] export stopped: ' + e.message + '. Nothing was written.'));
  } else if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  }
})();
