const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function elementStub() {
    return {
        addEventListener() {},
        appendChild() {},
        classList: { add() {}, remove() {}, toggle() {} },
        click() {},
        focus() {},
        querySelector() { return elementStub(); },
        querySelectorAll() { return []; },
        remove() {},
        removeAttribute() {},
        setAttribute() {},
        style: {},
        value: "",
    };
}

function loadFrontend() {
    const elements = new Map();
    const document = {
        createElement() { return elementStub(); },
        getElementById(id) {
            if (!elements.has(id)) elements.set(id, elementStub());
            return elements.get(id);
        },
        querySelector(selector) {
            if (!elements.has(selector)) elements.set(selector, elementStub());
            return elements.get(selector);
        },
    };
    const context = {
        URL,
        alert() {},
        console: { error() {}, log() {}, warn() {} },
        document,
        fetch() { return new Promise(() => {}); },
        localStorage: {
            getItem() { return ""; },
            removeItem() {},
            setItem() {},
        },
        navigator: {},
        setTimeout,
        window: {
            location: { origin: "https://askdorjee.example" },
            open() {},
        },
    };
    vm.createContext(context);
    const source = fs.readFileSync(path.join(__dirname, "static", "app.js"), "utf8");
    vm.runInContext(source, context, { filename: "static/app.js" });
    return context;
}

const dar = {
    label: "Dharamsala Animal Rescue Trust",
    url: "https://dharamsalaanimalrescue.org/",
    phone: "+91 98828 58631",
    address: "Slate Godam Road, VPO Rakkar, Dharamshala, Himachal Pradesh 176057",
};

const duplicateResponse = [
    "**Verified animal-rescue options**",
    "1. **Dharamsala Animal Rescue Trust** - Dharamshala, Himachal Pradesh. " +
        "[Source/contact](https://dharamsalaanimalrescue.org/). " +
        "Address: Slate Godam Road, VPO Rakkar, Dharamshala, Himachal Pradesh 176057. " +
        "Phone: \\+91 98828 58631.",
].join("\n");

test("keeps the AI disclaimer inside the chat widget", () => {
    const html = fs.readFileSync(path.join(__dirname, "static", "index.html"), "utf8");
    const chatStart = html.indexOf('<div class="chat-container hidden">');
    const inputStart = html.indexOf('<div class="input-area">', chatStart);
    const disclaimer = html.indexOf(
        "Ask Dorjee is powered by AI and may make mistakes. Please verify important information.",
        inputStart
    );
    const siteFooter = html.indexOf('<footer class="site-footer">');

    assert.notEqual(chatStart, -1);
    assert.notEqual(inputStart, -1);
    assert.notEqual(disclaimer, -1);
    assert.notEqual(siteFooter, -1);
    assert.ok(chatStart < inputStart);
    assert.ok(inputStart < disclaimer);
    assert.ok(disclaimer < siteFooter);
    assert.equal((html.match(/class="status-bar"/g) || []).length, 1);
});

test("renders ordinary and angle-wrapped source citations as safe links", () => {
    const frontend = loadFrontend();
    for (const destination of [
        "https://example.org/college?campus=1&service=vet",
        "<https://example.org/college?campus=1&service=vet>",
    ]) {
        assert.equal(
            frontend.renderMarkdown("[Source](" + destination + ")"),
            '<a href="https://example.org/college?campus=1&amp;service=vet" ' +
                'target="_blank" rel="noopener noreferrer">Source</a>'
        );
    }
    assert.match(
        frontend.renderMarkdown("[Source](<http://example.org/college_(campus)>)"),
        /href="http:\/\/example\.org\/college_\(campus\)"/
    );
});

test("citation rendering preserves HTML escaping and rejects unsafe schemes", () => {
    const frontend = loadFrontend();
    const escaped = frontend.renderMarkdown(
        '[<img src=x onerror="alert(1)">](<https://example.org/?q="unsafe">)'
    );
    assert.doesNotMatch(escaped, /<img\b|href="[^"]*"unsafe/);
    assert.match(escaped, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;/);
    assert.match(escaped, /href="https:\/\/example\.org\/\?q=&quot;unsafe&quot;"/);
    for (const destination of ["javascript:alert(1)", "<javascript:alert(1)>", "<data:text/html,unsafe>"]) {
        assert.doesNotMatch(frontend.renderMarkdown("[Source](" + destination + ")"), /<a\b/);
    }
});

test("suppresses a resource card whose exact contact is already in the response", () => {
    const frontend = loadFrontend();
    assert.equal(frontend.renderResourceLinks([dar], duplicateResponse), "");
});

test("keeps a card when it contributes a missing contact detail", () => {
    const frontend = loadFrontend();
    const responseWithoutPhone = duplicateResponse.replace(/ Phone:.*$/, "");
    const rendered = frontend.renderResourceLinks([dar], responseWithoutPhone);
    assert.match(rendered, /Dharamsala Animal Rescue Trust/);
    assert.match(rendered, /Phone: \+91 98828 58631/);
});

test("filters only the duplicated resource and preserves other rescue contacts", () => {
    const frontend = loadFrontend();
    const rendered = frontend.renderResourceLinks(
        [
            dar,
            {
                label: "Manali Strays",
                url: "https://manalistrays.org/",
                phone: "+91 94187 04924",
            },
        ],
        duplicateResponse
    );
    assert.doesNotMatch(rendered, /Dharamsala Animal Rescue Trust/);
    assert.match(rendered, /Manali Strays/);
    assert.match(rendered, /94187 04924/);
});

test("does not suppress a same-named resource with a different URL", () => {
    const frontend = loadFrontend();
    const rendered = frontend.renderResourceLinks(
        [{ ...dar, url: "https://dharamsalaanimalrescue.org/contact/" }],
        duplicateResponse
    );
    assert.match(rendered, /resource-card/);
});

test("does not borrow contact details from another organization", () => {
    const frontend = loadFrontend();
    const splitDetails = [
        "1. **Dharamsala Animal Rescue Trust** " +
            "[Source/contact](https://dharamsalaanimalrescue.org/). " +
            "Address: Slate Godam Road, VPO Rakkar, Dharamshala, Himachal Pradesh 176057.",
        "2. **Another Rescue** Phone: +91 98828 58631.",
    ].join("\n");
    const rendered = frontend.renderResourceLinks([dar], splitDetails);
    assert.match(rendered, /resource-card/);
    assert.match(rendered, /Phone: \+91 98828 58631/);
});
