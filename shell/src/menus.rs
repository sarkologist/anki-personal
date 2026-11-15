// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Native menus: the menu bar (`aqt/forms/main.ui` plus the reviewer's
//! context menu, `Reviewer._contextMenu`) and the reviewer's "More" popup.
//!
//! Single-key shortcuts stay in the pages (`crate::shortcuts`), since a
//! menu key equivalent would also fire while typing in the editor; the
//! menu shows them in the item titles instead. Cmd shortcuts are real key
//! equivalents.

use std::sync::Arc;

use tauri::menu::CheckMenuItem;
use tauri::menu::CheckMenuItemBuilder;
use tauri::menu::Menu;
use tauri::menu::MenuBuilder;
use tauri::menu::MenuItem;
use tauri::menu::MenuItemBuilder;
use tauri::menu::PredefinedMenuItem;
use tauri::menu::Submenu;
use tauri::menu::SubmenuBuilder;
use tauri::AppHandle;
use tauri::Manager;
use tauri::Wry;

use crate::app::Ui;
use crate::app::MAIN;
use crate::main_window;
use crate::main_window::MainState;

/// Flag ids and their names, `aqt.flags`.
fn flag_names(ui: &Ui) -> [String; 7] {
    let tr = &ui.main.tr;
    [
        tr.actions_flag_red().into_owned(),
        tr.actions_flag_orange().into_owned(),
        tr.actions_flag_green().into_owned(),
        tr.actions_flag_blue().into_owned(),
        tr.actions_flag_pink().into_owned(),
        tr.actions_flag_turquoise().into_owned(),
        tr.actions_flag_purple().into_owned(),
    ]
}

/// Items whose state changes: undo/redo titles, flag and auto-advance
/// checks, and what's enabled.
pub struct Menus {
    undo: MenuItem<Wry>,
    redo: MenuItem<Wry>,
    flags: Vec<CheckMenuItem<Wry>>,
    auto_advance: CheckMenuItem<Wry>,
    /// Enabled only while reviewing.
    review_items: Vec<MenuItem<Wry>>,
    /// Enabled with a profile open.
    profile_items: Vec<MenuItem<Wry>>,
    sync_logout: MenuItem<Wry>,
    tools: Submenu<Wry>,
    /// Add-ons' items at the end of Tools.
    addon_items: Vec<MenuItem<Wry>>,
}

/// Menu ids of add-ons' items.
const ADDON_ITEM: &str = "addon:";

fn addon_item(
    app: &AppHandle,
    item: &crate::plugins::ui::MenuItem,
) -> tauri::Result<MenuItem<Wry>> {
    let builder = || {
        MenuItemBuilder::with_id(format!("{ADDON_ITEM}{}", item.id), &item.text)
            .enabled(item.enabled)
    };
    let accel = item.shortcut.as_deref().and_then(qt_accelerator);
    match accel {
        // a sequence the menu can't take still gets its item
        Some(accel) => builder()
            .accelerator(&accel)
            .build(app)
            .or_else(|_| builder().build(app)),
        None => builder().build(app),
    }
}

/// Keys macOS or every app's own menus already have; an add-on's item
/// mustn't take them (Cmd+Option+Shift+Q logs out at once).
const RESERVED: &[&str] = &[
    "Cmd+Q",
    "Cmd+W",
    "Cmd+H",
    "Cmd+M",
    "Cmd+Alt+H",
    "Cmd+Shift+Q",
    "Cmd+Alt+Shift+Q",
    "Cmd+Ctrl+Q",
    "Cmd+Alt+Escape",
    "Cmd+Space",
    "Cmd+Tab",
    "Cmd+Shift+3",
    "Cmd+Shift+4",
    "Cmd+Shift+5",
];

/// Qt's key sequence as a menu accelerator: Qt's Ctrl is Cmd on macOS,
/// and its Meta is Ctrl. Only single chords, and none of the system's.
pub fn qt_accelerator(keys: &str) -> Option<String> {
    let accel = qt_chord(keys)?;
    (!RESERVED.contains(&accel.as_str())).then_some(accel)
}

fn qt_chord(keys: &str) -> Option<String> {
    let keys = keys.trim();
    if keys.is_empty() || keys.contains(',') {
        return None;
    }
    let parts: Vec<&str> = keys.split('+').map(str::trim).collect();
    let (key, mods) = parts.split_last()?;
    let mods: Vec<String> = mods.iter().map(|m| m.to_ascii_lowercase()).collect();
    let mut out: Vec<&str> = vec![];
    // in one order, so the reserved ones can be compared
    for (qt, accel) in [
        ("ctrl", "Cmd"),
        ("meta", "Ctrl"),
        ("alt", "Alt"),
        ("shift", "Shift"),
    ] {
        if mods.iter().any(|m| m == qt) {
            out.push(accel);
        }
    }
    if mods
        .iter()
        .any(|m| !["ctrl", "meta", "alt", "shift"].contains(&m.as_str()))
    {
        return None;
    }
    let key = match key.to_ascii_lowercase().as_str() {
        "" => return None,
        "esc" | "escape" => "Escape".to_string(),
        "return" | "enter" => "Enter".to_string(),
        "del" | "delete" => "Delete".to_string(),
        "backspace" => "Backspace".to_string(),
        "space" => "Space".to_string(),
        "tab" => "Tab".to_string(),
        k if k.len() == 1 => k.to_ascii_uppercase(),
        k if k.starts_with('f') && k[1..].parse::<u8>().is_ok() => k.to_ascii_uppercase(),
        _ => return None,
    };
    out.push(&key);
    Some(out.join("+"))
}

/// "Title (key)": the page shortcut, shown since it isn't a key equivalent.
fn titled(ui: &Ui, title: &str, action: &str) -> String {
    let settings = ui.settings.lock().unwrap();
    let s = crate::shortcuts::Shortcuts::new(&settings.answer_keys, &settings.shortcuts);
    match s.combos(action).first() {
        Some(key) => format!("{title}\t{key}"),
        None => title.to_string(),
    }
}

fn item(
    app: &AppHandle,
    id: &str,
    title: &str,
    accel: Option<&str>,
) -> tauri::Result<MenuItem<Wry>> {
    let mut b = MenuItemBuilder::with_id(id, title);
    if let Some(accel) = accel {
        b = b.accelerator(accel);
    }
    b.build(app)
}

struct CardMenu {
    menu: Submenu<Wry>,
    flags: Vec<CheckMenuItem<Wry>>,
    auto_advance: CheckMenuItem<Wry>,
    items: Vec<MenuItem<Wry>>,
}

/// The card actions, shared by the menu bar and the "More" popup.
fn card_submenu(ui: &Ui, app: &AppHandle, title: &str) -> tauri::Result<CardMenu> {
    let tr = &ui.main.tr;
    let flags: Vec<CheckMenuItem<Wry>> = flag_names(ui)
        .iter()
        .enumerate()
        .map(|(i, name)| {
            CheckMenuItemBuilder::with_id(format!("flag{}", i + 1), name)
                .accelerator(format!("CmdOrCtrl+{}", i + 1))
                .build(app)
        })
        .collect::<tauri::Result<_>>()?;
    let mut flag_menu = SubmenuBuilder::new(app, tr.studying_flag_card());
    for f in &flags {
        flag_menu = flag_menu.item(f);
    }
    let flag_menu = flag_menu.build()?;
    let i = |id: &str, title: std::borrow::Cow<str>, action: &str| {
        item(app, id, &titled(ui, &title, action), None)
    };
    let items = vec![
        i("buryCard", tr.studying_bury_card(), "buryCard")?,
        i("suspendCard", tr.actions_suspend_card(), "suspendCard")?,
        i("options", tr.actions_options(), "options")?,
        i("cardInfo", tr.actions_card_info(), "cardInfo")?,
        item(
            app,
            "previousCardInfo",
            &tr.actions_previous_card_info(),
            Some("CmdOrCtrl+Alt+I"),
        )?,
        i("mark", tr.studying_mark_note(), "mark")?,
        i("buryNote", tr.studying_bury_note(), "buryNote")?,
        i("suspendNote", tr.studying_suspend_note(), "suspendNote")?,
        item(
            app,
            "deleteNote",
            &tr.studying_delete_note(),
            Some("CmdOrCtrl+Backspace"),
        )?,
        i("edit", tr.studying_edit(), "edit")?,
        i("replay", tr.actions_replay_audio(), "replay")?,
    ];
    let auto_advance = CheckMenuItemBuilder::with_id(
        "autoAdvance",
        titled(ui, &tr.actions_auto_advance(), "autoAdvance"),
    )
    .build(app)?;
    let sep = || PredefinedMenuItem::separator(app);
    let menu = SubmenuBuilder::new(app, title)
        .item(&flag_menu)
        .item(&items[0])
        .item(&items[1])
        .item(&items[2])
        .item(&items[3])
        .item(&items[4])
        .item(&sep()?)
        .item(&items[5])
        .item(&items[6])
        .item(&items[7])
        .item(&items[8])
        .item(&sep()?)
        .item(&items[9])
        .item(&items[10])
        .item(&auto_advance)
        .build()?;
    Ok(CardMenu {
        menu,
        flags,
        auto_advance,
        items,
    })
}

pub fn build(ui: &Ui, app: &AppHandle) -> tauri::Result<(Menu<Wry>, Menus)> {
    let tr = &ui.main.tr;
    let app_menu = SubmenuBuilder::new(app, "Anki")
        .item(&PredefinedMenuItem::about(app, None, None)?)
        .separator()
        .item(&item(
            app,
            "preferences",
            &format!("{}…", tr.preferences_preferences()),
            Some("CmdOrCtrl+,"),
        )?)
        .separator()
        .item(&PredefinedMenuItem::hide(app, None)?)
        .item(&PredefinedMenuItem::hide_others(app, None)?)
        .separator()
        // not PredefinedMenuItem::quit: its terminate: skips ExitRequested,
        // so the editor's pending edits couldn't be saved
        .item(&item(app, "quit", "Quit Anki", Some("CmdOrCtrl+Q"))?)
        .build()?;
    let sync = item(app, "sync", &titled(ui, &tr.qt_misc_sync(), "sync"), None)?;
    let sync_logout = item(app, "syncLogout", &tr.sync_log_out_button(), None)?;
    let file = SubmenuBuilder::new(app, "File")
        .item(&sync)
        .item(&sync_logout)
        .separator()
        .item(&item(
            app,
            "reimport",
            "Re-import Profiles from Anki",
            None,
        )?)
        .build()?;
    let undo = item(app, "undo", &tr.qt_accel_undo(), Some("CmdOrCtrl+Z"))?;
    let redo = item(app, "redo", &tr.qt_accel_redo(), Some("CmdOrCtrl+Shift+Z"))?;
    let edit = SubmenuBuilder::new(app, "Edit")
        .item(&undo)
        .item(&redo)
        .separator()
        .cut()
        .copy()
        .paste()
        .select_all()
        .build()?;
    let decks = item(
        app,
        "decks",
        &titled(ui, &tr.actions_decks(), "decks"),
        None,
    )?;
    let study = item(app, "study", &titled(ui, "Study Deck", "study"), None)?;
    let browse = item(
        app,
        "browse",
        &titled(ui, &tr.qt_misc_browse(), "browse"),
        None,
    )?;
    let choose_deck = item(
        app,
        "chooseDeck",
        &titled(ui, &tr.qt_misc_study_deck(), "chooseDeck"),
        None,
    )?;
    let stats = item(
        app,
        "stats",
        &titled(ui, &tr.qt_misc_stats(), "stats"),
        None,
    )?;
    let view = SubmenuBuilder::new(app, "View")
        .item(&item(
            app,
            crate::zoom::ZOOM_IN,
            &tr.qt_accel_zoom_in(),
            Some("CmdOrCtrl+="),
        )?)
        .item(&item(
            app,
            crate::zoom::ZOOM_OUT,
            &tr.qt_accel_zoom_out(),
            Some("CmdOrCtrl+-"),
        )?)
        .item(&item(
            app,
            crate::zoom::RESET_ZOOM,
            &tr.qt_accel_reset_zoom(),
            Some("CmdOrCtrl+0"),
        )?)
        .build()?;
    let go = SubmenuBuilder::new(app, "Go")
        .item(&decks)
        .item(&study)
        .item(&browse)
        .item(&choose_deck)
        .item(&stats)
        .build()?;
    let CardMenu {
        menu: card,
        flags,
        auto_advance,
        items: review_items,
    } = card_submenu(ui, app, "Card")?;
    let create_filtered = item(
        app,
        "createFiltered",
        &titled(ui, &tr.qt_misc_create_filtered_deck(), "createFiltered"),
        None,
    )?;
    let check_media = item(
        app,
        "checkMedia",
        &tr.media_check_check_media_action(),
        None,
    )?;
    let empty_cards = item(app, "emptyCards", &tr.qt_misc_empty_cards(), None)?;
    let tools = SubmenuBuilder::new(app, "Tools")
        .item(&create_filtered)
        .separator()
        .item(&check_media)
        .item(&empty_cards)
        .separator()
        .item(&item(app, "addonsRestart", "Restart Add-ons", None)?)
        .item(&item(app, "addonsLog", "Add-on Log", None)?)
        .separator()
        .build()?;
    let window = SubmenuBuilder::new(app, "Window")
        .item(&PredefinedMenuItem::minimize(app, None)?)
        .item(&PredefinedMenuItem::fullscreen(app, None)?)
        .build()?;
    let menu = MenuBuilder::new(app)
        .items(&[&app_menu, &file, &edit, &view, &go, &card, &tools, &window])
        .build()?;
    let menus = Menus {
        undo,
        redo,
        flags,
        auto_advance,
        review_items,
        profile_items: vec![
            sync,
            decks,
            study,
            browse,
            choose_deck,
            stats,
            create_filtered,
            check_media,
            empty_cards,
        ],
        sync_logout,
        tools,
        addon_items: vec![],
    };
    Ok((menu, menus))
}

/// Update titles, checks and enabled items for the current state. The
/// menu calls wait for the main thread, so they're queued there instead of
/// blocking the caller (a bridge thread).
pub fn refresh(ui: &Arc<Ui>) {
    let tr = &ui.main.tr;
    let profile = ui.shell.open_profile_paths().is_some();
    let status: anki_proto::collection::UndoStatus = if profile {
        ui.shell
            .api
            .call("get_undo_status", &anki_proto::generic::Empty {})
            .unwrap_or_default()
    } else {
        Default::default()
    };
    let undo = if status.undo.is_empty() {
        tr.qt_accel_undo().into_owned()
    } else {
        tr.undo_undo_action(status.undo.clone()).into_owned()
    };
    let redo = if status.redo.is_empty() {
        tr.qt_accel_redo().into_owned()
    } else {
        tr.undo_redo_action(status.redo.clone()).into_owned()
    };
    let reviewing = *ui.main.state.lock().unwrap() == MainState::Review;
    let (flag, auto_advance) = {
        let r = ui.main.reviewer.lock().unwrap();
        (r.flag(), r.auto_advance)
    };
    let logged_in = main_window::sync_state_json(ui)["loggedIn"] == serde_json::Value::Bool(true);
    let u = ui.clone();
    let _ = ui.app.run_on_main_thread(move || {
        let guard = u.menus.lock().unwrap();
        let Some(m) = guard.as_ref() else {
            return;
        };
        let _ = m.undo.set_text(undo);
        let _ = m.redo.set_text(redo);
        for (i, item) in m.flags.iter().enumerate() {
            let _ = item.set_enabled(reviewing);
            let _ = item.set_checked(reviewing && flag == i as u32 + 1);
        }
        let _ = m.auto_advance.set_enabled(reviewing);
        let _ = m.auto_advance.set_checked(auto_advance);
        for item in &m.review_items {
            let _ = item.set_enabled(reviewing);
        }
        for item in &m.profile_items {
            let _ = item.set_enabled(profile);
        }
        let _ = m.sync_logout.set_enabled(logged_in);
    });
}

/// Replace the add-ons' items in Tools.
pub fn set_tools(ui: &Arc<Ui>, items: Vec<crate::plugins::ui::MenuItem>) {
    let u = ui.clone();
    let _ = ui.app.run_on_main_thread(move || {
        let mut guard = u.menus.lock().unwrap();
        let Some(m) = guard.as_mut() else {
            return;
        };
        for old in m.addon_items.drain(..) {
            let _ = m.tools.remove(&old);
        }
        for item in &items {
            match addon_item(&u.app, item) {
                Ok(new) => {
                    let _ = m.tools.append(&new);
                    m.addon_items.push(new);
                }
                Err(err) => tracing::warn!(%err, "add-on menu item"),
            }
        }
    });
}

/// The reviewer's "More" button and `m`.
pub fn popup_more(ui: &Arc<Ui>) {
    let Some(window) = ui.app.get_webview_window(MAIN) else {
        return;
    };
    let Ok(CardMenu {
        menu: card,
        flags,
        auto_advance,
        ..
    }) = card_submenu(ui, &ui.app, "More")
    else {
        return;
    };
    let (flag, on) = {
        let r = ui.main.reviewer.lock().unwrap();
        (r.flag(), r.auto_advance)
    };
    for (i, item) in flags.iter().enumerate() {
        let _ = item.set_checked(flag == i as u32 + 1);
    }
    let _ = auto_advance.set_checked(on);
    // `reviewer_will_show_context_menu`
    let card_id = ui.main.reviewer.lock().unwrap().card_id();
    let addon_items = card_id.map_or(vec![], |cid| crate::plugins::ui::context_menu(ui, cid));
    if !addon_items.is_empty() {
        if let Ok(sep) = PredefinedMenuItem::separator(&ui.app) {
            let _ = card.append(&sep);
        }
        for item in &addon_items {
            if let Ok(item) = addon_item(&ui.app, item) {
                let _ = card.append(&item);
            }
        }
    }
    let w = window.clone();
    // AppKit only on the main thread
    let _ = window.run_on_main_thread(move || {
        if let Err(err) = w.popup_menu(&card) {
            tracing::warn!(%err, "showing the More menu failed");
        }
    });
}

/// A menu item was chosen. Handled off the main thread, since a sync holds
/// the collection and most actions need it.
pub fn on_event(ui: &Arc<Ui>, id: &str) {
    let ui = ui.clone();
    let id = id.to_string();
    std::thread::spawn(move || handle_event(&ui, &id));
}

fn handle_event(ui: &Arc<Ui>, id: &str) {
    match id {
        "quit" => ui.app.exit(0),
        "reimport" => {
            if let Err(err) = ui.reimport() {
                main_window::message(ui, &format!("Re-import failed: {err:#}"));
            }
            crate::theme::apply(ui, None);
            refresh(ui);
        }
        "syncLogout" => main_window::logout(ui),
        "preferences" => crate::preferences::open(ui),
        "checkMedia" if ui.shell.open_profile_paths().is_some() => {
            crate::screens::open_media_check(ui)
        }
        "emptyCards" if ui.shell.open_profile_paths().is_some() => {
            crate::screens::open_empty_cards(ui)
        }
        "addonsRestart" => crate::plugins::ui::restart(ui),
        "addonsLog" => crate::plugins::ui::show_log(ui),
        id if crate::zoom::on_menu(ui, id) => return,
        id if id.starts_with(crate::browser_window::MENU_PREFIX) => {
            crate::browser_window::menu_event(ui, &id[crate::browser_window::MENU_PREFIX.len()..]);
        }
        id if id.starts_with(ADDON_ITEM) => {
            if let Ok(id) = id[ADDON_ITEM.len()..].parse() {
                crate::plugins::ui::menu_action(ui, id);
            }
        }
        "undo" | "redo" => {
            // text undo when the editor has the focus, as Qt's webview
            // takes Cmd+Z before the menu does
            if let Some(label) = crate::editor_window::focused_editor(ui) {
                // in the browser, the table's frame handles its own Cmd+Z
                ui.eval(
                    label,
                    format!(
                        "if (document.activeElement?.id !== 'shell-browser') \
                         document.execCommand({:?});",
                        id
                    ),
                );
            } else {
                main_window::action_for_state(ui, id);
            }
        }
        // the card menu's key equivalents (flags, delete) are the main
        // window's, as in Qt, not the editor's
        id if crate::editor_window::focused_editor(ui).is_some()
            && ![
                "decks",
                "add",
                "browse",
                "study",
                "chooseDeck",
                "stats",
                "sync",
                "createFiltered",
            ]
            .contains(&id) => {}
        id => main_window::action_for_state(ui, id),
    }
    crate::editor_window::note_changed_elsewhere(ui);
    refresh(ui);
}

#[cfg(test)]
mod test {
    use super::qt_accelerator;

    #[test]
    fn qt_shortcuts_become_menu_accelerators() {
        assert_eq!(
            qt_accelerator("Ctrl+Alt+Shift+A").as_deref(),
            Some("Cmd+Alt+Shift+A")
        );
        assert_eq!(
            qt_accelerator("Shift+Ctrl+A").as_deref(),
            Some("Cmd+Shift+A")
        );
        assert_eq!(qt_accelerator("Meta+Return").as_deref(), Some("Ctrl+Enter"));
        // the system's keys aren't taken (this one logs out at once)
        assert_eq!(qt_accelerator("Ctrl+Alt+Shift+Q"), None);
        assert_eq!(qt_accelerator("Shift+Alt+Ctrl+Q"), None);
        assert_eq!(qt_accelerator("Ctrl+F5").as_deref(), Some("Cmd+F5"));
        // sequences of chords and unknown keys or modifiers aren't
        assert_eq!(qt_accelerator("Ctrl+K, Ctrl+C"), None);
        assert_eq!(qt_accelerator("Hyper+Q"), None);
        assert_eq!(qt_accelerator("Ctrl+Media"), None);
        assert_eq!(qt_accelerator(""), None);
    }
}
