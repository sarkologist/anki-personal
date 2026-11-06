// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// Renders the form spec Python sends; the protocol is described in panels.py.
// Every string is set as textContent or a property, never parsed as markup; only
// a frame's html, the add-on's own charts, is a document, in a scriptless iframe.
"use strict";

(function() {
    const root = document.getElementById("root");
    // name -> {get(), set(props)}
    const fields = new Map();
    // Fields that take a "label" prop show it in a row beside the control.
    const LABELLED = new Set(["input", "select", "checklist", "list", "readonly", "textarea"]);
    let submit = "";
    let ids = 0;

    function post(message) {
        window.pycmd(JSON.stringify(message), apply);
    }

    function apply(reply) {
        if (reply && typeof reply === "object" && reply.type) {
            receive(reply);
        }
    }

    function values() {
        const result = {};
        for (const [name, field] of fields) {
            if (field.get) {
                result[name] = field.get();
            }
        }
        return result;
    }

    function act(name) {
        post({ type: "action", name, values: values() });
    }

    function changed(name) {
        post({ type: "change", name, values: values() });
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) {
            node.className = className;
        }
        if (text !== undefined) {
            node.textContent = String(text);
        }
        return node;
    }

    function same(a, b) {
        return JSON.stringify(a) === JSON.stringify(b);
    }

    function sized(node, spec) {
        if (spec.height) {
            node.style.height = `${spec.height}px`;
        }
    }

    const builders = {
        text(spec) {
            const node = el("p", "text");
            return {
                node,
                set(props) {
                    if ("text" in props) {
                        node.textContent = props.text;
                    }
                },
            };
        },

        checkbox(spec) {
            const node = el("label", "check");
            const box = el("input");
            box.type = "checkbox";
            const label = el("span");
            node.append(box, label);
            if (spec.notify) {
                box.addEventListener("change", () => changed(spec.name));
            }
            return {
                node,
                control: box,
                get: () => box.checked,
                set(props) {
                    if ("label" in props) {
                        label.textContent = props.label;
                    }
                    if ("value" in props) {
                        box.checked = Boolean(props.value);
                    }
                },
            };
        },

        input(spec) {
            const input = el("input");
            input.type = "text";
            input.id = `field-${ids++}`;
            const list = el("datalist");
            list.id = `${input.id}-items`;
            const node = el("div");
            node.append(input, list);
            input.addEventListener("keydown", (event) => {
                if (event.key === "Enter" && submit) {
                    event.preventDefault();
                    act(submit);
                }
            });
            return {
                node,
                control: input,
                get: () => input.value,
                set(props) {
                    if ("items" in props) {
                        // An editable combo box: free text with suggestions.
                        list.replaceChildren(
                            ...props.items.map((item) => {
                                const option = el("option");
                                option.value = item.label;
                                return option;
                            }),
                        );
                        input.setAttribute("list", list.id);
                    }
                    if ("value" in props) {
                        input.value = props.value;
                    }
                    if ("placeholder" in props) {
                        input.placeholder = props.placeholder;
                    }
                },
            };
        },

        textarea(spec) {
            const node = el("textarea");
            node.id = `field-${ids++}`;
            node.rows = spec.rows || 3;
            sized(node, spec);
            return {
                node,
                control: node,
                get: () => node.value,
                set(props) {
                    if ("value" in props) {
                        node.value = props.value;
                    }
                    if ("placeholder" in props) {
                        node.placeholder = props.placeholder;
                    }
                },
            };
        },

        readonly(spec) {
            const node = el("textarea");
            node.id = `field-${ids++}`;
            node.readOnly = true;
            sized(node, spec);
            return {
                node,
                control: node,
                set(props) {
                    if ("value" in props) {
                        const top = node.scrollTop;
                        node.value = props.value;
                        node.scrollTop = top;
                    }
                },
            };
        },

        select(spec) {
            const node = el("select");
            node.id = `field-${ids++}`;
            let items = [];
            if (spec.notify) {
                node.addEventListener("change", () => changed(spec.name));
            }
            const get = () => (items[node.selectedIndex] || { value: null }).value;
            const choose = (value) => {
                node.selectedIndex = items.findIndex((item) => same(item.value, value));
            };
            return {
                node,
                control: node,
                get,
                set(props) {
                    if ("items" in props) {
                        const current = get();
                        items = props.items;
                        node.replaceChildren(...items.map((item) => el("option", "", item.label)));
                        choose(current);
                        if (node.selectedIndex < 0 && items.length) {
                            node.selectedIndex = 0;
                        }
                    }
                    if ("value" in props) {
                        choose(props.value);
                    }
                },
            };
        },

        checklist(spec) {
            const node = el("div", "listbox");
            sized(node, spec);
            let boxes = [];
            return {
                node,
                get: () => boxes.filter(([box]) => box.checked).map(([, value]) => value),
                set(props) {
                    if ("items" in props) {
                        boxes = props.items.map((item) => {
                            const row = el("label", "check");
                            const box = el("input");
                            box.type = "checkbox";
                            row.append(box, el("span", "", item.label));
                            return [box, item.value, row];
                        });
                        node.replaceChildren(...boxes.map(([, , row]) => row));
                    }
                    if ("value" in props) {
                        for (const [box, value] of boxes) {
                            box.checked = props.value.some((chosen) => same(chosen, value));
                        }
                    }
                },
            };
        },

        list(spec) {
            const node = el("div", "listbox");
            node.tabIndex = 0;
            node.setAttribute("role", "listbox");
            sized(node, spec);
            let items = [];
            let rows = [];
            let chosen = new Set();
            let anchor = -1;
            const show = () => rows.forEach((row, i) => row.classList.toggle("selected", chosen.has(i)));
            const pick = (index, event) => {
                if (items[index].disabled) {
                    return;
                }
                if (spec.multiple && event && event.shiftKey && anchor >= 0) {
                    chosen = new Set();
                    for (let i = Math.min(anchor, index); i <= Math.max(anchor, index); i++) {
                        if (!items[i].disabled) {
                            chosen.add(i);
                        }
                    }
                } else if (spec.multiple && event && (event.metaKey || event.ctrlKey)) {
                    chosen.has(index) ? chosen.delete(index) : chosen.add(index);
                    anchor = index;
                } else {
                    chosen = new Set([index]);
                    anchor = index;
                }
                show();
                if (spec.notify) {
                    changed(spec.name);
                }
            };
            node.addEventListener("keydown", (event) => {
                const step = { ArrowDown: 1, ArrowUp: -1 }[event.key];
                if (step && items.length) {
                    event.preventDefault();
                    const next = Math.min(items.length - 1, Math.max(0, anchor + step));
                    pick(next, null);
                    rows[next].scrollIntoView({ block: "nearest" });
                } else if (event.key === "Enter" && spec.activate && chosen.size) {
                    act(spec.activate);
                }
            });
            const selected = () => [...chosen].sort((a, b) => a - b).map((i) => items[i].value);
            return {
                node,
                get: () => (spec.multiple ? selected() : selected()[0] ?? null),
                set(props) {
                    if ("items" in props) {
                        items = props.items;
                        chosen = new Set();
                        anchor = -1;
                        rows = items.map((item, index) => {
                            const row = el("div", item.disabled ? "option disabled" : "option", item.label);
                            row.setAttribute("role", "option");
                            if (item.title) {
                                row.title = item.title;
                            }
                            row.addEventListener("mousedown", (event) => pick(index, event));
                            if (spec.activate) {
                                row.addEventListener("dblclick", () => act(spec.activate));
                            }
                            return row;
                        });
                        node.replaceChildren(...rows);
                    }
                    if ("value" in props) {
                        const wanted = spec.multiple ? props.value : [props.value];
                        chosen = new Set(
                            items.flatMap((item, i) => (wanted.some((v) => same(v, item.value)) ? [i] : [])),
                        );
                        show();
                    }
                },
            };
        },

        frame(spec) {
            const node = el("iframe");
            // No scripts run inside; same origin only lets the page keep its scroll.
            node.setAttribute("sandbox", "allow-same-origin");
            sized(node, spec);
            return {
                node,
                set(props) {
                    if ("html" in props) {
                        let y = 0;
                        try {
                            y = node.contentWindow.scrollY;
                        } catch (_error) {}
                        node.onload = () => {
                            try {
                                node.contentWindow.scrollTo(0, y);
                            } catch (_error) {}
                        };
                        node.srcdoc = props.html;
                    }
                },
            };
        },

        button(spec) {
            const node = el("button");
            node.type = "button";
            node.addEventListener("click", () => act(spec.action || spec.name));
            return {
                node,
                control: node,
                set(props) {
                    if ("label" in props) {
                        node.textContent = props.label;
                    }
                },
            };
        },

        row(spec) {
            const node = el("div", spec.align === "end" ? "row end" : "row");
            node.append(...spec.body.map(build));
            return { node };
        },

        tabs(spec) {
            const node = el("div", "tabs");
            const bar = el("div", "bar");
            const tabs = spec.tabs.map((tab) => el("button", "tab", tab.label));
            const panes = spec.tabs.map((tab) => {
                const pane = el("div", "pane");
                pane.append(...tab.body.map(build));
                return pane;
            });
            const choose = (index) => {
                tabs.forEach((tab, i) => tab.classList.toggle("current", i === index));
                panes.forEach((pane, i) => (pane.hidden = i !== index));
            };
            tabs.forEach((tab, i) => {
                tab.type = "button";
                tab.addEventListener("click", () => choose(i));
            });
            bar.append(...tabs, el("div", "spacer"), ...(spec.corner || []).map(build));
            node.append(bar, ...panes);
            choose(0);
            return { node };
        },
    };

    function build(spec) {
        const field = builders[spec.kind](spec);
        let node = field.node;
        let label = null;
        if (LABELLED.has(spec.kind) && spec.label !== undefined) {
            label = el("label", "", spec.label);
            if (field.control) {
                label.htmlFor = field.control.id;
            }
            const row = el("div", "field");
            row.append(label, node);
            node = row;
        }
        const own = field.set || (() => {});
        field.set = (props) => {
            if ("hidden" in props) {
                node.hidden = Boolean(props.hidden);
            }
            if ("disabled" in props && field.control) {
                field.control.disabled = Boolean(props.disabled);
            }
            if ("tooltip" in props) {
                (field.control || field.node).title = props.tooltip || "";
            }
            if ("label" in props && label) {
                label.textContent = props.label;
            }
            own(props);
        };
        field.set(spec);
        if (spec.name) {
            fields.set(spec.name, field);
        }
        return node;
    }

    function receive(message) {
        if (message.type === "render") {
            fields.clear();
            submit = message.spec.submit || "";
            root.replaceChildren(...message.spec.body.map(build));
        } else if (message.type === "update") {
            for (const [name, props] of Object.entries(message.fields)) {
                const field = fields.get(name);
                if (field) {
                    field.set(props);
                }
            }
        }
    }

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            post({ type: "close" });
        }
    });

    // Qt Anki defines pycmd once its web channel connects, after the page starts.
    function start() {
        if (typeof window.pycmd !== "function") {
            setTimeout(start, 20);
            return;
        }
        window.pycmd(JSON.stringify({ type: "ready" }), (reply) => {
            if (!reply) {
                // the add-on was busy (the shell gave up waiting); ask again
                setTimeout(start, 500);
                return;
            }
            document.documentElement.classList.toggle("night", Boolean(reply && reply.dark));
            for (const message of (reply && reply.messages) || []) {
                receive(message);
            }
        });
    }

    window.frontierPanel = { receive };
    start();
})();
