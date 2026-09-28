'use strict';

// Re-captures tests/fixtures/fg_*.json from a real Family Graph build, so
// test_community.py stays held to the wire contract Family Graph actually
// speaks. Run it whenever Family Graph's roster output changes:
//
//   cd ~/doc-anonymizer && node tests/fixtures/capture_fg_fixtures.js
//
// FAMILY_GRAPH_DIR points at the Family Graph checkout (default: a sibling
// folder named familygraph). It builds the app on a throwaway database,
// provisions the same roster-only key Doc Anonymizer uses, seeds one
// existing person (Maria Garcia, the namesake Marie must be reviewed
// against), and sends the requests Doc Anonymizer sends (app/community.py
// _body / commit_body) over HTTP. Ids are random each run; the tests read
// them from the fixtures, never hard-code them. Fictional names only.

const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');
const crypto = require('crypto');

const FG = path.resolve(process.env.FAMILY_GRAPH_DIR || path.join(__dirname, '..', '..', '..', 'familygraph'));
const OUT = __dirname;
process.env.FAMILY_GRAPH_DISABLE_RATE_LIMIT = '1';

const { buildApp } = require(path.join(FG, 'server'));
const dbModule = require(path.join(FG, 'server', 'db'));
const apiKeys = require(path.join(FG, 'server', 'auth', 'api-keys'));
const people = require(path.join(FG, 'server', 'identity', 'people'));

function request(port, p, token, body) {
  return new Promise((resolve, reject) => {
    const data = Buffer.from(JSON.stringify(body));
    const req = http.request({
      method: 'POST', hostname: '127.0.0.1', port, path: p,
      headers: { 'content-type': 'application/json', 'content-length': data.length, authorization: `Bearer ${token}` },
    }, res => {
      let buf = '';
      res.setEncoding('utf8');
      res.on('data', c => { buf += c; });
      res.on('end', () => resolve({ status: res.statusCode, body: JSON.parse(buf) }));
    });
    req.on('error', reject);
    req.end(data);
  });
}

function write(name, obj) {
  fs.writeFileSync(path.join(OUT, name), JSON.stringify(obj, null, 2) + '\n');
}

function expect(what, got, want) {
  if (got !== want) throw new Error(`${what}: got ${got}, want ${want}`);
}

async function main() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'fg-capture-'));
  const db = dbModule.init(path.join(dir, 'capture.sqlite'));
  const secrets = {
    version: 1,
    master: crypto.randomBytes(32).toString('hex'),
    dataKey: crypto.randomBytes(32).toString('hex'),
    hmacKey: crypto.randomBytes(32).toString('hex'),
  };
  const app = buildApp({ db, secrets, thresholds: { autoMerge: 0.85, review: 0.30 } });
  const server = await new Promise(r => { const s = app.listen(0, '127.0.0.1', () => r(s)); });
  const port = server.address().port;
  try {
    const key = apiKeys.provision(db, { name: 'docanonymizer', scopes: ['roster'] });
    people.create(db, secrets, { given_name: 'Maria', family_name: 'Garcia' });

    const sheet = JSON.parse(fs.readFileSync(path.join(OUT, 'fg_request_sheet.json'), 'utf8'));
    // Same shape as app/community.py _body.
    const base = {
      sheets: [{ headers: sheet.headers, rows: sheet.rows }],
      source: 'docanonymizer',
      source_ref: 'docanonymizer:capture',
      category: 'school',
    };

    const plan = await request(port, '/api/identity/roster/plan', key.token, base);
    expect('plan status', plan.status, 200);
    expect('plan pending', JSON.stringify(plan.body.pending), '["0:2:0"]');
    const marie = plan.body.sheets[0].rows[2].persons[0];
    const decision = { key: marie.key, target: marie.candidates[0].community_id };

    // A commit with nothing decided is refused and writes nothing.
    const refused = await request(port, '/api/identity/roster/commit', key.token,
      { ...base, idempotency_key: 'docanon:capture:refused' });
    expect('refused status', refused.status, 409);

    const commit = await request(port, '/api/identity/roster/commit', key.token, {
      ...base,
      decisions: { [decision.key]: { action: 'attach', target: decision.target } },
      idempotency_key: 'docanon:capture:commit',
    });
    expect('commit status', commit.status, 201);
    expect('commit committed', commit.body.committed, true);

    write('fg_plan.json', plan.body);
    write('fg_commit_refused.json', refused.body);
    write('fg_commit.json', commit.body);
    write('fg_decision.json', decision);
    process.stdout.write('captured fg_plan, fg_commit_refused, fg_commit, fg_decision\n');
  } finally {
    await new Promise(r => server.close(r));
    db.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

main().catch(e => { process.stderr.write(`${e.stack || e}\n`); process.exit(1); });
