// Exercise the shipped indicator logic without requiring a running GNOME Shell.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function indicator(snapshot) {
    const notifications = [];
    const context = vm.createContext({
        GObject: {registerClass: cls => cls},
        Gio: {DBusProxy: {makeProxyWrapper: () => class {}}},
        St: {Widget: class {}},
        PopupMenu: {PopupBaseMenuItem: class {}},
        PanelMenu: {Button: class {}},
        Extension: class {},
        _: text => text,
        Main: {notify: (...args) => notifications.push(args)},
    });
    const source = fs.readFileSync(path.join(__dirname, '../extension.js'), 'utf8')
        .replace(/^import .*;\n/gm, '')
        .replace('export default class', 'class');
    vm.runInContext(source + '\nglobalThis.subject = Object.create(Indicator.prototype);', context);
    const subject = context.subject;
    subject._snapshot = snapshot;
    subject._alertState = new Map();
    return {subject, notifications, context};
}

function snapshot(real = null, live = {}) {
    return {tools: ['claude_code', 'codex'], live,
        budgets: {claude_5h: 1, claude_weekly: 1, codex_5h: 1, codex_weekly: 1},
        five_hours: {tools: ['claude_code', 'codex'].map(tool =>
            ({tool, cost_usd: 9999, total_tokens: 99999, real}))},
        week: {tools: []}};
}

test('missing confirmed quotas never produce estimated pressure or alerts', () => {
    const {subject, notifications} = indicator(snapshot());
    assert.equal(subject._worstWindow('claude_code').pct, null);
    assert.equal(subject._worstWindow('codex').pct, null);
    assert.equal(subject._topPressure(['claude_code', 'codex']), null);
    subject._checkAlerts();
    assert.equal(notifications.length, 0);
});

test('confirmed quota drives the indicator regardless of local spend', () => {
    const {subject} = indicator(snapshot({used_percent: 25}));
    assert.equal(subject._worstWindow('codex').pct, 25);
    assert.equal(subject._topPressure(['codex']).pct, 25);
});

test('cached percentages do not trigger new threshold alerts', () => {
    const {subject, notifications} = indicator(snapshot({used_percent: 99},
        {claude_code: {stale: true}, codex: {stale: true}}));
    subject._checkAlerts();
    assert.equal(notifications.length, 0);
});

test('confirmed details keep a compact date and the provider card keeps the full date', () => {
    const {context} = indicator(snapshot());
    const text = vm.runInContext(
        'realDetailText({confirmed_at: 1000, source: "local_log"}, {})', context);
    const compact = vm.runInContext('confirmationTime(1000, true)', context);
    const full = vm.runInContext('confirmationTime(1000)', context);
    assert.ok(text.includes(compact));
    assert.ok(full.includes('1970') || full.includes('1969'));
});

test('provider status distinguishes confirmed, saved and unavailable data', () => {
    const {context} = indicator(snapshot());
    assert.equal(vm.runInContext('providerState({status:"ok"}, true).label', context), 'Confirmed');
    assert.equal(vm.runInContext('providerState({status:"network",stale:true}, true).label', context), 'Saved data');
    assert.equal(vm.runInContext('providerState({status:"network"}, false).label', context), 'Unavailable');
    assert.equal(vm.runInContext('providerState({status:"ok",last_confirmed:{source:"local_log"}}, true).message', context), 'Local Codex log');
});

test('popup bounds fit the usable monitor area at different resolutions and scales', () => {
    const {context} = indicator(snapshot());
    for (const [w, h, scale, text] of [
        [1920, 1048, 1, 1], [1366, 736, 1, 1], [800, 568, 1, 1],
        [360, 608, 1, 1], [1280, 768, 2, 1], [800, 568, 2, 1.5],
    ]) {
        const layout = vm.runInContext(`popupLayout(${w},${h},${scale},${text})`, context);
        assert.ok((layout.width + 40) * scale <= w - 24);
        assert.ok(layout.maxHeight * scale <= h - 24);
    }
});

test('small monitors use a compact layout and large text receives more room when available', () => {
    const {context} = indicator(snapshot());
    assert.equal(vm.runInContext('popupLayout(360,608).compact', context), true);
    const normal = vm.runInContext('popupLayout(1920,1048,1,1).width', context);
    const large = vm.runInContext('popupLayout(1920,1048,1,1.5).width', context);
    assert.ok(large > normal);
});
