"use strict";
(() => {
    var T = typeof performance == "object" && performance && typeof performance.now == "function" ? performance : Date,
        k = new Set(),
        x = typeof process == "object" && process ? process : {},
        I = (h, t, e, i) => {
            typeof x.emitWarning == "function" ? x.emitWarning(h, t, e, i) : console.error(`[${e}] ${t}: ${h}`);
        },
        C = globalThis.AbortController,
        W = globalThis.AbortSignal;
    if (typeof C > "u") {
        W = class {
            onabort;
            _onabort = [];
            reason;
            aborted = !1;
            addEventListener(i, s) {
                this._onabort.push(s);
            }
        },
            C = class {
                constructor() {
                    t();
                }
                signal = new W();
                abort(i) {
                    if (!this.signal.aborted) {
                        this.signal.reason = i, this.signal.aborted = !0;
                        for (let s of this.signal._onabort) { s(i); }
                        this.signal.onabort?.(i);
                    }
                }
            };
        let h = x.env?.LRU_CACHE_IGNORE_AC_WARNING !== "1",
            t = () => {
                h && (h = !1,
                    I(
                        "AbortController is not defined. If using lru-cache in node 14, load an AbortController polyfill from the `node-abort-controller` package. A minimal polyfill is provided for use by LRUCache.fetch(), but it should not be relied upon in other contexts (eg, passing it to other APIs that use AbortController/AbortSignal might have undesirable effects). You may disable this with LRU_CACHE_IGNORE_AC_WARNING=1 in the env.",
                        "NO_ABORT_CONTROLLER",
                        "ENOTSUP",
                        t,
                    ));
            };
    }
    var B = h => !k.has(h);
    var A = h => h && h === Math.floor(h) && h > 0 && isFinite(h),
        N = h =>
            A(h)
                ? h <= Math.pow(2, 8)
                    ? Uint8Array
                    : h <= Math.pow(2, 16)
                    ? Uint16Array
                    : h <= Math.pow(2, 32)
                    ? Uint32Array
                    : h <= Number.MAX_SAFE_INTEGER
                    ? F
                    : null
                : null,
        F = class extends Array {
            constructor(t) {
                super(t), this.fill(0);
            }
        },
        L = class h {
            heap;
            length;
            static #a = !1;
            static create(t) {
                let e = N(t);
                if (!e) { return []; }
                h.#a = !0;
                let i = new h(t, e);
                return h.#a = !1, i;
            }
            constructor(t, e) {
                if (!h.#a) { throw new TypeError("instantiate Stack using Stack.create(n)"); }
                this.heap = new e(t), this.length = 0;
            }
            push(t) {
                this.heap[this.length++] = t;
            }
            pop() {
                return this.heap[--this.length];
            }
        },
        z = class h {
            #a;
            #c;
            #p;
            #m;
            #R;
            #x;
            ttl;
            ttlResolution;
            ttlAutopurge;
            updateAgeOnGet;
            updateAgeOnHas;
            allowStale;
            noDisposeOnSet;
            noUpdateTTL;
            maxEntrySize;
            sizeCalculation;
            noDeleteOnFetchRejection;
            noDeleteOnStaleGet;
            allowStaleOnFetchAbort;
            allowStaleOnFetchRejection;
            ignoreFetchAbort;
            #n;
            #w;
            #s;
            #i;
            #t;
            #l;
            #u;
            #r;
            #h;
            #S;
            #o;
            #y;
            #b;
            #d;
            #_;
            #v;
            #f;
            static unsafeExposeInternals(t) {
                return {
                    starts: t.#b,
                    ttls: t.#d,
                    sizes: t.#y,
                    keyMap: t.#s,
                    keyList: t.#i,
                    valList: t.#t,
                    next: t.#l,
                    prev: t.#u,
                    get head() {
                        return t.#r;
                    },
                    get tail() {
                        return t.#h;
                    },
                    free: t.#S,
                    isBackgroundFetch: e => t.#e(e),
                    backgroundFetch: (e, i, s, n) => t.#M(e, i, s, n),
                    moveToTail: e => t.#z(e),
                    indexes: e => t.#A(e),
                    rindexes: e => t.#E(e),
                    isStale: e => t.#g(e),
                };
            }
            get max() {
                return this.#a;
            }
            get maxSize() {
                return this.#c;
            }
            get calculatedSize() {
                return this.#w;
            }
            get size() {
                return this.#n;
            }
            get fetchMethod() {
                return this.#R;
            }
            get memoMethod() {
                return this.#x;
            }
            get dispose() {
                return this.#p;
            }
            get disposeAfter() {
                return this.#m;
            }
            constructor(t) {
                let {
                    max: e = 0,
                    ttl: i,
                    ttlResolution: s = 1,
                    ttlAutopurge: n,
                    updateAgeOnGet: o,
                    updateAgeOnHas: a,
                    allowStale: r,
                    dispose: g,
                    disposeAfter: y,
                    noDisposeOnSet: f,
                    noUpdateTTL: u,
                    maxSize: c = 0,
                    maxEntrySize: E = 0,
                    sizeCalculation: d,
                    fetchMethod: w,
                    memoMethod: l,
                    noDeleteOnFetchRejection: m,
                    noDeleteOnStaleGet: b,
                    allowStaleOnFetchRejection: p,
                    allowStaleOnFetchAbort: S,
                    ignoreFetchAbort: v,
                } = t;
                if (e !== 0 && !A(e)) { throw new TypeError("max option must be a nonnegative integer"); }
                let _ = e ? N(e) : Array;
                if (!_) { throw new Error("invalid max value: " + e); }
                if (
                    this.#a = e,
                        this.#c = c,
                        this.maxEntrySize = E || this.#c,
                        this.sizeCalculation = d,
                        this.sizeCalculation
                ) {
                    if (!this.#c && !this.maxEntrySize) {
                        throw new TypeError("cannot set sizeCalculation without setting maxSize or maxEntrySize");
                    }
                    if (typeof this.sizeCalculation != "function") {
                        throw new TypeError("sizeCalculation set to non-function");
                    }
                }
                if (l !== void 0 && typeof l != "function") {
                    throw new TypeError("memoMethod must be a function if defined");
                }
                if (this.#x = l, w !== void 0 && typeof w != "function") {
                    throw new TypeError("fetchMethod must be a function if specified");
                }
                if (
                    this.#R = w,
                        this.#v = !!w,
                        this.#s = new Map(),
                        this.#i = new Array(e).fill(void 0),
                        this.#t = new Array(e).fill(void 0),
                        this.#l = new _(e),
                        this.#u = new _(e),
                        this.#r = 0,
                        this.#h = 0,
                        this.#S = L.create(e),
                        this.#n = 0,
                        this.#w = 0,
                        typeof g == "function" && (this.#p = g),
                        typeof y == "function" ? (this.#m = y, this.#o = []) : (this.#m = void 0, this.#o = void 0),
                        this.#_ = !!this.#p,
                        this.#f = !!this.#m,
                        this.noDisposeOnSet = !!f,
                        this.noUpdateTTL = !!u,
                        this.noDeleteOnFetchRejection = !!m,
                        this.allowStaleOnFetchRejection = !!p,
                        this.allowStaleOnFetchAbort = !!S,
                        this.ignoreFetchAbort = !!v,
                        this.maxEntrySize !== 0
                ) {
                    if (this.#c !== 0 && !A(this.#c)) {
                        throw new TypeError("maxSize must be a positive integer if specified");
                    }
                    if (!A(this.maxEntrySize)) {
                        throw new TypeError("maxEntrySize must be a positive integer if specified");
                    }
                    this.#G();
                }
                if (
                    this.allowStale = !!r,
                        this.noDeleteOnStaleGet = !!b,
                        this.updateAgeOnGet = !!o,
                        this.updateAgeOnHas = !!a,
                        this.ttlResolution = A(s) || s === 0 ? s : 1,
                        this.ttlAutopurge = !!n,
                        this.ttl = i || 0,
                        this.ttl
                ) {
                    if (!A(this.ttl)) { throw new TypeError("ttl must be a positive integer if specified"); }
                    this.#W();
                }
                if (this.#a === 0 && this.ttl === 0 && this.#c === 0) {
                    throw new TypeError("At least one of max, maxSize, or ttl is required");
                }
                if (!this.ttlAutopurge && !this.#a && !this.#c) {
                    let O = "LRU_CACHE_UNBOUNDED";
                    B(O)
                        && (k.add(O),
                            I(
                                "TTL caching without ttlAutopurge, max, or maxSize can result in unbounded memory consumption.",
                                "UnboundedCacheWarning",
                                O,
                                h,
                            ));
                }
            }
            getRemainingTTL(t) {
                return this.#s.has(t) ? 1 / 0 : 0;
            }
            #W() {
                let t = new F(this.#a), e = new F(this.#a);
                this.#d = t,
                    this.#b = e,
                    this.#k = (n, o, a = T.now()) => {
                        if (e[n] = o !== 0 ? a : 0, t[n] = o, o !== 0 && this.ttlAutopurge) {
                            let r = setTimeout(() => {
                                this.#g(n) && this.#T(this.#i[n], "expire");
                            }, o + 1);
                            r.unref && r.unref();
                        }
                    },
                    this.#O = n => {
                        e[n] = t[n] !== 0 ? T.now() : 0;
                    },
                    this.#F = (n, o) => {
                        if (t[o]) {
                            let a = t[o], r = e[o];
                            if (!a || !r) { return; }
                            n.ttl = a, n.start = r, n.now = i || s();
                            let g = n.now - r;
                            n.remainingTTL = a - g;
                        }
                    };
                let i = 0,
                    s = () => {
                        let n = T.now();
                        if (this.ttlResolution > 0) {
                            i = n;
                            let o = setTimeout(() => i = 0, this.ttlResolution);
                            o.unref && o.unref();
                        }
                        return n;
                    };
                this.getRemainingTTL = n => {
                    let o = this.#s.get(n);
                    if (o === void 0) { return 0; }
                    let a = t[o], r = e[o];
                    if (!a || !r) { return 1 / 0; }
                    let g = (i || s()) - r;
                    return a - g;
                },
                    this.#g = n => {
                        let o = e[n], a = t[n];
                        return !!a && !!o && (i || s()) - o > a;
                    };
            }
            #O = () => {};
            #F = () => {};
            #k = () => {};
            #g = () => !1;
            #G() {
                let t = new F(this.#a);
                this.#w = 0,
                    this.#y = t,
                    this.#C = e => {
                        this.#w -= t[e], t[e] = 0;
                    },
                    this.#I = (e, i, s, n) => {
                        if (this.#e(i)) { return 0; }
                        if (!A(s)) {
                            if (n) {
                                if (typeof n != "function") {throw new TypeError(
                                        "sizeCalculation must be a function",
                                    );}
                                if (s = n(i, e), !A(s)) {
                                    throw new TypeError(
                                        "sizeCalculation return invalid (expect positive integer)",
                                    );
                                }
                            } else {throw new TypeError(
                                    "invalid size value (must be positive integer). When maxSize or maxEntrySize is used, sizeCalculation or size must be set.",
                                );}
                        }
                        return s;
                    },
                    this.#L = (e, i, s) => {
                        if (t[e] = i, this.#c) {
                            let n = this.#c - t[e];
                            for (; this.#w > n;) { this.#D(!0); }
                        }
                        this.#w += t[e], s && (s.entrySize = i, s.totalCalculatedSize = this.#w);
                    };
            }
            #C = t => {};
            #L = (t, e, i) => {};
            #I = (t, e, i, s) => {
                if (i || s) { throw new TypeError("cannot set size without setting maxSize or maxEntrySize on cache"); }
                return 0;
            };
            *#A({ allowStale: t = this.allowStale } = {}) {
                if (this.#n) {
                    for (let e = this.#h; !(!this.#N(e) || ((t || !this.#g(e)) && (yield e), e === this.#r));) {
                        e = this.#u[e];
                    }
                }
            }
            *#E({ allowStale: t = this.allowStale } = {}) {
                if (this.#n) {
                    for (let e = this.#r; !(!this.#N(e) || ((t || !this.#g(e)) && (yield e), e === this.#h));) {
                        e = this.#l[e];
                    }
                }
            }
            #N(t) {
                return t !== void 0 && this.#s.get(this.#i[t]) === t;
            }
            *entries() {
                for (let t of this.#A()) {
                    this.#t[t] !== void 0 && this.#i[t] !== void 0 && !this.#e(this.#t[t])
                        && (yield [this.#i[t], this.#t[t]]);
                }
            }
            *rentries() {
                for (let t of this.#E()) {
                    this.#t[t] !== void 0 && this.#i[t] !== void 0 && !this.#e(this.#t[t])
                        && (yield [this.#i[t], this.#t[t]]);
                }
            }
            *keys() {
                for (let t of this.#A()) {
                    let e = this.#i[t];
                    e !== void 0 && !this.#e(this.#t[t]) && (yield e);
                }
            }
            *rkeys() {
                for (let t of this.#E()) {
                    let e = this.#i[t];
                    e !== void 0 && !this.#e(this.#t[t]) && (yield e);
                }
            }
            *values() {
                for (let t of this.#A()) { this.#t[t] !== void 0 && !this.#e(this.#t[t]) && (yield this.#t[t]); }
            }
            *rvalues() {
                for (let t of this.#E()) { this.#t[t] !== void 0 && !this.#e(this.#t[t]) && (yield this.#t[t]); }
            }
            [Symbol.iterator]() {
                return this.entries();
            }
            [Symbol.toStringTag] = "LRUCache";
            find(t, e = {}) {
                for (let i of this.#A()) {
                    let s = this.#t[i], n = this.#e(s) ? s.__staleWhileFetching : s;
                    if (n !== void 0 && t(n, this.#i[i], this)) { return this.get(this.#i[i], e); }
                }
            }
            forEach(t, e = this) {
                for (let i of this.#A()) {
                    let s = this.#t[i], n = this.#e(s) ? s.__staleWhileFetching : s;
                    n !== void 0 && t.call(e, n, this.#i[i], this);
                }
            }
            rforEach(t, e = this) {
                for (let i of this.#E()) {
                    let s = this.#t[i], n = this.#e(s) ? s.__staleWhileFetching : s;
                    n !== void 0 && t.call(e, n, this.#i[i], this);
                }
            }
            purgeStale() {
                let t = !1;
                for (let e of this.#E({ allowStale: !0 })) { this.#g(e) && (this.#T(this.#i[e], "expire"), t = !0); }
                return t;
            }
            info(t) {
                let e = this.#s.get(t);
                if (e === void 0) { return; }
                let i = this.#t[e], s = this.#e(i) ? i.__staleWhileFetching : i;
                if (s === void 0) { return; }
                let n = { value: s };
                if (this.#d && this.#b) {
                    let o = this.#d[e], a = this.#b[e];
                    if (o && a) {
                        let r = o - (T.now() - a);
                        n.ttl = r, n.start = Date.now();
                    }
                }
                return this.#y && (n.size = this.#y[e]), n;
            }
            dump() {
                let t = [];
                for (let e of this.#A({ allowStale: !0 })) {
                    let i = this.#i[e], s = this.#t[e], n = this.#e(s) ? s.__staleWhileFetching : s;
                    if (n === void 0 || i === void 0) { continue; }
                    let o = { value: n };
                    if (this.#d && this.#b) {
                        o.ttl = this.#d[e];
                        let a = T.now() - this.#b[e];
                        o.start = Math.floor(Date.now() - a);
                    }
                    this.#y && (o.size = this.#y[e]), t.unshift([i, o]);
                }
                return t;
            }
            load(t) {
                this.clear();
                for (let [e, i] of t) {
                    if (i.start) {
                        let s = Date.now() - i.start;
                        i.start = T.now() - s;
                    }
                    this.set(e, i.value, i);
                }
            }
            set(t, e, i = {}) {
                if (e === void 0) { return this.delete(t), this; }
                let {
                        ttl: s = this.ttl,
                        start: n,
                        noDisposeOnSet: o = this.noDisposeOnSet,
                        sizeCalculation: a = this.sizeCalculation,
                        status: r,
                    } = i,
                    { noUpdateTTL: g = this.noUpdateTTL } = i,
                    y = this.#I(t, e, i.size || 0, a);
                if (this.maxEntrySize && y > this.maxEntrySize) {
                    return r && (r.set = "miss", r.maxEntrySizeExceeded = !0), this.#T(t, "set"), this;
                }
                let f = this.#n === 0 ? void 0 : this.#s.get(t);
                if (f === void 0) {
                    f = this.#n === 0
                        ? this.#h
                        : this.#S.length !== 0
                        ? this.#S.pop()
                        : this.#n === this.#a
                        ? this.#D(!1)
                        : this.#n,
                        this.#i[f] = t,
                        this.#t[f] = e,
                        this.#s.set(t, f),
                        this.#l[this.#h] = f,
                        this.#u[f] = this.#h,
                        this.#h = f,
                        this.#n++,
                        this.#L(f, y, r),
                        r && (r.set = "add"),
                        g = !1;
                } else {
                    this.#z(f);
                    let u = this.#t[f];
                    if (e !== u) {
                        if (this.#v && this.#e(u)) {
                            u.__abortController.abort(new Error("replaced"));
                            let { __staleWhileFetching: c } = u;
                            c !== void 0 && !o
                                && (this.#_ && this.#p?.(c, t, "set"), this.#f && this.#o?.push([c, t, "set"]));
                        } else { o || (this.#_ && this.#p?.(u, t, "set"), this.#f && this.#o?.push([u, t, "set"])); }
                        if (this.#C(f), this.#L(f, y, r), this.#t[f] = e, r) {
                            r.set = "replace";
                            let c = u && this.#e(u) ? u.__staleWhileFetching : u;
                            c !== void 0 && (r.oldValue = c);
                        }
                    } else { r && (r.set = "update"); }
                }
                if (
                    s !== 0 && !this.#d && this.#W(),
                        this.#d && (g || this.#k(f, s, n), r && this.#F(r, f)),
                        !o && this.#f && this.#o
                ) {
                    let u = this.#o, c;
                    for (; c = u?.shift();) { this.#m?.(...c); }
                }
                return this;
            }
            pop() {
                try {
                    for (; this.#n;) {
                        let t = this.#t[this.#r];
                        if (this.#D(!0), this.#e(t)) { if (t.__staleWhileFetching) { return t.__staleWhileFetching; } }
                        else if (t !== void 0) { return t; }
                    }
                } finally {
                    if (this.#f && this.#o) {
                        let t = this.#o, e;
                        for (; e = t?.shift();) { this.#m?.(...e); }
                    }
                }
            }
            #D(t) {
                let e = this.#r, i = this.#i[e], s = this.#t[e];
                return this.#v && this.#e(s)
                    ? s.__abortController.abort(new Error("evicted"))
                    : (this.#_ || this.#f)
                        && (this.#_ && this.#p?.(s, i, "evict"), this.#f && this.#o?.push([s, i, "evict"])),
                    this.#C(e),
                    t && (this.#i[e] = void 0, this.#t[e] = void 0, this.#S.push(e)),
                    this.#n === 1 ? (this.#r = this.#h = 0, this.#S.length = 0) : this.#r = this.#l[e],
                    this.#s.delete(i),
                    this.#n--,
                    e;
            }
            has(t, e = {}) {
                let { updateAgeOnHas: i = this.updateAgeOnHas, status: s } = e, n = this.#s.get(t);
                if (n !== void 0) {
                    let o = this.#t[n];
                    if (this.#e(o) && o.__staleWhileFetching === void 0) { return !1; }
                    if (this.#g(n)) { s && (s.has = "stale", this.#F(s, n)); }
                    else { return i && this.#O(n), s && (s.has = "hit", this.#F(s, n)), !0; }
                } else { s && (s.has = "miss"); }
                return !1;
            }
            peek(t, e = {}) {
                let { allowStale: i = this.allowStale } = e, s = this.#s.get(t);
                if (s === void 0 || !i && this.#g(s)) { return; }
                let n = this.#t[s];
                return this.#e(n) ? n.__staleWhileFetching : n;
            }
            #M(t, e, i, s) {
                let n = e === void 0 ? void 0 : this.#t[e];
                if (this.#e(n)) { return n; }
                let o = new C(), { signal: a } = i;
                a?.addEventListener("abort", () => o.abort(a.reason), { signal: o.signal });
                let r = { signal: o.signal, options: i, context: s },
                    g = (d, w = !1) => {
                        let { aborted: l } = o.signal, m = i.ignoreFetchAbort && d !== void 0;
                        if (
                            i.status && (l && !w
                                ? (i.status.fetchAborted = !0,
                                    i.status.fetchError = o.signal.reason,
                                    m && (i.status.fetchAbortIgnored = !0))
                                : i.status.fetchResolved = !0), l && !m && !w
                        ) { return f(o.signal.reason); }
                        let b = c;
                        return this.#t[e] === c
                            && (d === void 0
                                ? b.__staleWhileFetching ? this.#t[e] = b.__staleWhileFetching : this.#T(t, "fetch")
                                : (i.status && (i.status.fetchUpdated = !0), this.set(t, d, r.options))),
                            d;
                    },
                    y = d => (i.status && (i.status.fetchRejected = !0, i.status.fetchError = d), f(d)),
                    f = d => {
                        let { aborted: w } = o.signal,
                            l = w && i.allowStaleOnFetchAbort,
                            m = l || i.allowStaleOnFetchRejection,
                            b = m || i.noDeleteOnFetchRejection,
                            p = c;
                        if (
                            this.#t[e] === c && (!b || p.__staleWhileFetching === void 0
                                ? this.#T(t, "fetch")
                                : l || (this.#t[e] = p.__staleWhileFetching)), m
                        ) {
                            return i.status && p.__staleWhileFetching !== void 0 && (i.status.returnedStale = !0),
                                p.__staleWhileFetching;
                        }
                        if (p.__returned === p) { throw d; }
                    },
                    u = (d, w) => {
                        let l = this.#R?.(t, n, r);
                        l && l instanceof Promise && l.then(m => d(m === void 0 ? void 0 : m), w),
                            o.signal.addEventListener("abort", () => {
                                (!i.ignoreFetchAbort || i.allowStaleOnFetchAbort)
                                    && (d(void 0), i.allowStaleOnFetchAbort && (d = m => g(m, !0)));
                            });
                    };
                i.status && (i.status.fetchDispatched = !0);
                let c = new Promise(u).then(g, y),
                    E = Object.assign(c, { __abortController: o, __staleWhileFetching: n, __returned: void 0 });
                return e === void 0
                    ? (this.set(t, E, { ...r.options, status: void 0 }), e = this.#s.get(t))
                    : this.#t[e] = E,
                    E;
            }
            #e(t) {
                if (!this.#v) { return !1; }
                let e = t;
                return !!e && e instanceof Promise && e.hasOwnProperty("__staleWhileFetching")
                    && e.__abortController instanceof C;
            }
            async fetch(t, e = {}) {
                let {
                    allowStale: i = this.allowStale,
                    updateAgeOnGet: s = this.updateAgeOnGet,
                    noDeleteOnStaleGet: n = this.noDeleteOnStaleGet,
                    ttl: o = this.ttl,
                    noDisposeOnSet: a = this.noDisposeOnSet,
                    size: r = 0,
                    sizeCalculation: g = this.sizeCalculation,
                    noUpdateTTL: y = this.noUpdateTTL,
                    noDeleteOnFetchRejection: f = this.noDeleteOnFetchRejection,
                    allowStaleOnFetchRejection: u = this.allowStaleOnFetchRejection,
                    ignoreFetchAbort: c = this.ignoreFetchAbort,
                    allowStaleOnFetchAbort: E = this.allowStaleOnFetchAbort,
                    context: d,
                    forceRefresh: w = !1,
                    status: l,
                    signal: m,
                } = e;
                if (!this.#v) {
                    return l && (l.fetch = "get"),
                        this.get(t, { allowStale: i, updateAgeOnGet: s, noDeleteOnStaleGet: n, status: l });
                }
                let b = {
                        allowStale: i,
                        updateAgeOnGet: s,
                        noDeleteOnStaleGet: n,
                        ttl: o,
                        noDisposeOnSet: a,
                        size: r,
                        sizeCalculation: g,
                        noUpdateTTL: y,
                        noDeleteOnFetchRejection: f,
                        allowStaleOnFetchRejection: u,
                        allowStaleOnFetchAbort: E,
                        ignoreFetchAbort: c,
                        status: l,
                        signal: m,
                    },
                    p = this.#s.get(t);
                if (p === void 0) {
                    l && (l.fetch = "miss");
                    let S = this.#M(t, p, b, d);
                    return S.__returned = S;
                } else {
                    let S = this.#t[p];
                    if (this.#e(S)) {
                        let M = i && S.__staleWhileFetching !== void 0;
                        return l && (l.fetch = "inflight", M && (l.returnedStale = !0)),
                            M ? S.__staleWhileFetching : S.__returned = S;
                    }
                    let v = this.#g(p);
                    if (!w && !v) { return l && (l.fetch = "hit"), this.#z(p), s && this.#O(p), l && this.#F(l, p), S; }
                    let _ = this.#M(t, p, b, d), R = _.__staleWhileFetching !== void 0 && i;
                    return l && (l.fetch = v ? "stale" : "refresh", R && v && (l.returnedStale = !0)),
                        R ? _.__staleWhileFetching : _.__returned = _;
                }
            }
            async forceFetch(t, e = {}) {
                let i = await this.fetch(t, e);
                if (i === void 0) { throw new Error("fetch() returned undefined"); }
                return i;
            }
            memo(t, e = {}) {
                let i = this.#x;
                if (!i) { throw new Error("no memoMethod provided to constructor"); }
                let { context: s, forceRefresh: n, ...o } = e, a = this.get(t, o);
                if (!n && a !== void 0) { return a; }
                let r = i(t, a, { options: o, context: s });
                return this.set(t, r, o), r;
            }
            get(t, e = {}) {
                let {
                        allowStale: i = this.allowStale,
                        updateAgeOnGet: s = this.updateAgeOnGet,
                        noDeleteOnStaleGet: n = this.noDeleteOnStaleGet,
                        status: o,
                    } = e,
                    a = this.#s.get(t);
                if (a !== void 0) {
                    let r = this.#t[a], g = this.#e(r);
                    return o && this.#F(o, a),
                        this.#g(a)
                            ? (o && (o.get = "stale"),
                                g
                                    ? (o && i && r.__staleWhileFetching !== void 0 && (o.returnedStale = !0),
                                        i ? r.__staleWhileFetching : void 0)
                                    : (n || this.#T(t, "expire"), o && i && (o.returnedStale = !0), i ? r : void 0))
                            : (o && (o.get = "hit"), g ? r.__staleWhileFetching : (this.#z(a), s && this.#O(a), r));
                } else { o && (o.get = "miss"); }
            }
            #U(t, e) {
                this.#u[e] = t, this.#l[t] = e;
            }
            #z(t) {
                t !== this.#h
                    && (t === this.#r ? this.#r = this.#l[t] : this.#U(this.#u[t], this.#l[t]),
                        this.#U(this.#h, t),
                        this.#h = t);
            }
            delete(t) {
                return this.#T(t, "delete");
            }
            #T(t, e) {
                let i = !1;
                if (this.#n !== 0) {
                    let s = this.#s.get(t);
                    if (s !== void 0) {
                        if (i = !0, this.#n === 1) { this.#j(e); }
                        else {
                            this.#C(s);
                            let n = this.#t[s];
                            if (
                                this.#e(n)
                                    ? n.__abortController.abort(new Error("deleted"))
                                    : (this.#_ || this.#f)
                                        && (this.#_ && this.#p?.(n, t, e), this.#f && this.#o?.push([n, t, e])),
                                    this.#s.delete(t),
                                    this.#i[s] = void 0,
                                    this.#t[s] = void 0,
                                    s === this.#h
                            ) { this.#h = this.#u[s]; } else if (s === this.#r) { this.#r = this.#l[s]; }
                            else {
                                let o = this.#u[s];
                                this.#l[o] = this.#l[s];
                                let a = this.#l[s];
                                this.#u[a] = this.#u[s];
                            }
                            this.#n--, this.#S.push(s);
                        }
                    }
                }
                if (this.#f && this.#o?.length) {
                    let s = this.#o, n;
                    for (; n = s?.shift();) { this.#m?.(...n); }
                }
                return i;
            }
            clear() {
                return this.#j("delete");
            }
            #j(t) {
                for (let e of this.#E({ allowStale: !0 })) {
                    let i = this.#t[e];
                    if (this.#e(i)) { i.__abortController.abort(new Error("deleted")); }
                    else {
                        let s = this.#i[e];
                        this.#_ && this.#p?.(i, s, t), this.#f && this.#o?.push([i, s, t]);
                    }
                }
                if (
                    this.#s.clear(),
                        this.#t.fill(void 0),
                        this.#i.fill(void 0),
                        this.#d && this.#b && (this.#d.fill(0), this.#b.fill(0)),
                        this.#y && this.#y.fill(0),
                        this.#r = 0,
                        this.#h = 0,
                        this.#S.length = 0,
                        this.#w = 0,
                        this.#n = 0,
                        this.#f && this.#o
                ) {
                    let e = this.#o, i;
                    for (; i = e?.shift();) { this.#m?.(...i); }
                }
            }
        };
    var X = 1024,
        Y = 20,
        $ = 150,
        J = 153,
        Z = 170,
        Q = J + 2,
        tt = /\\(?:label|ref|eqref|tag)(?![a-zA-Z])/,
        et = /\\(?:newcommand|renewcommand|providecommand|newenvironment|renewenvironment|def|gdef|edef|xdef|let|futurelet|DeclareMathOperator|DeclarePairedDelimiter|definecolor|colorlet|require)(?![a-zA-Z])/,
        H = new z({ max: X });
    function it(h) {
        let t = 5381;
        for (let e = 0; e < h.length; e++) { t = (t << 5) + t + h.charCodeAt(e) >>> 0; }
        return t;
    }
    var D = 0, U = null;
    function st(h) {
        et.test(h) && h !== U && (U = h, D = it(`${D}|${h}`));
    }
    var K = "";
    function j(h) {
        let t = "";
        if (h.menu?.settings != null) {
            try {
                t = JSON.stringify(h.menu.settings);
            } catch {}
        }
        K = t;
    }
    function nt(h) {
        if (tt.test(h.math)) { return null; }
        let t = h.start.node?.parentElement;
        if (!t) { return null; }
        let e = getComputedStyle(t);
        return `${h.display ? "D" : "I"} ${D} ${K} ${e.fontSize} ${e.fontFamily} ${h.math}`;
    }
    function G(h) {
        if (h.state() >= $) { return !1; }
        let t = nt(h);
        h.ankiCacheKey = t, st(h.math);
        let e = t && H.get(t);
        return e ? (h.typesetRoot = e.cloneNode(!0), h.ankiFromCache = !0, h.state(Q), !0) : !1;
    }
    function P(h) {
        if (h.ankiFromCache || !h.ankiCacheKey || h.state() < $) { return; }
        let t = h.typesetRoot;
        !t || typeof t.cloneNode != "function" || H.set(h.ankiCacheKey, t.cloneNode(!0));
    }
    function V() {
        return {
            ankiCacheLookup: [Y + 10, h => {
                j(h);
                for (let t of h.math) { G(t); }
            }, (h, t) => {
                j(t), G(h);
            }],
            ankiCacheStore: [Z - 10, h => {
                for (let t of h.math) { P(t); }
            }, h => {
                P(h);
            }],
        };
    }
    var q = ["noerrors", "mathtools", "html"];
    function ht(h) {
        return h.map(t => `[tex]/${t}`);
    }
    window.MathJax = {
        tex: {
            displayMath: [["\\[", "\\]"]],
            processEscapes: !1,
            processEnvironments: !1,
            processRefs: !1,
            packages: { "[+]": q, "[-]": ["textmacros"] },
        },
        loader: { load: ht(q), paths: { mathjax: "/_anki/js/vendor/mathjax" } },
        startup: { typeset: !1 },
        options: { renderActions: V() },
    };
})();
