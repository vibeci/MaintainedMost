const assert = require('node:assert/strict');
const test = require('node:test');
const alert = require('../upstream-alert.cjs');

function fixture(issues = [], overrides = {}) {
    const calls = [];
    const api = Object.fromEntries(['getLabel', 'createLabel', 'listForRepo', 'create', 'update', 'createComment'].map((name) => [name, async (args) => {
        calls.push({...args, name});
        return {data: {number: 100}};
    }]));
    const github = {rest: {issues: api}, paginate: async (method, args) => {
        assert.equal(method, api.listForRepo);
        assert.equal(args.per_page, 100);
        calls.push({name: 'paginate'});
        return issues;
    }};
    const context = {repo: {owner: 'fork-owner', repo: 'maintainedmost'}, runId: 42};
    const env = {REPO: 'mattermost/mattermost-plugin-calls', KEY: 'CALLS_TAG', CURRENT: 'v1.12.3', NEW_REF: 'v1.12.5', VERIFICATION: 'clean', MIN_SERVER: '12.0.0', SERVER_TAG: 'v11.11.1', ...overrides};
    return {github, context, env, calls};
}

test('incompatible Calls releases warn, not recommend an automatic upgrade', async () => {
    const input = fixture();
    await alert(input);
    const created = input.calls.find((call) => call.name === 'create');
    assert.match(created.body, /Minimum Mattermost server: `12.0.0`/);
    assert.match(created.body, /Do not upgrade Calls/);
    assert.match(created.body, /not a build, test, or compatibility verdict/);
    assert.equal(created.owner, 'fork-owner');
});

test('unknown minimum compatibility requires a check', async () => {
    const input = fixture([], {MIN_SERVER: 'unknown'});
    await alert(input);
    assert.match(input.calls.find((call) => call.name === 'create').body, /Minimum server compatibility is unknown/);
});

test('patch conflicts require rebasing without dropping patches', async () => {
    const input = fixture([], {VERIFICATION: 'conflict'});
    await alert(input);
    assert.match(input.calls.find((call) => call.name === 'create').body, /do not drop security or feature patches/);
});

test('network failures and incomplete checks cannot create rebase alerts', async () => {
    for (const status of ['', 'error', 'unchanged']) {
        const input = fixture([], {VERIFICATION: status});
        await assert.rejects(alert(input), /without a completed patch check/);
        assert.equal(input.calls.length, 0);
    }
});

test('paginated alerts are updated, deduplicated, and maintainer notes survive', async () => {
    const begin = '<!-- maintainedmost-upstream:CALLS_TAG -->';
    const end = '<!-- /maintainedmost-upstream -->';
    const unrelated = Array.from({length: 100}, (_, index) => ({number: index + 1, title: 'other issue', body: ''}));
    const input = fixture([...unrelated,
        {number: 101, title: 'old alert', body: `Maintainer context\n${begin}\nstale\n${end}\nDo not lose this note`, user: {login: 'github-actions[bot]'}},
        {number: 102, title: 'duplicate', body: `${begin}\nstale\n${end}`, user: {login: 'github-actions[bot]'}},
        {number: 103, title: 'user issue', body: `Copied marker: ${begin}`, user: {login: 'maintainer'}},
    ]);
    await alert(input);
    assert.equal(input.calls.filter((call) => call.name === 'create').length, 0);
    const update = input.calls.find((call) => call.name === 'update' && call.issue_number === 101);
    assert.match(update.body, /^Maintainer context/);
    assert.match(update.body, /Do not lose this note$/);
    assert.match(update.body, /v1.12.5/);
    assert.equal(input.calls.find((call) => call.name === 'update' && call.issue_number === 102).state, 'closed');
    assert.ok(!input.calls.some((call) => call.issue_number === 103));
});

test('legacy bot alerts are adopted without overwriting manual issues', async () => {
    const input = fixture([
        {number: 1, title: 'mattermost/mattermost-plugin-calls v1.12.4 is available', body: 'Old discussion', user: {login: 'maintainer'}},
        {number: 2, title: 'mattermost/mattermost-plugin-calls v1.12.5 is available', body: 'Previous automation', user: {login: 'github-actions[bot]'}},
    ]);
    await alert(input);
    const update = input.calls.find((call) => call.name === 'update');
    assert.equal(update.issue_number, 2);
    assert.match(update.body, /Previous alert \(superseded\)/);
    assert.match(update.body, /Previous automation/);
});

test('fresh forks get the missing label but other API failures propagate', async () => {
    const input = fixture();
    input.github.rest.issues.getLabel = async () => { throw {status: 404}; };
    await alert(input);
    assert.ok(input.calls.some((call) => call.name === 'createLabel'));
    const denied = fixture();
    denied.github.rest.issues.getLabel = async () => { throw {status: 403}; };
    await assert.rejects(alert(denied));
    assert.equal(denied.calls.length, 0);
});

test('transcriber alerts describe branch advance and the immutable ref', async () => {
    const input = fixture([], {REPO: 'mattermost/calls-transcriber', KEY: 'TRANSCRIBER_TAG', CURRENT: 'a'.repeat(40), NEW_REF: 'b'.repeat(40), BRANCH: 'master'});
    await alert(input);
    const created = input.calls.find((call) => call.name === 'create');
    assert.match(created.title, /master advanced to bbbbbbbbbbbb/);
    assert.ok(created.body.includes('b'.repeat(40)));
});
