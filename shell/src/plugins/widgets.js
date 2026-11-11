// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// An add-on's Qt widgets (the plugin host's aqt.widgets), drawn in a side
// panel or an add-on window. The host has their state: this asks it for the
// tree, applies its ordered batches of changes (`shellWidgets.apply`), and
// sends it what the user does. Typing goes with the next message, or after
// a pause. Shortcuts (buttons', and `QShortcut`s) are this page's keys.
(() => {
    const setup = window.shellWidgetsSetup;
    const { root, label } = setup;
    const inWindow = !!setup.window;
    const widgets = new Map();
    // typed text the host hasn't had yet: id -> value
    const typed = new Map();
    let typedTimer = 0;
    // the tree's batch; until it's drawn, batches wait
    let seq = -1;
    let waiting = [];
    const container = document.getElementById("widgets");

    const send = (op, id, extra, cb) => {
        clearTimeout(typedTimer);
        const values = Object.fromEntries(typed);
        typed.clear();
        window.pycmd(
            "addonWidget:" + JSON.stringify({ root, op, id, values, ...extra }),
            cb,
        );
    };
    const noteTyping = (id, value) => {
        typed.set(String(id), value);
        clearTimeout(typedTimer);
        typedTimer = setTimeout(() => typed.size && send("values", 0, {}), 300);
    };

    // the host's text for a widget would undo typing it hasn't had yet
    const unsent = (id) => typed.has(String(id));

    // A label's rich text, as Qt draws it: formatting, no scripts. Parsed
    // inert, then only these tags (and http links) are kept; the panel has
    // the editor's bridge and API.
    const RICH_TAGS = new Set([
        "A",
        "B",
        "BIG",
        "BLOCKQUOTE",
        "BR",
        "CODE",
        "DIV",
        "EM",
        "FONT",
        "H1",
        "H2",
        "H3",
        "H4",
        "H5",
        "H6",
        "HR",
        "I",
        "LI",
        "OL",
        "P",
        "PRE",
        "S",
        "SMALL",
        "SPAN",
        "STRONG",
        "SUB",
        "SUP",
        "TABLE",
        "TD",
        "TH",
        "TR",
        "TT",
        "U",
        "UL",
    ]);
    function richText(html) {
        const parsed = new DOMParser().parseFromString(html, "text/html").body;
        const copy = (node) => {
            if (node.nodeType === Node.TEXT_NODE) {
                return document.createTextNode(node.textContent);
            }
            const out = document.createDocumentFragment();
            const kids = [...node.childNodes].map(copy);
            if (node.nodeType !== Node.ELEMENT_NODE || !RICH_TAGS.has(node.tagName)) {
                out.append(...kids);
                return out;
            }
            const el = document.createElement(node.tagName);
            const href = node.tagName === "A" && node.getAttribute("href");
            if (href && /^(https?|mailto):/i.test(href)) {
                el.setAttribute("href", href);
            }
            el.append(...kids);
            return el;
        };
        return copy(parsed);
    }

    // Widgets: each is { el, set(props) } plus what its ops need
    ////////////////////////////////////////////////////////////////////

    const element = (tag, className) => {
        const el = document.createElement(tag);
        if (className) {
            el.className = className;
        }
        return el;
    };

    const builders = {
        widget(node) {
            const el = element("div", "qwidget");
            if (node.layout) {
                el.append(layout(node.layout));
            }
            return { el };
        },
        window(node) {
            const el = element("div", "qwindow");
            if (node.child) {
                const child = build(node.child);
                child.classList.add("qwindow-child");
                el.append(child);
            }
            return { el };
        },
        table(node) {
            const el = element("div", "qtable");
            el.tabIndex = 0;
            const table = element("table");
            const cols = element("colgroup");
            const head = element("thead");
            const body = element("tbody");
            table.append(cols, head, body);
            el.append(table);
            let count = 0;
            let selected = -1;
            let shown = "";
            const mark = (row) => {
                selected = row;
                [...body.rows].forEach((tr, i) => tr.classList.toggle("selected", i === row));
                body.rows[row]?.scrollIntoView({ block: "nearest" });
            };
            const choose = (row, op, column = 0) => {
                if (row < 0 || row >= count) {
                    return;
                }
                mark(row);
                send(op, node.id, { value: row, column });
            };
            const at = (e) => {
                const td = e.target instanceof Element && e.target.closest("td");
                return td ? [td.parentElement.sectionRowIndex, td.cellIndex] : null;
            };
            body.addEventListener("click", (e) => {
                const cell = at(e);
                if (cell) {
                    choose(cell[0], "row", cell[1]);
                }
            });
            body.addEventListener("dblclick", (e) => {
                const cell = at(e);
                if (cell) {
                    choose(cell[0], "activate", cell[1]);
                }
            });
            el.addEventListener("keydown", (e) => {
                const step = { ArrowUp: -1, ArrowDown: 1, PageUp: -10, PageDown: 10 }[e.key];
                if (step !== undefined) {
                    e.preventDefault();
                    choose(Math.max(0, Math.min(count - 1, (selected < 0 ? -1 : selected) + step)), "row");
                } else if (e.key === "Home" || e.key === "End") {
                    e.preventDefault();
                    choose(e.key === "Home" ? 0 : count - 1, "row");
                } else if (e.key === "Enter" && selected >= 0) {
                    choose(selected, "activate");
                }
            });
            return {
                el,
                set(p) {
                    const key = JSON.stringify([p.headers, p.widths, p.rows, p.tips]);
                    if (key !== shown) {
                        shown = key;
                        const columns = Math.max(p.headers.length, ...p.rows.map((r) => r.length), 0);
                        cols.replaceChildren(
                            ...Array.from({ length: columns }, (_, i) => {
                                const col = element("col");
                                const width = p.widths[String(i)];
                                if (width !== undefined) {
                                    col.style.width = `${width}px`;
                                }
                                return col;
                            }),
                        );
                        const tr = element("tr");
                        for (let i = 0; i < columns; i++) {
                            const th = element("th");
                            th.textContent = p.headers[i] ?? "";
                            tr.append(th);
                        }
                        head.replaceChildren(tr);
                        body.replaceChildren(
                            ...p.rows.map((cells, r) => {
                                const row = element("tr");
                                cells.forEach((text, c) => {
                                    const td = element("td");
                                    td.textContent = text;
                                    td.title = p.tips?.[r]?.[c] || "";
                                    row.append(td);
                                });
                                return row;
                            }),
                        );
                        count = p.rows.length;
                        selected = -1;
                    }
                    head.hidden = !p.hheader;
                    if (p.selected !== selected) {
                        mark(p.selected);
                    }
                },
                focus: () => el.focus(),
            };
        },
        datetime(node) {
            const el = element("input", "qdatetime");
            el.type = "datetime-local";
            el.addEventListener("change", () => send("datetime", node.id, { value: el.value }));
            return {
                el,
                set(p) {
                    if (document.activeElement !== el || !el.value) {
                        el.value = p.value;
                    }
                    el.min = p.min ?? "";
                    el.disabled = !p.enabled;
                },
                focus: () => el.focus(),
            };
        },
        dock(node) {
            const el = element("div", "qdock");
            const bar = element("div", "qdock-bar");
            const title = element("span", "qdock-title");
            const close = element("button", "qdock-close");
            close.type = "button";
            close.textContent = "×";
            close.title = "Close";
            close.addEventListener("click", () => send("close", node.id, {}));
            bar.append(title, close);
            el.append(bar);
            if (node.child) {
                const child = build(node.child);
                child.classList.add("qdock-child");
                el.append(child);
            }
            return {
                el,
                set(p) {
                    title.textContent = p.title ?? "";
                },
            };
        },
        label() {
            const el = element("div", "qlabel");
            return {
                el,
                set(p) {
                    if (p.rich) {
                        el.replaceChildren(richText(p.text));
                    } else {
                        el.textContent = p.text;
                    }
                    el.classList.toggle("wrap", !!p.wrap);
                },
            };
        },
        button(node) {
            const el = element("button", "qbutton");
            el.type = "button";
            const w = {
                el,
                menu: null,
                shortcut: "",
                set(p) {
                    el.textContent = p.text;
                    el.disabled = !p.enabled;
                    el.classList.toggle("checked", !!p.checked);
                    w.menu = p.menu || null;
                    el.classList.toggle("has-menu", !!w.menu);
                    w.shortcut = p.shortcut ? normalKeys(p.shortcut) : "";
                },
                focus: () => el.focus(),
            };
            el.addEventListener("click", () => {
                if (w.menu) {
                    popupMenu(el, w.menu, (id) => send("menu", node.id, { value: id }));
                } else {
                    send("clicked", node.id, {});
                }
            });
            return w;
        },
        toolbutton(node) {
            const w = builders.button(node);
            w.el.classList.add("qtoolbutton");
            const arrows = { up: "▴", down: "▾", left: "◂", right: "▸" };
            const set = w.set;
            w.set = (p) => {
                set(p);
                if (p.arrow) {
                    w.el.textContent = arrows[p.arrow] + (p.text ? " " + p.text : "");
                }
            };
            return w;
        },
        checkbox(node) {
            const el = element("label", "qcheckbox");
            const box = element("input");
            box.type = "checkbox";
            const text = element("span");
            el.append(box, text);
            box.addEventListener("change", () => send("checked", node.id, { value: box.checked }));
            return {
                el,
                set(p) {
                    text.textContent = p.text;
                    box.checked = !!p.checked;
                    box.disabled = !p.enabled;
                },
                focus: () => box.focus(),
            };
        },
        lineedit(node) {
            const el = element("input", "qlineedit");
            el.type = "text";
            el.addEventListener("input", () => noteTyping(node.id, el.value));
            el.addEventListener("keydown", (e) => {
                if (e.key === "Enter" && !e.isComposing) {
                    send("return", node.id, {});
                }
            });
            el.addEventListener("change", () => send("finished", node.id, {}));
            return {
                el,
                set(p) {
                    if (!unsent(node.id) && el.value !== p.text) {
                        el.value = p.text;
                    }
                    el.placeholder = p.placeholder ?? "";
                    el.readOnly = !!p.readOnly;
                    el.disabled = !p.enabled;
                },
                focus: () => el.focus(),
                value: (v) => (el.value = v),
            };
        },
        combo(node) {
            const el = element("span", "qcombo");
            const select = element("select");
            const input = element("input");
            const list = element("datalist");
            list.id = `qcombo-${node.id}`;
            input.setAttribute("list", list.id);
            select.addEventListener("change", () => send("index", node.id, { value: select.selectedIndex }));
            input.addEventListener("input", () => noteTyping(node.id, input.value));
            el.append(select, input, list);
            return {
                el,
                set(p) {
                    select.hidden = !!p.editable;
                    input.hidden = !p.editable;
                    const options = p.items.map((text) => {
                        const option = element("option");
                        option.textContent = text;
                        option.value = text;
                        return option;
                    });
                    if (p.editable) {
                        list.replaceChildren(...options);
                        if (!unsent(node.id) && input.value !== p.text) {
                            input.value = p.text;
                        }
                        input.placeholder = p.placeholder ?? "";
                        input.disabled = !p.enabled;
                    } else {
                        select.replaceChildren(...options);
                        select.selectedIndex = p.index;
                        select.disabled = !p.enabled;
                    }
                },
                focus: () => (input.hidden ? select : input).focus(),
                value: (v) => (input.value = v),
            };
        },
        textedit(node) {
            const el = element("textarea", "qtextedit");
            let keys = false;
            el.addEventListener("input", () => noteTyping(node.id, el.value));
            // a subclass's keyPressEvent sees Enter, and Up/Down on the
            // first/last line, before the page acts on them; other keys
            // only tell it (Qt's base class handling is the page's)
            el.addEventListener("keydown", (e) => {
                if (!keys || e.isComposing || e.keyCode === 229) {
                    return;
                }
                if (["Shift", "Meta", "Alt", "Control", "CapsLock"].includes(e.key)) {
                    return;
                }
                const text = el.value;
                const start = el.selectionStart;
                const end = el.selectionEnd;
                const event = {
                    key: e.key,
                    shift: e.shiftKey,
                    alt: e.altKey,
                    meta: e.metaKey,
                    ctrl: e.ctrlKey,
                    text,
                    cursor: start,
                };
                typed.delete(String(node.id));
                const wanted = e.key === "Enter"
                    || (e.key === "ArrowUp" && !text.slice(0, start).includes("\n"))
                    || (e.key === "ArrowDown" && !text.slice(end).includes("\n"));
                if (!wanted) {
                    send("key", node.id, event);
                    return;
                }
                e.preventDefault();
                send("key", node.id, event, (out) => {
                    if (!out || !out.default) {
                        return;
                    }
                    if (e.key === "Enter") {
                        el.setRangeText("\n", el.selectionStart, el.selectionEnd, "end");
                        noteTyping(node.id, el.value);
                    } else if (e.key === "ArrowUp") {
                        el.setSelectionRange(0, 0);
                    } else {
                        el.setSelectionRange(el.value.length, el.value.length);
                    }
                });
            });
            return {
                el,
                set(p) {
                    if (!unsent(node.id) && el.value !== p.text) {
                        el.value = p.text;
                    }
                    el.placeholder = p.placeholder ?? "";
                    el.readOnly = !!p.readOnly;
                    el.disabled = !p.enabled;
                    keys = !!p.keys;
                },
                focus: () => el.focus(),
                setCursor: (n) => el.setSelectionRange(n, n),
                value: (v) => (el.value = v),
            };
        },
        splitter(node) {
            const el = element("div", "qsplitter");
            const panes = node.children.map((child) => {
                const pane = element("div", "qsplitter-pane");
                pane.append(build(child));
                return pane;
            });
            let vertical = false;
            panes.forEach((pane, i) => {
                if (i > 0) {
                    el.append(handle(i));
                }
                el.append(pane);
            });
            function handle(i) {
                const h = element("div", "qsplitter-handle");
                h.addEventListener("pointerdown", (down) => {
                    down.preventDefault();
                    h.setPointerCapture(down.pointerId);
                    const [a, b] = [panes[i - 1], panes[i]];
                    const size = (p) => (vertical ? p.offsetHeight : p.offsetWidth);
                    const start = vertical ? down.clientY : down.clientX;
                    const [sa, sb] = [size(a), size(b)];
                    const move = (e) => {
                        const d = (vertical ? e.clientY : e.clientX) - start;
                        const na = Math.max(20, Math.min(sa + sb - 20, sa + d));
                        a.style.flexGrow = na;
                        b.style.flexGrow = sa + sb - na;
                    };
                    const up = () => {
                        h.removeEventListener("pointermove", move);
                        h.removeEventListener("pointerup", up);
                        send("split", node.id, { value: panes.map(size) });
                    };
                    h.addEventListener("pointermove", move);
                    h.addEventListener("pointerup", up);
                });
                return h;
            }
            return {
                el,
                set(p) {
                    vertical = !!p.vertical;
                    el.classList.toggle("vertical", vertical);
                    const sizes = p.split.some((s) => s > 0) ? p.split : panes.map(() => 1);
                    panes.forEach((pane, i) => (pane.style.flexGrow = sizes[i] || 1));
                },
            };
        },
        web(node) {
            // sandboxed: the page's script talks to this one by messages
            const el = element("iframe", "qweb");
            el.sandbox = "allow-scripts";
            const w = { el, page: 0, loaded: false, queue: [] };
            el.addEventListener("load", () => {
                if (!w.page) {
                    return;
                }
                w.loaded = true;
                const queue = w.queue;
                w.queue = [];
                queue.forEach(([js, n]) => w.run(js, n));
            });
            w.run = (js, n) => el.contentWindow?.postMessage({ eval: js, n }, "*");
            w.evalJs = (js, n) => (w.loaded ? w.run(js, n) : w.queue.push([js, n]));
            w.message = (m, source) => {
                if (m.bridge !== undefined) {
                    send(
                        "bridge",
                        node.id,
                        { value: m.bridge },
                        (value) => source.postMessage({ reply: true, k: m.k, value }, "*"),
                    );
                } else if (m.n < 0) {
                    local.get(m.n)?.(m.result);
                    local.delete(m.n);
                } else if (m.n) {
                    send("evalResult", node.id, { value: m.result, n: m.n });
                } else if (m.openUrl) {
                    window.pycmd("openUrl:" + m.openUrl);
                }
            };
            w.set = (p) => {
                if (p.page && p.page !== w.page) {
                    w.page = p.page;
                    w.loaded = false;
                    // the tree's view comes with what was evaluated in it
                    w.queue = (p.replay || []).map((js) => [js, 0]);
                    const id = encodeURIComponent(`${label}-side-w${node.id}`);
                    el.src = `/_anki/legacyPageData?id=${id}&v=${p.page}`;
                }
            };
            return w;
        },
    };

    // Layouts, as Qt lays them out: widgets keep their size unless they or
    // a stretch take the room left
    ////////////////////////////////////////////////////////////////////

    const grows = new Set(["textedit", "web", "splitter", "widget", "table"]);

    function layout(l) {
        const el = element("div", `qlayout q${l.kind}`);
        const [left, top, right, bottom] = l.margins;
        el.style.padding = `${top}px ${right}px ${bottom}px ${left}px`;
        if (l.spacing !== undefined) {
            el.style.gap = `${l.spacing}px`;
        }
        if (l.kind === "form") {
            for (const row of l.rows) {
                if (row.label) {
                    const label = build(row.label);
                    label.classList.add("qform-label");
                    el.append(label);
                }
                const field = row.w ? build(row.w) : row.l ? layout(row.l) : null;
                if (field) {
                    field.classList.add(row.label ? "qform-field" : "qform-span");
                    el.append(field);
                }
            }
            return el;
        }
        const stretched = l.items.some((item) => item.stretch > 0);
        for (const item of l.items) {
            let child;
            if (item.w || item.l) {
                child = item.w ? build(item.w) : layout(item.l);
                const type = item.w ? item.w.type : "layout";
                if (item.stretch > 0) {
                    child.style.flex = `${item.stretch} 1 0`;
                } else if (!stretched && (l.kind === "h" || grows.has(type))) {
                    // Qt's size policies: in a row, controls share the
                    // room; in a column, only the expanding ones grow
                    child.style.flex = "1 1 auto";
                }
            } else if (item.space !== undefined) {
                child = element("div");
                child.style.flex = `0 0 ${item.space}px`;
            } else {
                child = element("div", "qstretch");
                child.style.flex = `${item.stretch} 1 0`;
            }
            el.append(child);
        }
        return el;
    }

    function build(node) {
        const w = (builders[node.type] || builders.widget)(node);
        w.el.dataset.widget = node.id;
        widgets.set(node.id, w);
        setProps(w, node);
        return w.el;
    }

    function setProps(w, p) {
        w.el.hidden = !!p.hidden;
        w.el.classList.toggle("disabled", p.enabled === false);
        w.el.title = p.tip ?? "";
        for (const key of ["minHeight", "maxHeight", "minWidth", "maxWidth"]) {
            const size = p.sizes?.[key];
            w.el.style[key] = size === undefined ? "" : `${size}px`;
        }
        w.set?.(p);
    }

    let rendered = false;
    function render(tree) {
        widgets.clear();
        closeMenu();
        shortcuts = (tree.shortcuts || []).map((s) => ({ id: s.id, keys: normalKeys(s.keys) }));
        container.replaceChildren(build(tree));
        // typing not sent yet stays
        for (const [id, value] of typed) {
            widgets.get(Number(id))?.value?.(value);
        }
        if (inWindow && !rendered) {
            // as Qt, a new window's focus is on its first field
            const first = [
                ...container.querySelectorAll(
                    "input:not([type=checkbox]), textarea, select, .qtable",
                ),
            ].find((el) => !el.disabled && el.checkVisibility());
            first?.focus();
        }
        rendered = true;
    }

    // Button menus
    ////////////////////////////////////////////////////////////////////

    let openMenu = null;
    function closeMenu() {
        openMenu?.remove();
        openMenu = null;
    }
    function popupMenu(anchor, items, choose) {
        closeMenu();
        const menu = element("div", "qmenu");
        for (const item of items) {
            if (item.sep) {
                menu.append(element("div", "qmenu-sep"));
                continue;
            }
            const entry = element("button", "qmenu-item");
            entry.type = "button";
            entry.textContent = item.text;
            entry.disabled = !item.enabled;
            entry.addEventListener("click", () => {
                closeMenu();
                choose(item.id);
            });
            menu.append(entry);
        }
        document.body.append(menu);
        const rect = anchor.getBoundingClientRect();
        const below = rect.bottom + menu.offsetHeight <= innerHeight;
        menu.style.left = `${Math.max(0, Math.min(rect.left, innerWidth - menu.offsetWidth))}px`;
        menu.style.top = `${below ? rect.bottom : Math.max(0, rect.top - menu.offsetHeight)}px`;
        openMenu = menu;
        menu.querySelector(".qmenu-item:not(:disabled)")?.focus();
    }
    document.addEventListener(
        "pointerdown",
        (e) => {
            if (openMenu && !openMenu.contains(e.target)) {
                closeMenu();
            }
        },
        true,
    );

    // Shortcuts, as Qt writes them ("Ctrl+Shift+A"; Qt's Ctrl is Cmd)
    ////////////////////////////////////////////////////////////////////

    let shortcuts = [];
    const MODIFIERS = ["ctrl", "meta", "alt", "shift"];
    const KEY_NAMES = { esc: "escape", return: "enter", del: "delete", " ": "space" };
    function normalKeys(keys) {
        const parts = String(keys).split("+").map((k) => k.trim().toLowerCase());
        const key = parts.pop() || "";
        const mods = MODIFIERS.filter((m) => parts.includes(m));
        return [...mods, KEY_NAMES[key] ?? key].join("+");
    }
    function eventKeys(e) {
        let key = e.key.toLowerCase();
        if (/^Key[A-Z]$/.test(e.code)) {
            key = e.code.slice(3).toLowerCase();
        } else if (/^Digit\d$/.test(e.code)) {
            key = e.code.slice(5);
        }
        const held = { ctrl: e.metaKey, meta: e.ctrlKey, alt: e.altKey, shift: e.shiftKey };
        return [...MODIFIERS.filter((m) => held[m]), KEY_NAMES[key] ?? key].join("+");
    }
    document.addEventListener(
        "keydown",
        (e) => {
            if (e.isComposing || ["Shift", "Meta", "Alt", "Control"].includes(e.key)) {
                return;
            }
            if (openMenu) {
                if (e.key === "Escape") {
                    e.preventDefault();
                    e.stopPropagation();
                    closeMenu();
                }
                return;
            }
            const keys = eventKeys(e);
            const button = [...widgets.values()].find(
                (w) => w.shortcut === keys && !w.el.disabled && w.el.checkVisibility(),
            );
            const shortcut = shortcuts.find((s) => s.keys === keys);
            if (!button && !shortcut) {
                return;
            }
            e.preventDefault();
            e.stopPropagation();
            if (button) {
                button.el.click();
            } else {
                send("shortcut", shortcut.id, {});
            }
        },
        true,
    );

    function applyOp(op) {
        if (op.tree) {
            render(op.tree);
            return;
        }
        const w = widgets.get(op.id);
        if (!w) {
            return;
        }
        if (op.props) {
            setProps(w, op.props);
        }
        if (op.eval !== undefined) {
            w.evalJs?.(op.eval, op.callback);
        }
        if (op.focus) {
            w.focus?.();
        }
        if (op.cursor !== undefined) {
            w.setCursor?.(op.cursor);
        }
    }

    // links (rich labels) open in the browser, as the bridge script does
    // for top-level pages
    document.addEventListener("click", (e) => {
        const link = e.target instanceof Element && e.target.closest("a[href]");
        if (link) {
            e.preventDefault();
            window.pycmd("openUrl:" + link.href);
        }
    });

    window.addEventListener("message", (e) => {
        const w = widgets.get(e.data?.shellWidget);
        if (w?.message && e.source === w.el.contentWindow) {
            w.message(e.data, e.source);
        }
    });

    // evals answered here, not by the add-on (checks of a web view)
    const local = new Map();
    let localIds = 0;

    window.shellWidgets = {
        root,
        evalIn(id, js) {
            const w = widgets.get(id);
            if (!w?.run || !w.loaded) {
                return Promise.resolve(null);
            }
            const n = --localIds;
            return new Promise((resolve) => {
                local.set(n, resolve);
                w.run(js, n);
            });
        },
        apply(update) {
            if (seq < 0) {
                waiting.push(update);
                return;
            }
            if (update.seq <= seq) {
                return;
            }
            update.ops.forEach(applyOp);
            seq = update.seq;
        },
    };

    send("tree", 0, {}, (out) => {
        if (!out) {
            return;
        }
        seq = out.seq;
        render(out.tree);
        const batches = waiting;
        waiting = [];
        batches.forEach(window.shellWidgets.apply);
    });
})();
