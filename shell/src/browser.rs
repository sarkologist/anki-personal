// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The card browser without its window: what `aqt/browser/table`
//! (state, model, selection), the sidebar tree and Find Duplicates do in
//! Python. `browser_window.rs` draws it and sends the page's requests here.
//!
//! The browser shows items: cards, or notes in notes mode. Rows are fetched
//! by position as the page scrolls; `gen` changes whenever the item list
//! does, so a page holding older positions can tell.

use std::collections::HashMap;
use std::collections::HashSet;

use anki::prelude::I18n;
use anki_proto::generic;
use anki_proto::search::browser_columns::Sorting;
use anki_proto::search::search_node;
use anki_proto::search::search_node::group::Joiner;
use anki_proto::search::search_node::Filter;
use anki_proto::search::BrowserRow;
use anki_proto::search::SearchNode;
use serde::Serialize;
use serde_json::json;
use serde_json::Value;

use crate::api::Api;
use crate::api::ApiError;

/// At most this many rows stay selected after a search
/// (`Table.SELECTION_LIMIT`).
pub const SELECTION_LIMIT: usize = 500;

pub const CARDS_COLUMNS_KEY: &str = "activeCols";
pub const NOTES_COLUMNS_KEY: &str = "activeNoteCols";
const NOTES_MODE_KEY: &str = "browserTableShowNotesMode";
const DEFAULT_CARD_COLUMNS: &[&str] = &["noteFld", "template", "cardDue", "deck"];
const DEFAULT_NOTE_COLUMNS: &[&str] = &["noteFld", "note", "template", "noteTags"];

fn sort_keys(notes: bool) -> (&'static str, &'static str) {
    if notes {
        ("noteSortType", "browserNoteSortBackwards")
    } else {
        ("sortType", "sortBackwards")
    }
}

pub fn get_config(api: &Api, key: &str) -> Option<Value> {
    let json: generic::Json = api
        .call("get_config_json", &generic::String { val: key.into() })
        .ok()?;
    serde_json::from_slice(&json.json).ok()
}

pub fn set_config(api: &Api, key: &str, value: &Value) {
    // not undoable, and no changes to observe, as `col.set_config`
    let result: Result<generic::Empty, _> = api.call(
        "set_config_json_no_undo",
        &anki_proto::config::SetConfigJsonRequest {
            key: key.into(),
            value_json: serde_json::to_vec(value).unwrap(),
            undoable: false,
        },
    );
    if let Err(err) = result {
        tracing::warn!(key, %err, "saving browser config failed");
    }
}

// Columns
//////////////////////////////////////////////////////////////////////////

/// `BrowserColumns.Column`, from rslib or an add-on
/// (`browser_did_fetch_columns`).
#[derive(Debug, Clone, PartialEq, Serialize, serde::Deserialize)]
pub struct Column {
    pub key: String,
    pub cards_label: String,
    pub notes_label: String,
    #[serde(default)]
    pub cards_tooltip: String,
    #[serde(default)]
    pub notes_tooltip: String,
    /// `Sorting` values
    pub sorting_cards: i32,
    pub sorting_notes: i32,
    /// 0 start, 1 centre
    #[serde(default)]
    pub alignment: i32,
    #[serde(default)]
    pub uses_cell_font: bool,
}

impl Column {
    fn from_pb(c: anki_proto::search::browser_columns::Column) -> Self {
        Column {
            key: c.key,
            cards_label: c.cards_mode_label,
            notes_label: c.notes_mode_label,
            cards_tooltip: c.cards_mode_tooltip,
            notes_tooltip: c.notes_mode_tooltip,
            sorting_cards: c.sorting_cards,
            sorting_notes: c.sorting_notes,
            alignment: c.alignment,
            uses_cell_font: c.uses_cell_font,
        }
    }

    /// `addon_column_fillin`: an active column nobody describes (an add-on's
    /// that isn't running).
    fn fillin(key: &str, tr: &I18n) -> Self {
        Column {
            key: key.into(),
            cards_label: tr.browsing_addon().into_owned(),
            notes_label: tr.browsing_addon().into_owned(),
            cards_tooltip: String::new(),
            notes_tooltip: String::new(),
            sorting_cards: Sorting::None as i32,
            sorting_notes: Sorting::None as i32,
            alignment: 0,
            uses_cell_font: false,
        }
    }

    pub fn sorting(&self, notes: bool) -> i32 {
        if notes {
            self.sorting_notes
        } else {
            self.sorting_cards
        }
    }

    pub fn label(&self, notes: bool) -> &str {
        if notes {
            &self.notes_label
        } else {
            &self.cards_label
        }
    }

    pub fn tooltip(&self, notes: bool) -> &str {
        if notes {
            &self.notes_tooltip
        } else {
            &self.cards_tooltip
        }
    }
}

pub fn builtin_columns(api: &Api) -> Vec<Column> {
    api.call::<anki_proto::search::BrowserColumns>("all_browser_columns", &generic::Empty {})
        .map(|c| c.columns.into_iter().map(Column::from_pb).collect())
        .unwrap_or_default()
}

// Searching
//////////////////////////////////////////////////////////////////////////

/// What `browser_will_search`/`browser_did_search` see and may change
/// (`aqt.browser.table.SearchContext`).
#[derive(Debug, Clone, PartialEq)]
pub struct SearchContext {
    pub search: String,
    /// The column to sort by, if any (Qt's `order`: a column, or False).
    pub order: Option<String>,
    pub reverse: bool,
    /// Set by a hook to skip the search.
    pub ids: Option<Vec<i64>>,
    /// Whether a `browser_will_search` hook changed the context (kept
    /// state in `addon_metadata`, or replaced the order).
    pub changed_by_addons: bool,
}

/// The add-on hooks around searching and row fetching
/// (`browser_will_search`, `browser_did_search`, `browser_did_fetch_row`).
pub trait Hooks {
    fn will_search(&self, _ctx: &mut SearchContext, _notes: bool) {}
    fn did_search(&self, _ctx: &mut SearchContext, _notes: bool) {}
    fn fetched_rows(&self, _rows: &mut [(i64, BrowserRow)], _notes: bool, _columns: &[String]) {}
}

pub struct NoHooks;
impl Hooks for NoHooks {}

/// The text in a search node, as rslib writes it (and checks it).
pub fn normalize(api: &Api, node: SearchNode) -> Result<String, ApiError> {
    api.call::<generic::String>("build_search_string", &node)
        .map(|s| s.val)
}

pub fn text_node(text: &str) -> SearchNode {
    node(Filter::ParsableText(text.into()))
}

pub fn node(filter: Filter) -> SearchNode {
    SearchNode {
        filter: Some(filter),
    }
}

pub fn group(nodes: Vec<SearchNode>, joiner: Joiner) -> SearchNode {
    node(Filter::Group(search_node::Group {
        nodes,
        joiner: joiner as i32,
    }))
}

/// How a sidebar click changes the search (`SidebarTreeView.update_search`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, serde::Deserialize)]
pub struct Modifiers {
    #[serde(default)]
    pub cmd: bool,
    #[serde(default)]
    pub shift: bool,
    #[serde(default)]
    pub alt: bool,
}

/// The search after clicking an item with `item_search` while `current`
/// is shown: Cmd adds it (AND), Shift ORs it, both replace the same kind of
/// term, Option negates it, nothing replaces the search.
pub fn combine_search(
    api: &Api,
    current: &str,
    item_search: &str,
    mods: Modifiers,
) -> Result<String, ApiError> {
    let mut item = text_node(item_search);
    if mods.alt {
        item = node(Filter::Negated(Box::new(item)));
    }
    let previous = text_node(current);
    let join = |joiner: Joiner| {
        api.call::<generic::String>(
            "join_search_nodes",
            &anki_proto::search::JoinSearchNodesRequest {
                joiner: joiner as i32,
                existing_node: Some(previous.clone()),
                additional_node: Some(item.clone()),
            },
        )
        .map(|s| s.val)
    };
    match (mods.cmd, mods.shift) {
        (true, true) => api
            .call::<generic::String>(
                "replace_search_node",
                &anki_proto::search::ReplaceSearchNodeRequest {
                    existing_node: Some(previous.clone()),
                    replacement_node: Some(item.clone()),
                },
            )
            .map(|s| s.val),
        (true, false) => join(Joiner::And),
        (false, true) => join(Joiner::Or),
        (false, false) => normalize(api, item),
    }
}

/// The search when nothing is typed: the collection's default search,
/// or the current deck (`Browser._default_search`).
pub fn default_search(api: &Api) -> String {
    let text = get_config(api, "defaultSearchText")
        .and_then(|v| v.as_str().map(str::to_string))
        .unwrap_or_default();
    if !text.trim().is_empty() {
        return text;
    }
    normalize(api, node(Filter::Deck("current".into()))).unwrap_or_else(|_| "deck:current".into())
}

// The table
//////////////////////////////////////////////////////////////////////////

/// What the table shows: `Table` with its `ItemState` and `DataModel`.
pub struct State {
    pub notes_mode: bool,
    /// The search shown, as run (normalized).
    pub search: String,
    /// What the search bar shows for it (empty for the default search).
    pub search_prompt: String,
    pub items: Vec<i64>,
    positions: HashMap<i64, usize>,
    /// Changes with `items`.
    pub gen: u64,
    /// Selected items, in row order.
    pub selected: Vec<i64>,
    pub current: Option<i64>,
    /// Active columns, for this mode.
    pub columns: Vec<String>,
    pub sort_column: String,
    pub sort_backwards: bool,
    /// rslib's and the add-ons' columns, by key.
    known: Vec<Column>,
}

/// What a page needs to draw the table's header and rows.
#[derive(Debug, Serialize)]
pub struct TableJson {
    pub gen: u64,
    pub notes_mode: bool,
    pub search: String,
    pub count: usize,
    pub columns: Vec<Value>,
    pub sort_column: String,
    pub sort_backwards: bool,
}

impl State {
    /// The mode and its columns and sort as saved in the collection.
    pub fn load(api: &Api, addon_columns: Vec<Column>) -> State {
        let notes_mode = get_config(api, NOTES_MODE_KEY)
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let mut known = builtin_columns(api);
        known.retain(|c| !addon_columns.iter().any(|a| a.key == c.key));
        known.extend(addon_columns);
        let mut state = State {
            notes_mode,
            search: String::new(),
            search_prompt: String::new(),
            items: vec![],
            positions: HashMap::new(),
            gen: 0,
            selected: vec![],
            current: None,
            columns: vec![],
            sort_column: String::new(),
            sort_backwards: false,
            known,
        };
        state.load_mode(api);
        state
    }

    fn load_mode(&mut self, api: &Api) {
        let (key, defaults) = if self.notes_mode {
            (NOTES_COLUMNS_KEY, DEFAULT_NOTE_COLUMNS)
        } else {
            (CARDS_COLUMNS_KEY, DEFAULT_CARD_COLUMNS)
        };
        self.columns = get_config(api, key)
            .and_then(|v| serde_json::from_value::<Vec<String>>(v).ok())
            .filter(|c| !c.is_empty())
            .unwrap_or_else(|| defaults.iter().map(|s| s.to_string()).collect());
        let (sort_key, backwards_key) = sort_keys(self.notes_mode);
        self.sort_column = get_config(api, sort_key)
            .and_then(|v| v.as_str().map(str::to_string))
            .unwrap_or_else(|| "noteCrt".into());
        self.sort_backwards = get_config(api, backwards_key)
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        self.sync_backend_columns(api);
    }

    /// rslib renders the active columns, which are global; another
    /// browser state (a test) may have set others.
    pub fn sync_backend_columns(&self, api: &Api) {
        let _: Result<generic::Empty, _> = api.call(
            "set_active_browser_columns",
            &generic::StringList {
                vals: self.columns.clone(),
            },
        );
    }

    pub fn column(&self, key: &str, tr: &I18n) -> Column {
        self.known
            .iter()
            .find(|c| c.key == key)
            .cloned()
            .unwrap_or_else(|| Column::fillin(key, tr))
    }

    /// Every column the header menu offers, labelled for this mode.
    pub fn known_columns(&self) -> &[Column] {
        &self.known
    }

    pub fn set_addon_columns(&mut self, addon: Vec<Column>) {
        let keys: HashSet<String> = addon.iter().map(|c| c.key.clone()).collect();
        self.known.retain(|c| !keys.contains(&c.key));
        self.known.extend(addon);
    }

    pub fn table_json(&self, tr: &I18n) -> TableJson {
        let notes = self.notes_mode;
        TableJson {
            gen: self.gen,
            notes_mode: notes,
            search: self.search.clone(),
            count: self.items.len(),
            columns: self
                .columns
                .iter()
                .map(|key| {
                    let c = self.column(key, tr);
                    json!({
                        "key": c.key,
                        "label": c.label(notes),
                        "tooltip": c.tooltip(notes),
                        "sortable": c.sorting(notes) != Sorting::None as i32,
                        "center": c.alignment == 1,
                        "font": c.uses_cell_font,
                    })
                })
                .collect(),
            sort_column: self.sort_column.clone(),
            sort_backwards: self.sort_backwards,
        }
    }

    fn set_items(&mut self, items: Vec<i64>) {
        self.positions = items.iter().enumerate().map(|(i, id)| (*id, i)).collect();
        self.items = items;
        self.gen += 1;
    }

    pub fn row_of(&self, item: i64) -> Option<usize> {
        self.positions.get(&item).copied()
    }

    /// `ItemState.find_items` with the hooks around it
    /// (`DataModel._search_inner`).
    fn find(
        &self,
        api: &Api,
        hooks: &dyn Hooks,
        search: &str,
        tr: &I18n,
    ) -> Result<Vec<i64>, ApiError> {
        let mut ctx = SearchContext {
            search: search.into(),
            order: Some(self.sort_column.clone()),
            reverse: self.sort_backwards,
            ids: None,
            changed_by_addons: false,
        };
        hooks.will_search(&mut ctx, self.notes_mode);
        let ids = match ctx.ids.take() {
            Some(ids) => ids,
            None => {
                let order = ctx
                    .order
                    .as_deref()
                    .map(|key| self.column(key, tr))
                    .filter(|c| c.sorting(self.notes_mode) != Sorting::None as i32)
                    .map(|c| anki_proto::search::SortOrder {
                        value: Some(anki_proto::search::sort_order::Value::Builtin(
                            anki_proto::search::sort_order::Builtin {
                                column: c.key,
                                reverse: ctx.reverse,
                            },
                        )),
                    });
                let order = order.unwrap_or(anki_proto::search::SortOrder {
                    value: Some(anki_proto::search::sort_order::Value::None(
                        generic::Empty {},
                    )),
                });
                let method = if self.notes_mode {
                    "search_notes"
                } else {
                    "search_cards"
                };
                api.call::<anki_proto::search::SearchResponse>(
                    method,
                    &anki_proto::search::SearchRequest {
                        search: ctx.search.clone(),
                        order: Some(order),
                    },
                )?
                .ids
            }
        };
        ctx.ids = Some(ids);
        hooks.did_search(&mut ctx, self.notes_mode);
        Ok(ctx.ids.unwrap_or_default())
    }

    /// Run `search` (already normalized), keeping the selected items that
    /// are still there (`Table.search`).
    pub fn search(
        &mut self,
        api: &Api,
        hooks: &dyn Hooks,
        search: &str,
        tr: &I18n,
    ) -> Result<(), ApiError> {
        let items = self.find(api, hooks, search, tr)?;
        self.search = search.into();
        let (selected, current) = (self.selected.clone(), self.current);
        self.set_items(items);
        self.restore_selection(&selected, current);
        Ok(())
    }

    /// Search again for what's shown (after an add-on's request, or a
    /// change that adds or removes rows).
    pub fn research(&mut self, api: &Api, hooks: &dyn Hooks, tr: &I18n) -> Result<(), ApiError> {
        let search = self.search.clone();
        self.search(api, hooks, &search, tr)
    }

    /// `Table._restore_selection`: the given items' rows, at most
    /// [SELECTION_LIMIT] of them, or the first row.
    fn restore_selection(&mut self, items: &[i64], current: Option<i64>) {
        let rows: Vec<usize> = items.iter().filter_map(|i| self.row_of(*i)).collect();
        let current = current.and_then(|c| self.row_of(c));
        let (rows, current) = qualify_selection(rows, current, self.items.len());
        self.set_selection_rows(&rows, current);
    }

    fn set_selection_rows(&mut self, rows: &[usize], current: Option<usize>) {
        let mut rows: Vec<usize> = rows
            .iter()
            .copied()
            .filter(|&r| r < self.items.len())
            .collect();
        rows.sort_unstable();
        rows.dedup();
        self.selected = rows.iter().map(|&r| self.items[r]).collect();
        self.current = current
            .filter(|&r| r < self.items.len())
            .map(|r| self.items[r]);
    }

    /// The page's selection: row ranges (`[start, end)`) and the current
    /// row, for list `gen`. A stale selection (the items changed since) is
    /// ignored.
    pub fn set_selection(
        &mut self,
        gen: u64,
        ranges: &[(usize, usize)],
        current: Option<usize>,
    ) -> bool {
        if gen != self.gen {
            return false;
        }
        let rows: Vec<usize> = ranges
            .iter()
            .flat_map(|&(start, end)| start..end.min(self.items.len()))
            .collect();
        self.set_selection_rows(&rows, current);
        true
    }

    /// The selection as row ranges, for the page.
    pub fn selection_json(&self) -> Value {
        let mut rows: Vec<usize> = self
            .selected
            .iter()
            .filter_map(|i| self.row_of(*i))
            .collect();
        rows.sort_unstable();
        let mut ranges: Vec<(usize, usize)> = vec![];
        for r in rows {
            match ranges.last_mut() {
                Some(last) if last.1 == r => last.1 = r + 1,
                _ => ranges.push((r, r + 1)),
            }
        }
        json!({
            "gen": self.gen,
            "ranges": ranges,
            "current": self.current.and_then(|c| self.row_of(c)),
        })
    }

    /// The single selected item (`get_single_selected_card`), as a card.
    pub fn single_card(&self, api: &Api) -> Option<i64> {
        match self.selected.as_slice() {
            [item] => self.card_of(api, *item),
            _ => None,
        }
    }

    pub fn current_card(&self, api: &Api) -> Option<i64> {
        self.current.and_then(|item| self.card_of(api, item))
    }

    /// A card id for an item: itself, or a note's first card.
    pub fn card_of(&self, api: &Api, item: i64) -> Option<i64> {
        if !self.notes_mode {
            return Some(item);
        }
        api.query(
            "select id from cards where nid = ? order by ord limit 1",
            &[json!(item)],
        )
        .ok()?
        .first()?
        .first()?
        .as_i64()
    }

    pub fn note_of(&self, api: &Api, item: i64) -> Option<i64> {
        if self.notes_mode {
            return Some(item);
        }
        api.query("select nid from cards where id = ?", &[json!(item)])
            .ok()?
            .first()?
            .first()?
            .as_i64()
    }

    pub fn selected_card_ids(&self, api: &Api) -> Vec<i64> {
        if self.notes_mode {
            cards_of_notes(api, &self.selected)
        } else {
            self.selected.clone()
        }
    }

    pub fn selected_note_ids(&self, api: &Api) -> Vec<i64> {
        if self.notes_mode {
            self.selected.clone()
        } else {
            notes_of_cards(api, &self.selected)
        }
    }

    /// Switch between cards and notes, keeping the selection
    /// (`Table.toggle_state`).
    pub fn toggle_mode(&mut self, api: &Api, hooks: &dyn Hooks, tr: &I18n) -> Result<(), ApiError> {
        let (selected, current) = (self.selected.clone(), self.current);
        self.notes_mode = !self.notes_mode;
        self.load_mode(api);
        let search = self.search.clone();
        let items = match self.find(api, hooks, &search, tr) {
            Ok(items) => items,
            Err(err) => {
                self.notes_mode = !self.notes_mode;
                self.load_mode(api);
                return Err(err);
            }
        };
        set_config(api, NOTES_MODE_KEY, &json!(self.notes_mode));
        let convert = |ids: &[i64]| {
            if self.notes_mode {
                notes_of_cards(api, ids)
            } else {
                cards_of_notes(api, ids)
            }
        };
        let new_selected = convert(&selected);
        let new_current = current.and_then(|c| convert(&[c]).first().copied());
        self.set_items(items);
        self.restore_selection(&new_selected, new_current);
        Ok(())
    }

    /// A header click (`_on_sort_column_changed`): sort by `key`, or
    /// reverse if it already is. Err with a message for columns that
    /// can't sort.
    pub fn sort_by(
        &mut self,
        api: &Api,
        hooks: &dyn Hooks,
        key: &str,
        tr: &I18n,
    ) -> Result<(), String> {
        let column = self.column(key, tr);
        let sorting = column.sorting(self.notes_mode);
        if sorting == Sorting::None as i32 {
            return Err(tr.browsing_sorting_on_this_column_is_not().into_owned());
        }
        let (sort_key, backwards_key) = sort_keys(self.notes_mode);
        if self.sort_column != key {
            self.sort_column = key.into();
            // numeric columns start descending
            self.sort_backwards = sorting == Sorting::Descending as i32;
            set_config(api, sort_key, &json!(key));
            set_config(api, backwards_key, &json!(self.sort_backwards));
            return self.research(api, hooks, tr).map_err(|e| e.message);
        }
        self.sort_backwards = !self.sort_backwards;
        set_config(api, backwards_key, &json!(self.sort_backwards));
        if self.builtin(key) {
            // `Table._reverse`
            let (selected, current) = (self.selected.clone(), self.current);
            let mut items = std::mem::take(&mut self.items);
            items.reverse();
            self.set_items(items);
            self.restore_selection(&selected, current);
            Ok(())
        } else {
            // an add-on's order comes from its search hooks, and needn't
            // be symmetric (unscored cards stay last)
            self.research(api, hooks, tr).map_err(|e| e.message)
        }
    }

    /// Sort by `key` in this direction, then search again (an add-on's
    /// `_on_sort_column_changed`).
    pub fn sort_to(
        &mut self,
        api: &Api,
        hooks: &dyn Hooks,
        key: &str,
        descending: bool,
        tr: &I18n,
    ) -> Result<(), String> {
        if self.column(key, tr).sorting(self.notes_mode) == Sorting::None as i32 {
            return Err(tr.browsing_sorting_on_this_column_is_not().into_owned());
        }
        let (sort_key, backwards_key) = sort_keys(self.notes_mode);
        self.sort_column = key.into();
        self.sort_backwards = descending;
        set_config(api, sort_key, &json!(key));
        set_config(api, backwards_key, &json!(descending));
        self.research(api, hooks, tr).map_err(|e| e.message)
    }

    fn builtin(&self, key: &str) -> bool {
        // rslib's own columns are the ones `all_browser_columns` names
        key.parse::<BuiltinKey>().is_ok()
    }

    /// Show or hide a column (`_on_column_toggled`); Err if it's the last.
    pub fn toggle_column(&mut self, api: &Api, key: &str, tr: &I18n) -> Result<(), String> {
        if let Some(pos) = self.columns.iter().position(|c| c == key) {
            if self.columns.len() < 2 {
                return Err(tr.browsing_you_must_have_at_least_one().into_owned());
            }
            self.columns.remove(pos);
        } else {
            self.columns.push(key.into());
        }
        let config_key = if self.notes_mode {
            NOTES_COLUMNS_KEY
        } else {
            CARDS_COLUMNS_KEY
        };
        set_config(api, config_key, &json!(self.columns));
        self.sync_backend_columns(api);
        // same items, new cells
        self.gen += 1;
        Ok(())
    }

    /// Rows `start..end` for the page (`DataModel.get_row`).
    pub fn rows(&self, api: &Api, hooks: &dyn Hooks, start: usize, end: usize) -> Vec<Value> {
        let end = end.min(self.items.len());
        let start = start.min(end);
        let mut fetched: Vec<(i64, BrowserRow)> = vec![];
        let mut out: Vec<Option<Value>> = vec![];
        for &id in &self.items[start..end] {
            match api.call::<BrowserRow>("browser_row_for_id", &generic::Int64 { val: id }) {
                Ok(row) => {
                    fetched.push((id, row));
                    out.push(None);
                }
                Err(err) => out.push(Some(json!({
                    "id": id,
                    "disabled": true,
                    "cells": vec![json!({"text": err.message}); self.columns.len()],
                }))),
            }
        }
        if !fetched.is_empty() {
            hooks.fetched_rows(&mut fetched, self.notes_mode, &self.columns);
        }
        let mut fetched = fetched.into_iter();
        out.into_iter()
            .map(|row| match row {
                Some(row) => row,
                None => {
                    let (id, row) = fetched.next().unwrap();
                    row_json(id, &row)
                }
            })
            .collect()
    }
}

/// rslib's column keys (`browser_table::Column`, less `Custom`).
struct BuiltinKey;

impl std::str::FromStr for BuiltinKey {
    type Err = ();
    fn from_str(s: &str) -> Result<Self, ()> {
        const KEYS: &[&str] = &[
            "answer",
            "cardMod",
            "template",
            "deck",
            "cardDue",
            "cardEase",
            "cardLapses",
            "cardIvl",
            "noteCrt",
            "noteMod",
            "note",
            "originalPosition",
            "question",
            "cardReps",
            "noteFld",
            "noteTags",
            "stability",
            "difficulty",
            "retrievability",
        ];
        KEYS.contains(&s).then_some(BuiltinKey).ok_or(())
    }
}

fn row_json(id: i64, row: &BrowserRow) -> Value {
    json!({
        "id": id,
        "cells": row.cells.iter().map(|c| json!({"text": c.text, "rtl": c.is_rtl})).collect::<Vec<_>>(),
        "color": row.color,
        "font": row.font_name,
        "fontSize": row.font_size,
    })
}

/// `_qualify_selected_rows` and the current row: between 1 and
/// [SELECTION_LIMIT] rows, or none in an empty table.
pub fn qualify_selection(
    rows: Vec<usize>,
    current: Option<usize>,
    len: usize,
) -> (Vec<usize>, Option<usize>) {
    if len == 0 {
        return (vec![], None);
    }
    let rows = if rows.is_empty() {
        vec![current.unwrap_or(0)]
    } else if rows.len() < SELECTION_LIMIT {
        rows
    } else if let Some(c) = current.filter(|c| rows.contains(c)) {
        vec![c]
    } else {
        rows[..1].to_vec()
    };
    let current = current.or_else(|| rows.first().copied());
    (rows, current)
}

fn id_list(ids: &[i64]) -> String {
    ids.iter().map(i64::to_string).collect::<Vec<_>>().join(",")
}

fn ids_of(api: &Api, sql: &str) -> Vec<i64> {
    api.query(sql, &[])
        .unwrap_or_default()
        .into_iter()
        .filter_map(|row| row.first().and_then(Value::as_i64))
        .collect()
}

pub fn notes_of_cards(api: &Api, cards: &[i64]) -> Vec<i64> {
    if cards.is_empty() {
        return vec![];
    }
    ids_of(
        api,
        &format!(
            "select distinct nid from cards where id in ({})",
            id_list(cards)
        ),
    )
}

pub fn cards_of_notes(api: &Api, notes: &[i64]) -> Vec<i64> {
    if notes.is_empty() {
        return vec![];
    }
    ids_of(
        api,
        &format!("select id from cards where nid in ({})", id_list(notes)),
    )
}

// The sidebar
//////////////////////////////////////////////////////////////////////////

#[derive(Debug, Clone, Serialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct SidebarItem {
    pub name: String,
    /// "savedSearch", "today", "flag", "cardState", "deck", "notetype",
    /// "template", "tag", or a section ("…Root").
    pub kind: &'static str,
    /// What clicking it searches for (empty: nothing).
    pub search: String,
    /// The deck, tag or saved search's full name, for renaming.
    #[serde(skip_serializing_if = "String::is_empty")]
    pub full_name: String,
    /// A deck or notetype id, a flag number.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub id: Option<i64>,
    /// A colour for its icon (a CSS variable).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub color: Option<&'static str>,
    pub expanded: bool,
    pub children: Vec<SidebarItem>,
}

impl SidebarItem {
    fn new(name: impl Into<String>, kind: &'static str, search: String) -> Self {
        SidebarItem {
            name: name.into(),
            kind,
            search,
            full_name: String::new(),
            id: None,
            color: None,
            expanded: false,
            children: vec![],
        }
    }
}

/// The sections' collapse config keys (`Config.Bool.COLLAPSE_*`).
pub const SECTION_KEYS: &[(&str, &str)] = &[
    ("savedSearchRoot", "collapseSavedSearches"),
    ("todayRoot", "collapseToday"),
    ("flagRoot", "collapseFlags"),
    ("cardStateRoot", "collapseCardState"),
    ("deckRoot", "collapseDecks"),
    ("notetypeRoot", "collapseNotetypes"),
    ("tagRoot", "collapseTags"),
];

/// The flag names, with the collection's own names (`flagLabels`).
pub fn flag_names(api: &Api, tr: &I18n) -> Vec<String> {
    let custom = get_config(api, "flagLabels").unwrap_or(Value::Null);
    [
        tr.actions_flag_red(),
        tr.actions_flag_orange(),
        tr.actions_flag_green(),
        tr.actions_flag_blue(),
        tr.actions_flag_pink(),
        tr.actions_flag_turquoise(),
        tr.actions_flag_purple(),
    ]
    .into_iter()
    .enumerate()
    .map(|(i, name)| {
        custom[(i + 1).to_string()]
            .as_str()
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .unwrap_or_else(|| name.into_owned())
    })
    .collect()
}

pub const FLAG_COLORS: [&str; 7] = [
    "--flag-1", "--flag-2", "--flag-3", "--flag-4", "--flag-5", "--flag-6", "--flag-7",
];

pub fn saved_searches(api: &Api) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = get_config(api, "savedFilters")
        .and_then(|v| serde_json::from_value::<HashMap<String, String>>(v).ok())
        .unwrap_or_default()
        .into_iter()
        .collect();
    out.sort_by_key(|(name, _)| name.to_lowercase());
    out
}

pub fn set_saved_searches(api: &Api, searches: &[(String, String)]) {
    let map: serde_json::Map<String, Value> = searches
        .iter()
        .map(|(k, v)| (k.clone(), json!(v)))
        .collect();
    set_config(api, "savedFilters", &Value::Object(map));
}

/// `SidebarTreeView._build_tree`.
pub fn sidebar(api: &Api, tr: &I18n) -> Result<Vec<SidebarItem>, ApiError> {
    let build = |filter: Filter| normalize(api, node(filter));
    let collapsed = |key: &str| {
        get_config(api, key)
            .and_then(|v| v.as_bool())
            .unwrap_or(false)
    };
    let section = |name: std::borrow::Cow<str>, kind: &'static str, search: String| {
        let key = SECTION_KEYS
            .iter()
            .find(|(k, _)| *k == kind)
            .map(|(_, key)| *key)
            .unwrap();
        let mut item = SidebarItem::new(name, kind, search);
        item.expanded = !collapsed(key);
        item
    };
    let mut roots = vec![];

    let mut saved = section(
        tr.browsing_sidebar_saved_searches(),
        "savedSearchRoot",
        String::new(),
    );
    for (name, search) in saved_searches(api) {
        let mut item = SidebarItem::new(name.clone(), "savedSearch", search);
        item.full_name = name;
        saved.children.push(item);
    }
    roots.push(saved);

    let mut today = section(tr.browsing_today(), "todayRoot", String::new());
    let rated = |rating: search_node::Rating| {
        Filter::Rated(search_node::Rated {
            days: 1,
            rating: rating as i32,
        })
    };
    let overdue = normalize(
        api,
        group(
            vec![
                node(Filter::CardState(search_node::CardState::Due as i32)),
                node(Filter::Negated(Box::new(node(Filter::DueOnDay(0))))),
            ],
            Joiner::And,
        ),
    )?;
    for (name, search) in [
        (tr.browsing_sidebar_due_today(), build(Filter::DueOnDay(0))?),
        (tr.browsing_added_today(), build(Filter::AddedInDays(1))?),
        (tr.browsing_edited_today(), build(Filter::EditedInDays(1))?),
        (
            tr.browsing_studied_today(),
            build(rated(search_node::Rating::Any))?,
        ),
        (
            tr.browsing_sidebar_first_review(),
            build(Filter::IntroducedInDays(1))?,
        ),
        (
            tr.browsing_sidebar_rescheduled(),
            build(rated(search_node::Rating::ByReschedule))?,
        ),
        (
            tr.browsing_again_today(),
            build(rated(search_node::Rating::Again))?,
        ),
        (tr.browsing_sidebar_overdue(), overdue),
    ] {
        today.children.push(SidebarItem::new(name, "today", search));
    }
    roots.push(today);

    let mut flags = section(
        tr.browsing_sidebar_flags(),
        "flagRoot",
        build(Filter::Flag(search_node::Flag::Any as i32))?,
    );
    flags.children.push(SidebarItem::new(
        tr.browsing_no_flag(),
        "flag",
        build(Filter::Flag(search_node::Flag::None as i32))?,
    ));
    for (i, name) in flag_names(api, tr).into_iter().enumerate() {
        let mut item = SidebarItem::new(
            name,
            "flag",
            build(Filter::Flag(search_node::Flag::Red as i32 + i as i32))?,
        );
        item.id = Some(i as i64 + 1);
        item.color = Some(FLAG_COLORS[i]);
        flags.children.push(item);
    }
    roots.push(flags);

    let mut states = section(
        tr.browsing_sidebar_card_state(),
        "cardStateRoot",
        String::new(),
    );
    for (name, state, color) in [
        (tr.actions_new(), search_node::CardState::New, "--state-new"),
        (
            tr.scheduling_learning(),
            search_node::CardState::Learn,
            "--state-learn",
        ),
        (
            tr.browsing_sidebar_card_state_review(),
            search_node::CardState::Review,
            "--state-review",
        ),
        (
            tr.browsing_suspended(),
            search_node::CardState::Suspended,
            "--state-suspended",
        ),
        (
            tr.browsing_buried(),
            search_node::CardState::Buried,
            "--state-buried",
        ),
    ] {
        let mut item = SidebarItem::new(name, "cardState", build(Filter::CardState(state as i32))?);
        item.color = Some(color);
        states.children.push(item);
    }
    roots.push(states);

    let mut decks = section(
        tr.browsing_sidebar_decks(),
        "deckRoot",
        build(Filter::Deck("_*".into()))?,
    );
    let mut current = SidebarItem::new(
        tr.browsing_current_deck(),
        "currentDeck",
        build(Filter::Deck("current".into()))?,
    );
    current.id = api
        .call::<anki_proto::decks::Deck>("get_current_deck", &generic::Empty {})
        .ok()
        .map(|d| d.id);
    decks.children.push(current);
    let tree: anki_proto::decks::DeckTreeNode =
        api.call("deck_tree", &anki_proto::decks::DeckTreeRequest { now: 0 })?;
    fn deck_items(
        api: &Api,
        nodes: &[anki_proto::decks::DeckTreeNode],
        head: &str,
    ) -> Result<Vec<SidebarItem>, ApiError> {
        nodes
            .iter()
            .map(|n| {
                let full = format!("{head}{}", n.name);
                let mut item = SidebarItem::new(
                    n.name.clone(),
                    if n.filtered { "filteredDeck" } else { "deck" },
                    normalize(api, node(Filter::Deck(full.clone())))?,
                );
                item.id = Some(n.deck_id);
                item.expanded = !n.collapsed;
                item.children = deck_items(api, &n.children, &format!("{full}::"))?;
                item.full_name = full;
                Ok(item)
            })
            .collect()
    }
    decks.children.extend(deck_items(api, &tree.children, "")?);
    roots.push(decks);

    let mut notetypes = section(
        tr.browsing_sidebar_notetypes(),
        "notetypeRoot",
        build(Filter::Note("_*".into()))?,
    );
    let names: anki_proto::notetypes::NotetypeNames =
        api.call("get_notetype_names", &generic::Empty {})?;
    let mut names = names.entries;
    names.sort_by_key(|e| e.name.to_lowercase());
    for entry in names {
        let nt = crate::editor::notetype_json(api, entry.id).unwrap_or_default();
        let mut item = SidebarItem::new(
            entry.name.clone(),
            "notetype",
            build(Filter::Note(entry.name.clone()))?,
        );
        item.id = Some(entry.id);
        item.full_name = entry.name.clone();
        let templates = nt["tmpls"].as_array().cloned().unwrap_or_default();
        for (ord, t) in templates.iter().enumerate() {
            let search = normalize(
                api,
                group(
                    vec![
                        node(Filter::Note(entry.name.clone())),
                        node(Filter::Template(ord as u32)),
                    ],
                    Joiner::And,
                ),
            )?;
            item.children.push(SidebarItem::new(
                t["name"].as_str().unwrap_or_default(),
                "template",
                search,
            ));
        }
        notetypes.children.push(item);
    }
    roots.push(notetypes);

    let mut tags = section(
        tr.browsing_sidebar_tags(),
        "tagRoot",
        build(Filter::Tag("_*".into()))?,
    );
    tags.children.push(SidebarItem::new(
        tr.browsing_sidebar_untagged(),
        "untagged",
        normalize(
            api,
            node(Filter::Negated(Box::new(node(Filter::Tag("_*".into()))))),
        )?,
    ));
    let tree: anki_proto::tags::TagTreeNode = api.call("tag_tree", &generic::Empty {})?;
    fn tag_items(
        api: &Api,
        nodes: &[anki_proto::tags::TagTreeNode],
        head: &str,
    ) -> Result<Vec<SidebarItem>, ApiError> {
        nodes
            .iter()
            .map(|n| {
                let full = format!("{head}{}", n.name);
                let mut item = SidebarItem::new(
                    n.name.clone(),
                    "tag",
                    normalize(api, node(Filter::Tag(full.clone())))?,
                );
                item.expanded = !n.collapsed;
                item.children = tag_items(api, &n.children, &format!("{full}::"))?;
                item.full_name = full;
                Ok(item)
            })
            .collect()
    }
    tags.children.extend(tag_items(api, &tree.children, "")?);
    roots.push(tags);
    Ok(roots)
}

// Find Duplicates
//////////////////////////////////////////////////////////////////////////

/// Every field name, sorted (`anki.find.fieldNames`).
pub fn all_field_names(api: &Api) -> Vec<String> {
    let names: anki_proto::notetypes::NotetypeNames = api
        .call("get_notetype_names", &generic::Empty {})
        .unwrap_or_default();
    let mut fields: Vec<String> = names
        .entries
        .iter()
        .flat_map(|e| {
            crate::editor::field_names(&crate::editor::notetype_json(api, e.id).unwrap_or_default())
        })
        .collect();
    fields.sort_by_key(|f| f.to_lowercase());
    fields.dedup_by_key(|f| f.to_lowercase());
    fields
}

/// `Collection.find_dupes`: groups of notes whose `field` is the same
/// (without HTML), in the order the second of each was found.
pub fn find_dupes(
    api: &Api,
    field: &str,
    search: &str,
) -> Result<Vec<(String, Vec<i64>)>, ApiError> {
    let mut nodes = vec![node(Filter::FieldName(field.into()))];
    if !search.trim().is_empty() {
        nodes.insert(0, text_node(search));
    }
    let query = normalize(api, group(nodes, Joiner::And))?;
    let nids = api
        .call::<anki_proto::search::SearchResponse>(
            "search_notes",
            &anki_proto::search::SearchRequest {
                search: query,
                order: None,
            },
        )?
        .ids;
    if nids.is_empty() {
        return Ok(vec![]);
    }
    let rows = api.query(
        &format!(
            "select id, mid, flds from notes where id in ({})",
            id_list(&nids)
        ),
        &[],
    )?;
    let mut ords: HashMap<i64, Option<usize>> = HashMap::new();
    let mut groups: HashMap<String, Vec<i64>> = HashMap::new();
    let mut order: Vec<String> = vec![];
    for row in rows {
        let (Some(nid), Some(mid), Some(flds)) =
            (row[0].as_i64(), row[1].as_i64(), row[2].as_str())
        else {
            continue;
        };
        let ord = *ords.entry(mid).or_insert_with(|| {
            crate::editor::field_names(&crate::editor::notetype_json(api, mid).unwrap_or_default())
                .iter()
                .position(|f| f.to_lowercase() == field.to_lowercase())
        });
        let Some(ord) = ord else { continue };
        let Some(value) = flds.split('\x1f').nth(ord) else {
            continue;
        };
        let value = anki::text::strip_html_preserving_media_filenames(value).into_owned();
        if value.is_empty() {
            continue;
        }
        let group = groups.entry(value.clone()).or_default();
        group.push(nid);
        if group.len() == 2 {
            order.push(value);
        }
    }
    Ok(order
        .into_iter()
        .map(|value| {
            let nids = groups.remove(&value).unwrap_or_default();
            (value, nids)
        })
        .collect())
}

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn a_search_keeps_the_selection_it_can() {
        assert_eq!(
            qualify_selection(vec![3, 5], Some(5), 10),
            (vec![3, 5], Some(5))
        );
        // gone: the first row
        assert_eq!(qualify_selection(vec![], None, 10), (vec![0], Some(0)));
        // only the current row is left
        assert_eq!(qualify_selection(vec![], Some(4), 10), (vec![4], Some(4)));
        assert_eq!(qualify_selection(vec![], None, 0), (vec![], None));
    }

    #[test]
    fn a_big_selection_shrinks_to_the_current_row() {
        let rows: Vec<usize> = (0..SELECTION_LIMIT).collect();
        assert_eq!(
            qualify_selection(rows.clone(), Some(7), 1000),
            (vec![7], Some(7))
        );
        assert_eq!(qualify_selection(rows, None, 1000), (vec![0], Some(0)));
    }
}

#[cfg(test)]
mod store_test {
    use anki_proto::notes::AddNoteRequest;
    use anki_proto::notes::AddNoteResponse;

    use super::*;
    use crate::profile::ProfilePaths;
    use crate::Shell;

    fn shell() -> (tempfile::TempDir, Shell) {
        let dir = tempfile::tempdir().unwrap();
        let shell = Shell::new(Api::new(vec!["en".into()]).unwrap(), Default::default());
        shell
            .open_profile(ProfilePaths::new(dir.path(), "User 1"))
            .unwrap();
        (dir, shell)
    }

    fn tr() -> I18n {
        I18n::new(&["en"])
    }

    /// Basic notes with these fronts and tags, in deck 1, oldest first.
    fn add_notes(api: &Api, notes: &[(&str, &str)]) -> Vec<i64> {
        let ntid: anki_proto::notetypes::NotetypeId = api
            .call(
                "get_notetype_id_by_name",
                &generic::String {
                    val: "Basic (and reversed card)".into(),
                },
            )
            .unwrap();
        notes
            .iter()
            .map(|(front, tags)| {
                let mut note = crate::editor::new_note(api, ntid.ntid).unwrap();
                note.fields = vec![front.to_string(), "back".into()];
                note.tags = tags.split_whitespace().map(str::to_string).collect();
                let added: AddNoteResponse = api
                    .call(
                        "add_note",
                        &AddNoteRequest {
                            note: Some(note),
                            deck_id: 1,
                        },
                    )
                    .unwrap();
                // distinct creation times
                std::thread::sleep(std::time::Duration::from_millis(2));
                added.note_id
            })
            .collect()
    }

    fn search(state: &mut State, api: &Api, text: &str) {
        let text = normalize(api, text_node(text)).unwrap();
        state.search(api, &NoHooks, &text, &tr()).unwrap();
    }

    fn fronts(state: &State, api: &Api) -> Vec<String> {
        let sort_field = state.columns.iter().position(|c| c == "noteFld").unwrap();
        state
            .rows(api, &NoHooks, 0, state.items.len())
            .iter()
            .map(|r| r["cells"][sort_field]["text"].as_str().unwrap().to_string())
            .collect()
    }

    #[test]
    fn searches_sort_by_the_saved_column() {
        let (_dir, shell) = shell();
        let api = &shell.api;
        add_notes(api, &[("b", ""), ("c", ""), ("a", "")]);
        set_config(api, "sortType", &json!("noteFld"));
        set_config(api, "sortBackwards", &json!(false));
        let mut state = State::load(api, vec![]);
        assert!(!state.notes_mode);
        search(&mut state, api, "card:1");
        assert_eq!(fronts(&state, api), ["a", "b", "c"]);
        // the same column again reverses
        state.sort_by(api, &NoHooks, "noteFld", &tr()).unwrap();
        assert_eq!(fronts(&state, api), ["c", "b", "a"]);
        assert_eq!(get_config(api, "sortBackwards"), Some(json!(true)));
        // a new numeric column starts descending
        state.sort_by(api, &NoHooks, "noteCrt", &tr()).unwrap();
        assert!(state.sort_backwards);
        assert_eq!(fronts(&state, api), ["a", "c", "b"]);
        assert_eq!(get_config(api, "sortType"), Some(json!("noteCrt")));
        // and some columns can't sort
        assert!(state.sort_by(api, &NoHooks, "question", &tr()).is_err());
    }

    #[test]
    fn a_search_keeps_the_selected_items_still_found() {
        let (_dir, shell) = shell();
        let api = &shell.api;
        add_notes(api, &[("a", "x"), ("b", "x"), ("c", "")]);
        let mut state = State::load(api, vec![]);
        search(&mut state, api, "card:1");
        assert_eq!(state.selected.len(), 1, "the first row is selected");
        let gen = state.gen;
        assert!(state.set_selection(gen, &[(0, 3)], Some(2)));
        let all = state.selected.clone();
        search(&mut state, api, "card:1 tag:x");
        assert_eq!(state.items.len(), 2);
        let kept: Vec<i64> = all
            .iter()
            .copied()
            .filter(|i| state.items.contains(i))
            .collect();
        assert_eq!(state.selected, kept);
        // a selection for an old list is ignored
        assert!(!state.set_selection(gen, &[(0, 1)], Some(0)));
    }

    #[test]
    fn notes_mode_keeps_the_selection_as_notes() {
        let (_dir, shell) = shell();
        let api = &shell.api;
        let nids = add_notes(api, &[("a", ""), ("b", "")]);
        let mut state = State::load(api, vec![]);
        search(&mut state, api, "");
        assert_eq!(state.items.len(), 4, "two cards each");
        let row = state
            .items
            .iter()
            .position(|&cid| state.note_of(api, cid) == Some(nids[1]))
            .unwrap();
        state.set_selection(state.gen, &[(row, row + 1)], Some(row));
        state.toggle_mode(api, &NoHooks, &tr()).unwrap();
        assert!(state.notes_mode);
        assert_eq!(state.items.len(), 2);
        assert_eq!(state.selected, vec![nids[1]]);
        assert_eq!(state.current, Some(nids[1]));
        assert_eq!(get_config(api, NOTES_MODE_KEY), Some(json!(true)));
        // its own columns
        assert_eq!(state.columns, DEFAULT_NOTE_COLUMNS);
        state.toggle_mode(api, &NoHooks, &tr()).unwrap();
        assert_eq!(state.selected.len(), 2, "both of its cards");
    }

    #[test]
    fn columns_toggle_but_one_stays() {
        let (_dir, shell) = shell();
        let api = &shell.api;
        add_notes(api, &[("a", "")]);
        let mut state = State::load(api, vec![]);
        search(&mut state, api, "");
        state.toggle_column(api, "noteTags", &tr()).unwrap();
        assert_eq!(
            get_config(api, CARDS_COLUMNS_KEY),
            Some(json!([
                "noteFld", "template", "cardDue", "deck", "noteTags"
            ]))
        );
        let row = &state.rows(api, &NoHooks, 0, 1)[0];
        assert_eq!(row["cells"].as_array().unwrap().len(), 5);
        for key in ["noteFld", "template", "cardDue", "deck"] {
            state.toggle_column(api, key, &tr()).unwrap();
        }
        assert!(state.toggle_column(api, "noteTags", &tr()).is_err());
        assert_eq!(state.columns, ["noteTags"]);
    }

    #[test]
    fn unknown_columns_are_add_ons_and_dont_sort() {
        let (_dir, shell) = shell();
        let api = &shell.api;
        add_notes(api, &[("b", ""), ("a", "")]);
        set_config(api, CARDS_COLUMNS_KEY, &json!(["noteFld", "addonKey"]));
        set_config(api, "sortType", &json!("addonKey"));
        let mut state = State::load(api, vec![]);
        search(&mut state, api, "card:1");
        assert_eq!(state.items.len(), 2);
        let json = state.table_json(&tr());
        assert_eq!(json.columns[1]["label"], json!("Add-on"));
        assert_eq!(json.columns[1]["sortable"], json!(false));
        let row = &state.rows(api, &NoHooks, 0, 1)[0];
        assert_eq!(row["cells"][1]["text"], json!(""));
    }

    struct PriorityHooks;
    impl Hooks for PriorityHooks {
        fn will_search(&self, ctx: &mut SearchContext, _notes: bool) {
            if ctx.order.as_deref() == Some("addonKey") {
                ctx.order = None;
                ctx.changed_by_addons = true;
            }
        }
        fn did_search(&self, ctx: &mut SearchContext, _notes: bool) {
            ctx.ids.as_mut().unwrap().sort_unstable_by(|a, b| b.cmp(a));
        }
        fn fetched_rows(&self, rows: &mut [(i64, BrowserRow)], _notes: bool, columns: &[String]) {
            let col = columns.iter().position(|c| c == "addonKey").unwrap();
            for (id, row) in rows {
                row.cells[col].text = format!("p{id}");
            }
        }
    }

    #[test]
    fn add_ons_sort_and_fill_their_columns() {
        let (_dir, shell) = shell();
        let api = &shell.api;
        add_notes(api, &[("a", ""), ("b", "")]);
        set_config(api, CARDS_COLUMNS_KEY, &json!(["noteFld", "addonKey"]));
        set_config(api, "sortType", &json!("addonKey"));
        let column = Column {
            key: "addonKey".into(),
            cards_label: "Priority".into(),
            notes_label: "Priority".into(),
            cards_tooltip: String::new(),
            notes_tooltip: String::new(),
            sorting_cards: Sorting::Descending as i32,
            sorting_notes: Sorting::None as i32,
            alignment: 1,
            uses_cell_font: false,
        };
        let mut state = State::load(api, vec![column]);
        let text = normalize(api, text_node("card:1")).unwrap();
        state.search(api, &PriorityHooks, &text, &tr()).unwrap();
        let mut sorted = state.items.clone();
        sorted.sort_unstable_by(|a, b| b.cmp(a));
        assert_eq!(state.items, sorted);
        let rows = state.rows(api, &PriorityHooks, 0, 2);
        assert_eq!(
            rows[0]["cells"][1]["text"],
            json!(format!("p{}", sorted[0]))
        );
        assert_eq!(
            state.table_json(&tr()).columns[1]["label"],
            json!("Priority")
        );
    }

    /// Qt runs `browser_did_search` on every search, whatever
    /// `browser_will_search` did.
    struct Filter;
    impl Hooks for Filter {
        fn did_search(&self, ctx: &mut SearchContext, _notes: bool) {
            ctx.ids.as_mut().unwrap().truncate(1);
        }
    }

    #[test]
    fn did_search_hooks_run_on_their_own() {
        let (_dir, shell) = shell();
        let api = &shell.api;
        add_notes(api, &[("a", ""), ("b", "")]);
        let mut state = State::load(api, vec![]);
        state.search(api, &Filter, "", &tr()).unwrap();
        assert_eq!(state.items.len(), 1);
    }

    #[test]
    fn deleted_items_show_as_disabled_rows() {
        let (_dir, shell) = shell();
        let api = &shell.api;
        let nids = add_notes(api, &[("a", "")]);
        let mut state = State::load(api, vec![]);
        search(&mut state, api, "");
        let _: anki_proto::collection::OpChangesWithCount = api
            .call(
                "remove_notes",
                &anki_proto::notes::RemoveNotesRequest {
                    note_ids: nids,
                    card_ids: vec![],
                },
            )
            .unwrap();
        let row = &state.rows(api, &NoHooks, 0, 1)[0];
        assert_eq!(row["disabled"], json!(true));
    }

    #[test]
    fn sidebar_items_search_for_themselves() {
        let (_dir, shell) = shell();
        let api = &shell.api;
        add_notes(api, &[("a", "leech parent::child")]);
        set_saved_searches(api, &[("my leeches".into(), "tag:leech".into())]);
        set_config(api, "collapseToday", &json!(true));
        let roots = sidebar(api, &tr()).unwrap();
        let kinds: Vec<&str> = roots.iter().map(|r| r.kind).collect();
        assert_eq!(
            kinds,
            [
                "savedSearchRoot",
                "todayRoot",
                "flagRoot",
                "cardStateRoot",
                "deckRoot",
                "notetypeRoot",
                "tagRoot"
            ]
        );
        assert_eq!(roots[0].children[0].search, "tag:leech");
        assert!(!roots[1].expanded && roots[0].expanded);
        let red = &roots[2].children[1];
        assert_eq!((red.search.as_str(), red.id), ("flag:1", Some(1)));
        let tags = &roots[6].children;
        let parent = tags.iter().find(|t| t.name == "parent").unwrap();
        assert_eq!(parent.children[0].full_name, "parent::child");
        assert_eq!(parent.children[0].search, "tag:parent::child");
        let default = roots[4]
            .children
            .iter()
            .find(|d| d.name == "Default")
            .unwrap();
        assert_eq!(default.search, "deck:Default");
        let basic = roots[5]
            .children
            .iter()
            .find(|n| n.name == "Basic (and reversed card)")
            .unwrap();
        assert_eq!(basic.children.len(), 2);
        assert_eq!(
            basic.children[1].search,
            "\"note:Basic (and reversed card)\" card:2"
        );
    }

    #[test]
    fn modifier_clicks_combine_searches() {
        let (_dir, shell) = shell();
        let api = &shell.api;
        let m = |cmd, shift, alt| Modifiers { cmd, shift, alt };
        let combine = |mods| combine_search(api, "deck:a tag:x", "tag:y", mods).unwrap();
        assert_eq!(combine(m(false, false, false)), "tag:y");
        assert_eq!(combine(m(true, false, false)), "(deck:a tag:x) tag:y");
        assert_eq!(combine(m(false, true, false)), "(deck:a tag:x) OR tag:y");
        assert_eq!(combine(m(false, false, true)), "-tag:y");
        assert_eq!(combine(m(true, true, false)), "deck:a tag:y");
    }

    #[test]
    fn duplicates_ignore_html_and_empty_fields() {
        let (_dir, shell) = shell();
        let api = &shell.api;
        let nids = add_notes(
            api,
            &[
                ("dog", ""),
                ("<b>dog</b>", ""),
                ("cat", ""),
                ("", ""),
                ("", "x"),
            ],
        );
        let dupes = find_dupes(api, "Front", "").unwrap();
        assert_eq!(dupes.len(), 1);
        assert_eq!(dupes[0].0, "dog");
        let mut found = dupes[0].1.clone();
        found.sort_unstable();
        assert_eq!(found, nids[..2]);
        assert!(find_dupes(api, "Front", "-dog").unwrap().is_empty());
        assert!(all_field_names(api).contains(&"Front".to_string()));
    }
}
