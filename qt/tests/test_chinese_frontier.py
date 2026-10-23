# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import json
import sys
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from threading import Event

import pytest

ADDONS = Path(__file__).resolve().parents[2] / "addons"
sys.path.insert(0, str(ADDONS))

from chinese_frontier.collection import (  # noqa: E402
    SETTINGS_KEY,
    admit,
    ensure_model,
    initial_scope_settings,
    needs_audit,
    report_confusion,
    request_repair,
    snapshot,
    upgrade_presentation,
)
from chinese_frontier.core import (  # noqa: E402
    Candidate,
    Contrast,
    DeckBudget,
    Source,
    ValidationError,
    admission_slots,
    cloze_words,
    effective_limit,
    plain_text,
    successful_days,
    validate_candidate,
)
from chinese_frontier.generation import (
    GENERATION_POLICY,
    Draft,
    generate,
    review_units,
    validate_review,
)  # noqa: E402
from chinese_frontier.lexicon import Lexicon  # noqa: E402
from chinese_frontier.providers import (  # noqa: E402
    cli_command,
    parse_response,
    subscription_environment,
)
from chinese_frontier.state import State  # noqa: E402

from anki.collection import Collection  # noqa: E402


def audit_report(
    source: Source, candidate: Candidate, *, approved: bool = True
) -> dict:
    alternatives = []
    for (example, ordinal), target in review_units(candidate, source).items():
        word = "千" if target != "千" else "百"
        fits = candidate.mode != "characters"
        alternatives.append(
            {
                "example": example,
                "ordinal": ordinal,
                "word": word,
                "fits_context": False,
                "fits_cloze": fits,
                "reason": "Does not match this situation and translation",
            }
        )
    return {
        "card_id": source.card_id,
        "sentence": candidate.sentence,
        "approved": approved,
        "reason": "clear" if approved else "unnatural",
        "search_summary": "Checked the following competing answers",
        "alternatives": alternatives,
    }


def audited_draft(
    source: Source, candidate: Candidate, *, audit_only: bool = False
) -> Draft:
    return Draft(
        source,
        candidate,
        {
            "version": GENERATION_POLICY,
            "checks": [
                {
                    "candidate": asdict(candidate),
                    "review": audit_report(source, candidate),
                }
            ],
        },
        audit_only,
    )


@pytest.fixture
def collection(tmp_path: Path):
    col = Collection(str(tmp_path / "test.anki2"))
    yield col
    col.close()


def add_word(
    col: Collection,
    word: str,
    *,
    established: bool = False,
    suspended: bool = False,
    deck: int = 1,
):
    note = col.new_note(col.models.by_name("Basic"))
    note["Front"] = word
    note["Back"] = "definition of " + word
    col.add_note(note, deck)
    card = note.cards()[0]
    if established:
        card.type = card.queue = 2
        card.reps = 2
        card.due = col.sched.today + 100
        col.update_card(card)
        cutoff = col.sched.day_cutoff
        for offset in (2, 3):
            timestamp = (cutoff - offset * 86400 + 3600) * 1000 + card.id % 1000
            col.db.execute(
                "insert into revlog values (?,?,?,?,?,?,?,?,?)",
                timestamp,
                card.id,
                0,
                3,
                1,
                1,
                2500,
                1000,
                1,
            )
    if suspended:
        col.sched.suspend_cards([card.id])
    return note, card


def setup_sentence(col: Collection):
    settings = {"enabled": True, "deck_id": 1}
    col.set_config(SETTINGS_KEY, settings)
    for word in ("这", "本", "书", "有", "一", "页"):
        add_word(col, word, established=True)
    note, card = add_word(col, "百", suspended=True)
    current = snapshot(col, settings, {})
    return settings, note, card, audited_draft(current.sources[0], candidate())


@pytest.fixture
def lexicon() -> Lexicon:
    return Lexicon({"这", "本", "书", "有", "一", "百", "页", "学", "生", "学生"})


def source(word: str = "百") -> Source:
    return Source(1, 2, 3, word, "bǎi - hundred", "original")


def candidate(sentence: str = "这本书有一百页。") -> Candidate:
    return Candidate(sentence, "This book has one hundred pages.", "bǎi", "hundred")


def test_whole_word_cloze_has_no_definition_hint(lexicon: Lexicon) -> None:
    result = validate_candidate(
        candidate(), source(), {"这", "本", "书", "有", "一", "页"}, lexicon
    )
    assert result.text == "这本书有一{{c1::百}}页。"
    assert result.dependencies == ("这", "本", "书", "有", "一", "页")


def test_unknown_context_is_rejected(lexicon: Lexicon) -> None:
    with pytest.raises(ValidationError, match="页"):
        validate_candidate(
            candidate(), source(), {"这", "本", "书", "有", "一"}, lexicon
        )


def test_known_characters_do_not_establish_compound(lexicon: Lexicon) -> None:
    with pytest.raises(ValidationError, match="学生"):
        validate_candidate(
            candidate("学生有一百页。"),
            source(),
            {"学", "生", "有", "一", "页"},
            lexicon,
        )


@pytest.mark.parametrize(
    "sentence", ["这本书有一页。", "百百。", "{{c1::百}}。", "<b>百</b>。"]
)
def test_invalid_target_or_markup_is_rejected(sentence: str, lexicon: Lexicon) -> None:
    with pytest.raises(ValidationError):
        validate_candidate(
            candidate(sentence), source(), {"这", "本", "书", "有", "一", "页"}, lexicon
        )


def test_cloze_vocabulary_uses_only_this_live_card_answer() -> None:
    assert cloze_words("{{c1::甘}}{{c2::苦}}。其他字", 0) == ("甘",)
    assert cloze_words("{{c1::甘苦::hardships}}", 0) == ("甘苦",)
    assert cloze_words("{{c2::甘苦}}", 0) == ()


def test_pending_cards_reserve_daily_slots() -> None:
    assert admission_slots([DeckBudget(3, 10, 4, 3, 100, 0, 0)]) == 3


def test_parent_and_sibling_pending_cards_share_budget() -> None:
    parent = DeckBudget(1, 10, 2, 6, 100, 0, 0)
    child = DeckBudget(2, 20, 0, 0, 100, 0, 0)
    assert admission_slots([parent, child]) == 2


def test_review_backlog_blocks_admission() -> None:
    budget = DeckBudget(1, 10, 0, 0, 5, 2, 3)
    assert admission_slots([budget]) == 0
    assert admission_slots([budget], ignore_review_limit=True) == 10


def test_zero_and_exhausted_limits_never_admit() -> None:
    assert admission_slots([DeckBudget(1, 0, 0, 0, 100, 0, 0)]) == 0
    assert admission_slots([DeckBudget(1, 10, 10, 0, 100, 0, 0)]) == 0


def test_context_can_never_use_unrecognised_non_chinese_text(lexicon: Lexicon) -> None:
    with pytest.raises(ValidationError):
        validate_candidate(candidate("ISBN百。"), source(), set(), lexicon)


def test_native_admission_is_new_reversible_and_idempotent(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, original, card, draft = setup_sentence(collection)
    original_fields = list(original.fields)
    result = admit(collection, settings, {}, [draft, draft], lexicon)
    assert len(result.admitted) == 1
    replacement = collection.get_note(result.admitted[0])
    assert replacement["Text"] == "这本书有一{{c1::百}}页。"
    assert "This book has one hundred pages." in replacement.cards()[0].question()
    assert len(replacement.cards()) == 1
    assert replacement.cards()[0].queue == 0
    assert collection.get_card(card.id).queue == -1
    assert collection.get_note(original.id).fields == original_fields
    assert "百" not in snapshot(collection, settings, {}).known
    assert admit(collection, settings, {}, [draft], lexicon).admitted == []
    collection.undo()
    assert collection.find_notes("tag:chinese-frontier::generated") == []
    assert collection.get_card(card.id).queue == -1


def test_admission_rechecks_changed_vocabulary(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, draft = setup_sentence(collection)
    context_card = collection.find_cards("Front:页")[0]
    collection.sched.suspend_cards([context_card])
    result = admit(collection, settings, {}, [draft], lexicon)
    assert result.admitted == []
    assert "页" in result.rejected[draft.source.key]


def test_admission_rechecks_source_unsuspension(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, card, draft = setup_sentence(collection)
    collection.sched.unsuspend_cards([card.id])
    assert admit(collection, settings, {}, [draft], lexicon).admitted == []


def test_admission_rechecks_paused_settings(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, draft = setup_sentence(collection)
    collection.set_config(SETTINGS_KEY, {**settings, "enabled": False})
    result = admit(collection, settings, {}, [draft], lexicon)
    assert result.admitted == []
    assert result.remaining == [draft]


def test_daily_deck_override_and_pending_reservation(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, draft = setup_sentence(collection)
    deck = collection.decks.get(1)
    deck["newLimit"] = 1
    collection.decks.update_dict(deck)
    add_word(collection, "学生")
    assert snapshot(collection, settings, {}).slots(1) == 0
    result = admit(collection, settings, {}, [draft], lexicon)
    assert result.admitted == [] and result.remaining == [draft]


def test_unseen_and_relearning_cards_do_not_establish_vocabulary(
    collection: Collection,
) -> None:
    settings = {"deck_id": 1}
    add_word(collection, "学生")
    _note, card = add_word(collection, "书", established=True)
    timestamp = (collection.sched.day_cutoff - 86400) * 1000
    collection.db.execute(
        "insert into revlog values (?,?,?,?,?,?,?,?,?)",
        timestamp,
        card.id,
        0,
        1,
        1,
        1,
        2500,
        1000,
        1,
    )
    assert snapshot(collection, settings, {}).known == {}


def test_cloze_snapshot_uses_only_reviewed_live_ordinal(collection: Collection) -> None:
    model = collection.models.by_name("Cloze")
    note = collection.new_note(model)
    note["Text"] = "{{c1::甘}}{{c2::苦}}。亲身经历"
    collection.add_note(note, 1)
    first, second = note.cards()
    first.type = first.queue = 2
    first.reps = 2
    first.due = collection.sched.today + 100
    collection.update_card(first)
    for offset in (2, 3):
        timestamp = (collection.sched.day_cutoff - offset * 86400 + 3600) * 1000
        collection.db.execute(
            "insert into revlog values (?,?,?,?,?,?,?,?,?)",
            timestamp,
            first.id,
            0,
            3,
            1,
            1,
            2500,
            1000,
            1,
        )
    current = snapshot(collection, {"deck_id": 1}, {})
    assert set(current.known) == {"甘"}
    assert "苦" not in current.known and "亲身经历" not in current.known
    collection.sched.suspend_cards([first.id])
    assert snapshot(collection, {"deck_id": 1}, {}).known == {}


def test_today_only_override_expires() -> None:
    config = {"new": {"perDay": 4}}
    deck = {"newLimit": 2, "newLimitToday": {"today": 3, "limit": 0}}
    assert effective_limit(deck, config, "new", 3) == 0
    assert effective_limit(deck, config, "new", 4) == 2


def test_success_days_follow_study_rollover() -> None:
    cutoff = int(datetime(2026, 10, 12, 4).timestamp())
    reviews = [
        (int(datetime(2026, 10, 11, hour).timestamp()) * 1000, 3) for hour in (3, 5)
    ]
    assert successful_days(reviews, cutoff) == 2
    assert successful_days([(reviews[0][0], 3), (reviews[0][0] + 1000, 3)], cutoff) == 1
    assert successful_days([*reviews, (reviews[-1][0] + 1000, 1)], cutoff) == 0


def test_codex_cli_uses_subscription_and_schema(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-be-used")
    assert "OPENAI_API_KEY" not in subscription_environment()
    assert "CODEX_API_KEY" not in subscription_environment()
    command = cli_command("codex", "/codex", tmp_path, {})
    assert command[1] == "exec" and command[-1] == "-"
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--ignore-user-config" in command
    assert command[command.index("--output-schema") + 1] == str(
        tmp_path / "schema.json"
    )


def test_claude_cli_disables_tools_and_parses_structured_output(tmp_path: Path) -> None:
    command = cli_command("claude", "/claude", tmp_path, {})
    assert command[command.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in command
    value = {"proposals": []}
    assert (
        parse_response(
            "claude", json.dumps({"structured_output": value, "is_error": False})
        )
        == value
    )
    with pytest.raises(ValueError):
        parse_response("claude", '{"is_error":true}')


def test_journal_survives_restart_and_does_not_catch_up(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = State(drafts=[audited_draft(source(), candidate())], day=1, batches=8)
    state.defer(source().key, "unknown word", "v1")
    state.save(path)
    restored = State.load(path)
    assert restored.drafts == state.drafts
    assert not restored.can_try(source().key, "v1")
    assert restored.can_try(source().key, "v2")
    restored.reset_day(10)
    assert restored.batches == 0


def test_generation_requires_independent_language_approval(
    tmp_path: Path, lexicon: Lexicon, monkeypatch
) -> None:
    from chinese_frontier import generation

    monkeypatch.setattr(generation.Lexicon, "load", lambda _folder: lexicon)
    responses = iter(
        [
            {"proposals": [{"card_id": 1, "candidates": [candidate().__dict__]}]},
            {
                "reviews": [
                    {
                        "card_id": 1,
                        "sentence": candidate().sentence,
                        "approved": False,
                        "reason": "unnatural",
                        "search_summary": "Checked competitors",
                        "alternatives": audit_report(source(), candidate())[
                            "alternatives"
                        ],
                    }
                ]
            },
            {"proposals": [{"card_id": 1, "candidates": []}]},
        ]
    )
    monkeypatch.setattr(
        generation.CliGenerator, "__init__", lambda self, _config, _stop: None
    )
    monkeypatch.setattr(generation.CliGenerator, "check_login", lambda self: None)
    monkeypatch.setattr(
        generation.CliGenerator,
        "request",
        lambda self, _prompt, _schema: next(responses),
    )
    result = generate(
        [source()],
        {word: "familiar" for word in ("这", "本", "书", "有", "一", "页")},
        {},
        tmp_path,
        Event(),
    )
    assert result.drafts == []
    assert json.loads(result.deferred[source().key])["reason"] == "unnatural"
    assert json.loads(result.deferred[source().key])["alternatives"]


def test_failed_render_verification_rolls_back_admission(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, draft = setup_sentence(collection)
    model = ensure_model(collection)
    model["tmpls"][0]["qfmt"] = "<div>{{cloze:Text}}</div>"
    collection.models.update_dict(model)
    with pytest.raises(ValueError, match="rendering"):
        admit(collection, settings, {}, [draft], lexicon)
    assert collection.find_notes("tag:chinese-frontier::generated") == []


def test_parent_budget_is_reserved_across_multiple_children(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, draft = setup_sentence(collection)
    child = collection.decks.add_normal_deck_with_name("Default::A").id
    sibling = collection.decks.add_normal_deck_with_name("Default::B").id
    _note, source_card = add_word(collection, "半", suspended=True, deck=child)
    add_word(collection, "学生", deck=sibling)
    deck = collection.decks.get(1)
    deck["newLimit"] = 1
    collection.decks.update_dict(deck)
    current = snapshot(collection, settings, {})
    assert current.slots(child) == 0
    assert current.slots(1) == 0


def test_reviewed_dictionary_cloze_does_not_prevent_sentence_replacement(
    collection: Collection,
) -> None:
    settings, _note, _card, _draft = setup_sentence(collection)
    note = collection.new_note(collection.models.by_name("Cloze"))
    note["Text"] = "{{c1::百}} hundred"
    collection.add_note(note, 1)
    current = snapshot(collection, settings, {})
    assert any(source.word == "百" for source in current.sources)


def test_bad_sentence_is_replaced_without_resetting_history_or_using_new_slots(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _original, _card, draft = setup_sentence(collection)
    first = admit(collection, settings, {}, [draft], lexicon)
    note_id = first.admitted[0]
    card = collection.get_note(note_id).cards()[0]
    card.type = card.queue = 2
    card.reps = 12
    card.ivl = 100
    card.due = collection.sched.today + 100
    collection.update_card(card)
    tagged_note = collection.get_note(note_id)
    tagged_note.tags.append("personal-tag")
    collection.update_note(tagged_note)
    request_repair(collection, note_id)
    deck = collection.decks.get(1)
    deck["newLimit"] = 0
    collection.decks.update_dict(deck)
    current = snapshot(collection, settings, {})
    assert current.repairs[draft.source.key] == note_id
    updated = audited_draft(current.sources[0], candidate("这本书有百页。"))
    result = admit(collection, settings, {}, [updated], lexicon)
    assert result.admitted == [] and result.updated == [note_id]
    new_card = collection.get_note(note_id).cards()[0]
    assert (new_card.id, new_card.reps, new_card.ivl, new_card.due) == (
        card.id,
        12,
        100,
        card.due,
    )
    assert "chinese-frontier::repair" not in collection.get_note(note_id).tags
    assert "personal-tag" in collection.get_note(note_id).tags


def test_repair_cannot_clear_request_by_repeating_bad_sentence(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _original, _card, draft = setup_sentence(collection)
    first = admit(collection, settings, {}, [draft], lexicon)
    request_repair(collection, first.admitted[0])
    current = snapshot(collection, settings, {})
    repeated = audited_draft(current.sources[0], draft.candidate)
    result = admit(collection, settings, {}, [repeated], lexicon)
    assert result.updated == []
    assert result.rejected
    assert "chinese-frontier::repair" in collection.get_note(first.admitted[0]).tags


def test_active_sibling_outside_source_deck_prevents_conversion(
    collection: Collection,
) -> None:
    model = collection.models.by_name("Basic (and reversed card)")
    note = collection.new_note(model)
    note["Front"] = "百"
    note["Back"] = "hundred"
    collection.add_note(note, 1)
    front, reverse = note.cards()
    other_deck = collection.decks.add_normal_deck_with_name("Other").id
    reverse.did = other_deck
    collection.update_card(reverse)
    collection.sched.suspend_cards([front.id])
    assert snapshot(collection, {"deck_id": 1}, {}).sources == []


def test_corrupt_dictionary_cache_does_not_fall_back_to_characters(
    tmp_path: Path,
) -> None:
    (tmp_path / "cedict-headwords.txt").write_text("学\n生\n", encoding="utf-8")
    with pytest.raises(ValueError, match="dictionary"):
        Lexicon.load(tmp_path)


def test_additional_hsk_sources_use_primary_deck_budget(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, _draft = setup_sentence(collection)
    hsk = collection.decks.id("HSK6 - 1000")
    note, card = add_word(collection, "百 1", suspended=True, deck=hsk)
    settings["source_deck_ids"] = [hsk]
    collection.set_config(SETTINGS_KEY, settings)
    primary = collection.decks.get(1)
    primary["newLimit"] = 1
    collection.decks.update_dict(primary)
    current = snapshot(collection, settings, {})
    imported = next(source for source in current.sources if source.card_id == card.id)
    assert imported.word == "百" and imported.deck_id == 1
    assert imported.hsk_level == 1
    result = admit(
        collection, settings, {}, [audited_draft(imported, candidate())], lexicon
    )
    assert len(result.admitted) == 1
    assert collection.get_note(result.admitted[0]).cards()[0].did == 1
    assert collection.get_card(card.id).queue == -1
    assert collection.get_note(note.id)["Front"] == "百 1"
    assert snapshot(collection, settings, {}).slots(1) == 0


def test_hsk_marker_is_metadata_not_known_vocabulary(collection: Collection) -> None:
    hsk = collection.decks.id("HSK6 - 1000")
    add_word(collection, "学生 1", established=True, deck=hsk)
    add_word(collection, "书 1", suspended=True, deck=hsk)
    current = snapshot(collection, {"deck_id": 1, "vocabulary_deck_ids": [hsk]}, {})
    assert set(current.known) == {"学生"}
    assert current.sources == []


def test_hsk_level_orders_targets_before_old_due_position(
    collection: Collection,
) -> None:
    hsk = collection.decks.id("HSK6 - 2000")
    for word in ("精心 6", "书 1", "翻 6"):
        add_word(collection, word, suspended=True, deck=hsk)
    current = snapshot(collection, {"deck_id": 1, "source_deck_ids": [hsk]}, {})
    assert [source.word for source in current.sources] == ["书", "精心", "翻"]


def test_hsk_like_numbers_in_other_fronts_are_not_stripped(
    collection: Collection,
) -> None:
    add_word(collection, "书 1", suspended=True)
    assert snapshot(collection, {"deck_id": 1}, {}).sources == []


def test_scope_bootstrap_preserves_pause_and_saved_choices(
    collection: Collection,
) -> None:
    hsk = collection.decks.id("HSK6 - 1000")
    settings = {"deck_id": 1, "enabled": False}
    collection.set_config(SETTINGS_KEY, settings)
    config = {
        "initial_collection_path": collection.path,
        "initial_source_deck_names": ["HSK6 - 1000"],
    }
    updated = initial_scope_settings(collection, config)
    assert updated == {**settings, "source_deck_ids": [hsk]}
    collection.set_config(SETTINGS_KEY, updated)
    assert initial_scope_settings(collection, config) is None
    collection.set_config(SETTINGS_KEY, {**settings, "source_deck_ids": []})
    assert initial_scope_settings(collection, config) is None
    assert (
        initial_scope_settings(
            collection, {**config, "initial_collection_path": "other.anki2"}
        )
        is None
    )


def test_known_whole_word_is_not_introduced_again_from_hsk(
    collection: Collection,
) -> None:
    hsk = collection.decks.id("HSK6 - 1000")
    add_word(collection, "书", established=True)
    add_word(collection, "书 1", suspended=True, deck=hsk)
    current = snapshot(collection, {"deck_id": 1, "source_deck_ids": [hsk]}, {})
    assert "书" in current.known and current.sources == []


def test_extra_source_decks_supply_targets_but_not_context(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, _draft = setup_sentence(collection)
    hsk = collection.decks.id("HSK6 - 1000")
    add_word(collection, "学生 1", established=True, deck=hsk)
    _note, target = add_word(collection, "半 1", suspended=True, deck=hsk)
    settings["source_deck_ids"] = [hsk]
    collection.set_config(SETTINGS_KEY, settings)
    current = snapshot(collection, settings, {})
    assert "学生" not in current.known
    assert "学生" not in current.evidence
    assert any(source.card_id == target.id for source in current.sources)
    hundred = next(source for source in current.sources if source.word == "百")
    rejected = admit(
        collection,
        settings,
        {},
        [audited_draft(hundred, candidate("学生有一百页。"))],
        lexicon,
    )
    assert rejected.admitted == [] and "学生" in rejected.rejected[hundred.key]
    explicit_context = {**settings, "vocabulary_deck_ids": [hsk]}
    assert "学生" in snapshot(collection, explicit_context, {}).known


def test_live_cloze_in_target_only_deck_does_not_cover_primary_target(
    collection: Collection,
) -> None:
    hsk = collection.decks.id("HSK6 - 1000")
    note = collection.new_note(collection.models.by_name("Cloze"))
    note["Text"] = "这是{{c1::百}}页的书。"
    collection.add_note(note, hsk)
    add_word(collection, "百", suspended=True)
    current = snapshot(collection, {"deck_id": 1, "source_deck_ids": [hsk]}, {})
    assert "百" not in current.covered_words
    assert [source.word for source in current.sources] == ["百"]


def test_generation_keeps_informative_order_not_shortest(
    tmp_path: Path, lexicon: Lexicon, monkeypatch
) -> None:
    from chinese_frontier import generation

    longer = candidate()
    shorter = candidate("书有一百页。")
    responses = iter(
        [
            {
                "proposals": [
                    {"card_id": 1, "candidates": [longer.__dict__, shorter.__dict__]}
                ]
            },
            {
                "reviews": [
                    {
                        "card_id": 1,
                        "sentence": c.sentence,
                        "approved": True,
                        "reason": "clear",
                        "search_summary": "Checked competitors",
                        "alternatives": audit_report(source(), c)["alternatives"],
                    }
                    for c in (longer, shorter)
                ]
            },
        ]
    )
    monkeypatch.setattr(generation.Lexicon, "load", lambda _folder: lexicon)
    monkeypatch.setattr(
        generation.CliGenerator, "__init__", lambda self, _config, _stop: None
    )
    monkeypatch.setattr(generation.CliGenerator, "check_login", lambda self: None)
    monkeypatch.setattr(
        generation.CliGenerator,
        "request",
        lambda self, _prompt, _schema: next(responses),
    )
    result = generate(
        [source()],
        {word: "familiar" for word in ("这", "本", "书", "有", "一", "页")},
        {},
        tmp_path,
        Event(),
    )
    assert result.drafts[0].candidate == longer


def test_existing_template_gets_front_translation_without_history_change(
    collection: Collection,
) -> None:
    model = ensure_model(collection)
    model["tmpls"][0]["qfmt"] = '<div class="sentence">{{cloze:Text}}</div>'
    collection.models.update_dict(model)
    note = collection.new_note(model)
    note["Text"] = "这本书有一{{c1::百}}页。"
    note["Translation"] = "This book has one hundred pages."
    collection.add_note(note, 1)
    before = note.cards()[0]
    before.reps = 7
    before.ivl = 15
    collection.update_card(before)
    ensure_model(collection)
    after = collection.get_card(before.id)
    assert "This book has one hundred pages." in after.question()
    assert "百" not in plain_text(after.question())
    assert (after.id, after.reps, after.ivl) == (before.id, 7, 15)


def test_presentation_upgrade_strips_old_hints_only_from_generated_notes(
    collection: Collection,
) -> None:
    model = ensure_model(collection)
    generated = collection.new_note(model)
    generated["Text"] = "请等候五{{c1::分钟::A unit of time lasting sixty seconds.}}。"
    generated.tags = ["chinese-frontier::generated"]
    collection.add_note(generated, 1)
    manual = collection.new_note(model)
    manual["Text"] = generated["Text"]
    collection.add_note(manual, 1)
    card = generated.cards()[0]
    upgrade_presentation(collection)
    assert collection.get_note(generated.id)["Text"] == "请等候五{{c1::分钟}}。"
    assert collection.get_note(manual.id)["Text"] == manual["Text"]
    assert collection.get_note(generated.id).cards()[0].id == card.id
    assert not upgrade_presentation(collection).note


def test_old_generation_policy_drops_drafts_without_resetting_daily_budget(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    State(drafts=[audited_draft(source(), candidate())], day=1, batches=8).save(path)
    data = json.loads(path.read_text())
    data.pop("generation_policy", None)
    path.write_text(json.dumps(data))
    restored = State.load(path)
    assert restored.drafts == []
    assert (restored.day, restored.batches) == (1, 8)


def test_character_fallback_keeps_other_character_visible(lexicon: Lexicon) -> None:
    c = replace(candidate("学生有一页。"), mode="characters")
    valid = validate_candidate(c, source("学生"), {"有", "一", "页"}, lexicon)
    assert valid.text == "{{c1::学}}{{c2::生}}有一页。"
    assert valid.card_count == 2


def test_audit_field_is_editable_but_hidden_and_disambiguation_label_is_on_both_sides(
    collection: Collection,
) -> None:
    model = ensure_model(collection)
    note = collection.new_note(model)
    note["Text"] = "{{c1::推}}辞。"
    note["Translation"] = "Decline."
    note["CardKind"] = "Disambiguation"
    note["Disambiguation"] = "PRIVATE-AUDIT 拒绝 fits the whole-word blank"
    collection.add_note(note, 1)
    c = note.cards()[0]
    assert "Disambiguation" in c.question() and "Disambiguation" in c.answer()
    assert "PRIVATE-AUDIT" not in c.question() and "PRIVATE-AUDIT" not in c.answer()


def test_model_approval_cannot_override_a_fitting_alternative() -> None:
    c, target = candidate(), source()
    report = audit_report(target, c)
    report["alternatives"][0]["fits_context"] = True
    assert not validate_review(report, c, target)


def test_character_review_requires_both_sibling_checks() -> None:
    c = replace(candidate("学生有一页。"), mode="characters")
    report = audit_report(source("学生"), c)
    report["alternatives"].pop()
    with pytest.raises(ValidationError, match="omitted"):
        validate_review(report, c, source("学生"))


def student_target(col: Collection):
    settings, _note, _card, _draft = setup_sentence(col)
    add_word(col, "学生", suspended=True)
    target = next(s for s in snapshot(col, settings, {}).sources if s.word == "学生")
    return (
        settings,
        target,
        Candidate("学生有一页。", "The student has one page.", "xué shēng", ""),
    )


def test_character_fallback_stages_siblings_under_one_card_daily_limit(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, target, c = student_target(collection)
    deck = collection.decks.get(1)
    deck["newLimit"] = 1
    collection.decks.update_dict(deck)
    draft = audited_draft(target, replace(c, mode="characters"))
    result = admit(collection, settings, {}, [draft], lexicon)
    assert len(result.admitted) == 1 and len(result.remaining) == 1
    note = collection.get_note(result.admitted[0])
    assert note["Text"] == "{{c1::学}}生有一页。"
    assert len(note.cards()) == 1
    assert "pending" in note["Disambiguation"]
    assert snapshot(collection, settings, {}).slots(1) == 0
    deck = collection.decks.get(1)
    deck["newLimit"] = 2
    collection.decks.update_dict(deck)
    second = admit(collection, settings, {}, result.remaining, lexicon)
    assert second.updated == [note.id]
    assert len(collection.get_note(note.id).cards()) == 2
    assert snapshot(collection, settings, {}).slots(1) == 0


def test_existing_card_gets_character_cue_without_creating_unaffordable_sibling(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, target, c = student_target(collection)
    first = admit(collection, settings, {}, [audited_draft(target, c)], lexicon)
    note = collection.get_note(first.admitted[0])
    old = note.cards()[0]
    old.type = old.queue = 2
    old.reps = 8
    old.ivl = 30
    collection.update_card(old)
    request_repair(collection, note.id)
    target = next(
        s for s in snapshot(collection, settings, {}).sources if s.word == "学生"
    )
    deck = collection.decks.get(1)
    deck["newLimit"] = 0
    collection.decks.update_dict(deck)
    result = admit(
        collection,
        settings,
        {},
        [audited_draft(target, replace(c, mode="characters"))],
        lexicon,
    )
    after = collection.get_note(note.id)
    assert after["Text"] == "{{c1::学}}生有一页。"
    queued = next(
        s for s in snapshot(collection, settings, {}).sources if s.word == "学生"
    )
    assert queued.reported_confusions == ()
    assert len(after.cards()) == 1 and result.remaining
    assert (after.cards()[0].id, after.cards()[0].reps, after.cards()[0].ivl) == (
        old.id,
        8,
        30,
    )


def fake_generator(monkeypatch, lexicon: Lexicon, responses: list) -> list[str]:
    from chinese_frontier import generation

    replies = iter(responses)
    prompts: list[str] = []

    def request(self, prompt, schema):
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr(generation.Lexicon, "load", lambda _folder: lexicon)
    monkeypatch.setattr(
        generation.CliGenerator, "__init__", lambda self, _config, _stop: None
    )
    monkeypatch.setattr(generation.CliGenerator, "check_login", lambda self: None)
    monkeypatch.setattr(generation.CliGenerator, "request", request)
    return prompts


def test_ambiguous_word_examples_never_escalate_to_contrast(
    tmp_path: Path, lexicon: Lexicon, monkeypatch
) -> None:
    target = source("学生")
    word = Candidate("学生有一页。", "The student has one page.", "xué shēng", "")
    char = replace(
        word, mode="characters", explanation="The other character identifies the word."
    )
    ambiguous = audit_report(target, word)
    ambiguous["alternatives"][0].update(word="学者", fits_context=True)
    prompts = fake_generator(
        monkeypatch,
        lexicon,
        [
            {"proposals": [{"card_id": 1, "candidates": [asdict(word)]}]},
            {"reviews": [ambiguous]},
            {"proposals": [{"card_id": 1, "candidates": [asdict(char)]}]},
            {"reviews": [audit_report(target, char)]},
        ],
    )
    result = generate(
        [target],
        {word: "familiar" for word in ("有", "一", "页")},
        {},
        tmp_path,
        Event(),
    )
    assert result.drafts[0].candidate.mode == "characters"
    assert not any('mode="contrast"' in prompt for prompt in prompts)
    assert 'mode="characters"' in prompts[2]
    checks = result.drafts[0].audit["checks"]
    assert checks[0]["review"]["alternatives"][0]["word"] == "学者"
    assert checks[-1]["candidate"]["mode"] == "characters"


KNOWN_PAGE_WORDS = {w: "familiar" for w in ("这", "本", "书", "有", "一", "页")}


def thousand_contrast() -> Candidate:
    return replace(
        candidate(),
        mode="contrast",
        explanation="百 is a hundred; 千 is a thousand.",
        contrast=Contrast(
            "千",
            "这本书有一千页。",
            "This book has one thousand pages.",
            "qiān",
            "thousand",
        ),
    )


def test_reported_confusion_is_repaired_only_as_a_contrast_with_that_word(
    tmp_path: Path, lexicon: Lexicon, monkeypatch
) -> None:
    target = replace(
        source(),
        reported_confusions=("千",),
        existing_cards=1,
        existing_candidate=asdict(candidate()),
    )
    pair = thousand_contrast()
    prompts = fake_generator(
        monkeypatch,
        lexicon,
        [
            {"proposals": [{"card_id": 1, "candidates": [asdict(pair)]}]},
            {"reviews": [audit_report(target, pair)]},
        ],
    )
    result = generate([target], KNOWN_PAGE_WORDS, {}, tmp_path, Event())
    # The old single example does not answer the confusion, so it is not re-audited.
    assert len(prompts) == 2
    assert 'mode="contrast"' in prompts[0] and "千" in prompts[0]
    assert result.drafts[0].candidate == pair


def test_failed_confusion_contrast_defers_instead_of_falling_back(
    tmp_path: Path, lexicon: Lexicon, monkeypatch
) -> None:
    target = replace(source("学生"), reported_confusions=("学者",))
    prompts = fake_generator(
        monkeypatch,
        lexicon,
        [{"proposals": [{"card_id": 1, "candidates": []}]}],
    )
    result = generate([target], KNOWN_PAGE_WORDS, {}, tmp_path, Event())
    assert len(prompts) == 1 and result.drafts == []
    assert target.key in result.deferred


def test_contrast_needs_a_reported_competitor(lexicon: Lexicon) -> None:
    known = set(KNOWN_PAGE_WORDS) | {"千"}
    with pytest.raises(ValidationError):
        validate_candidate(thousand_contrast(), source(), known, lexicon)
    reported = replace(source(), reported_confusions=("千",))
    assert validate_candidate(thousand_contrast(), reported, known, lexicon).text == (
        "<div>这本书有一{{c1::百}}页。</div><br><div>这本书有一{{c1::千}}页。</div>"
    )


def test_existing_note_without_current_audit_is_queued_and_audit_only_preserves_text(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, draft = setup_sentence(collection)
    result = admit(collection, settings, {}, [draft], lexicon)
    note = collection.get_note(result.admitted[0])
    original_text = note["Text"]
    note["Disambiguation"] = ""
    collection.update_note(note)
    target = next(
        s for s in snapshot(collection, settings, {}).sources if s.word == "百"
    )
    assert target.existing_candidate and target.existing_cards == 1
    assert target.key in snapshot(collection, settings, {}).repairs
    applied = admit(
        collection,
        settings,
        {},
        [
            audited_draft(
                target, Candidate.from_dict(target.existing_candidate), audit_only=True
            )
        ],
        lexicon,
    )
    assert applied.updated == [note.id]
    assert collection.get_note(note.id)["Text"] == original_text
    assert not snapshot(collection, settings, {}).repairs


def test_manual_sentence_edit_during_audit_is_not_overwritten(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, draft = setup_sentence(collection)
    note = collection.get_note(
        admit(collection, settings, {}, [draft], lexicon).admitted[0]
    )
    note["Disambiguation"] = ""
    collection.update_note(note)
    target = next(
        s for s in snapshot(collection, settings, {}).sources if s.word == "百"
    )
    prepared = audited_draft(
        target, Candidate.from_dict(target.existing_candidate), audit_only=True
    )
    note["Text"] = "这本书有{{c1::百}}页。"
    collection.update_note(note)
    result = admit(collection, settings, {}, [prepared], lexicon)
    assert result.rejected and not result.updated
    assert collection.get_note(note.id)["Text"] == note["Text"]


def test_character_draft_can_progress_without_all_sibling_slots() -> None:
    target = source("学生")
    c = replace(candidate("学生有一页。"), mode="characters")
    draft = audited_draft(target, c)
    assert not draft.can_progress(0)
    assert draft.can_progress(1)
    old = replace(
        target, existing_cards=1, existing_candidate=asdict(replace(c, mode="word"))
    )
    assert replace(draft, source=old).can_progress(0)
    partial = replace(old, existing_candidate=asdict(c))
    assert not replace(draft, source=partial).can_progress(0)
    assert replace(draft, source=partial).can_progress(1)


def test_character_fallback_counts_all_distinct_characters(lexicon: Lexicon) -> None:
    c = Candidate(
        "出租车有一页。", "The taxi has one page.", "chū zū chē", "", "characters"
    )
    result = validate_candidate(c, source("出租车"), {"有", "一", "页"}, lexicon)
    assert result.card_count == 3
    assert result.text == "{{c1::出}}{{c2::租}}{{c3::车}}有一页。"
    assert len(review_units(c, source("出租车"))) == 3
    repeated = replace(c, sentence="往往有一页。")
    with pytest.raises(ValidationError):
        validate_candidate(repeated, source("往往"), {"有", "一", "页"}, lexicon)


def test_deferred_audit_is_logged_without_changing_existing_card(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _original, _card, initial = setup_sentence(collection)
    note = collection.get_note(
        admit(collection, settings, {}, [initial], lexicon).admitted[0]
    )
    text, card_id = note["Text"], note.cards()[0].id
    note["Disambiguation"] = ""
    collection.update_note(note)
    target = next(
        s for s in snapshot(collection, settings, {}).sources if s.word == "百"
    )
    c = Candidate.from_dict(target.existing_candidate)
    draft = audited_draft(target, c, audit_only=True)
    draft.audit.update(status="deferred", reason="A competing answer still fits")
    draft.audit["checks"][-1]["review"]["approved"] = False
    result = admit(collection, settings, {}, [draft], lexicon)
    after = collection.get_note(note.id)
    assert result.updated == [note.id] and result.rejected
    assert after["Text"] == text and after.cards()[0].id == card_id
    assert "competing answer" in after["Disambiguation"]


def test_reported_confusion_becomes_one_labelled_contrast_card_keeping_history(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, initial = setup_sentence(collection)
    note = collection.get_note(
        admit(collection, settings, {}, [initial], lexicon).admitted[0]
    )
    card = note.cards()[0]
    card.type = card.queue = 2
    card.reps, card.ivl = 6, 20
    collection.update_card(card)
    report_confusion(collection, note.id, "千")
    target = next(
        s for s in snapshot(collection, settings, {}).sources if s.word == "百"
    )
    assert target.reported_confusions == ("千",) and target.force_repair
    c = thousand_contrast()
    result = admit(collection, settings, {}, [audited_draft(target, c)], lexicon)
    assert result.updated == [note.id]
    after = collection.get_note(note.id)
    assert len(after.cards()) == 1
    card = after.cards()[0]
    assert (card.id, card.reps, card.ivl) == (note.cards()[0].id, 6, 20)
    assert card.question().count('class="cloze"') == 2
    assert "Disambiguation" in card.question() and "Disambiguation" in card.answer()
    assert "Checked the following" not in card.answer()
    assert (
        c.translation in card.question() and c.contrast.translation in card.question()
    )
    assert (
        json.loads(after["Source"].replace("&quot;", '"'))["disambiguation"]["with"]
        == "千"
    )


@pytest.mark.parametrize("word", ["百", "hundred", ""])
def test_reported_confusion_must_be_another_chinese_word(
    collection: Collection, lexicon: Lexicon, word: str
) -> None:
    settings, _note, _card, initial = setup_sentence(collection)
    note_id = admit(collection, settings, {}, [initial], lexicon).admitted[0]
    with pytest.raises(ValueError):
        report_confusion(collection, note_id, word)


def test_draft_written_before_a_report_cannot_clear_the_confusion(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, initial = setup_sentence(collection)
    note_id = admit(collection, settings, {}, [initial], lexicon).admitted[0]
    request_repair(collection, note_id)
    stale = audited_draft(
        snapshot(collection, settings, {}).sources[0], candidate("这本书有百页。")
    )
    report_confusion(collection, note_id, "千")
    result = admit(collection, settings, {}, [stale], lexicon)
    assert result.updated == [] and result.rejected
    assert "chinese-frontier::repair" in collection.get_note(note_id).tags


def test_confusion_on_character_cards_does_not_block_their_repair(
    collection: Collection, lexicon: Lexicon
) -> None:
    import html as html_module

    settings, target, c = student_target(collection)
    note = collection.get_note(
        admit(
            collection,
            settings,
            {},
            [audited_draft(target, replace(c, mode="characters"))],
            lexicon,
        ).admitted[0]
    )
    assert len(note.cards()) == 2
    data = json.loads(html_module.unescape(note["Source"]))
    data["disambiguation"] = {"with": "学者"}
    note["Source"] = html_module.escape(json.dumps(data, ensure_ascii=False))
    collection.update_note(note)
    request_repair(collection, note.id)
    queued = next(
        s for s in snapshot(collection, settings, {}).sources if s.word == "学生"
    )
    # A pair would remove a sibling card, so the note keeps its character repair.
    assert queued.reported_confusions == ()


def test_second_reported_confusion_keeps_the_first(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, initial = setup_sentence(collection)
    note_id = admit(collection, settings, {}, [initial], lexicon).admitted[0]
    original = collection.get_note(note_id)["Text"]
    report_confusion(collection, note_id, "千")
    report_confusion(collection, note_id, "万")
    report_confusion(collection, note_id, "千")
    target = snapshot(collection, settings, {}).sources[0]
    assert target.reported_confusions == ("千", "万")
    data = json.loads(collection.get_note(note_id)["Source"].replace("&quot;", '"'))
    assert data["disambiguation"]["previous_text"] == original


def contrast_note(collection: Collection, source_extra: dict) -> tuple:
    import html as html_module

    note = collection.new_note(ensure_model(collection))
    note["Text"] = (
        "<div>这本书有一{{c1::百}}页。</div><br><div>这本书有一{{c1::千}}页。</div>"
    )
    note["Translation"] = (
        "<div>This book has one hundred pages.</div><br>"
        "<div>This book has one thousand pages.</div>"
    )
    note["Pinyin"] = "bǎi / qiān"
    note["Target"] = "百 / 千"
    note["Meaning"] = "百 is a hundred; 千 is a thousand."
    note["CardKind"] = "Disambiguation"
    note["Source"] = html_module.escape(
        json.dumps(
            {
                "word": "百",
                "definition": "bǎi - hundred",
                "key": "k",
                "mode": "contrast",
                "tested_words": {"百": "bǎi - hundred", "千": "thousand"},
                **source_extra,
            },
            ensure_ascii=False,
        )
    )
    note.tags = ["chinese-frontier::generated", "disambiguation"]
    collection.add_note(note, 1)
    card = note.cards()[0]
    card.type = card.queue = 2
    card.reps, card.ivl = 5, 12
    collection.update_card(card)
    return note, card


def test_upgrade_reverts_unreported_contrast_to_its_first_example(
    collection: Collection,
) -> None:
    note, card = contrast_note(collection, {})
    reported, _ = contrast_note(collection, {"disambiguation": {"with": "千"}})
    upgrade_presentation(collection)
    after = collection.get_note(note.id)
    assert after["Text"] == "这本书有一{{c1::百}}页。"
    assert after["Translation"] == "This book has one hundred pages."
    assert (after["Pinyin"], after["Target"]) == ("bǎi", "百")
    assert (after["Meaning"], after["CardKind"]) == ("bǎi - hundred", "")
    assert "disambiguation" not in after.tags
    source_data = json.loads(after["Source"].replace("&quot;", '"'))
    assert source_data["mode"] == "word"
    assert source_data["tested_words"] == {"百": "bǎi - hundred"}
    kept = after.cards()[0]
    assert (kept.id, kept.reps, kept.ivl) == (card.id, 5, 12)
    assert needs_audit(dict(after.items()))
    assert collection.get_note(reported.id)["Text"] == reported["Text"]
    assert "disambiguation" in collection.get_note(reported.id).tags
    assert not upgrade_presentation(collection).note


def test_character_word_needs_all_siblings_learned_for_context(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, target, c = student_target(collection)
    result = admit(
        collection,
        settings,
        {},
        [audited_draft(target, replace(c, mode="characters"))],
        lexicon,
    )
    note = collection.get_note(result.admitted[0])
    cards = note.cards()
    for index, card in enumerate(cards):
        card.type = card.queue = 2
        card.reps = 2
        card.due = collection.sched.today + 100
        collection.update_card(card)
        for offset in (2, 3):
            collection.db.execute(
                "insert into revlog values (?,?,?,?,?,?,?,?,?)",
                (collection.sched.day_cutoff - offset * 86400 + 3600) * 1000
                + card.id % 1000,
                card.id,
                0,
                3,
                1,
                1,
                2500,
                1000,
                1,
            )
        known = snapshot(collection, settings, {}).known
        assert ("学生" in known) == (index == 1)
        assert "学" not in known and "生" not in known


@pytest.mark.parametrize(
    "old_label", ["Disambiguation · contrast pair", "Disambiguation · character recall"]
)
def test_existing_disambiguation_labels_are_shortened(
    collection: Collection, old_label: str
) -> None:
    note = collection.new_note(ensure_model(collection))
    note["Text"] = "{{c1::推}}辞。"
    note["CardKind"] = old_label
    note.tags = ["chinese-frontier::generated"]
    collection.add_note(note, 1)
    cid = note.cards()[0].id
    upgrade_presentation(collection)
    updated = collection.get_note(note.id)
    assert updated["CardKind"] == "Disambiguation"
    assert updated.cards()[0].id == cid
    assert old_label not in updated.cards()[0].question()
    assert old_label not in updated.cards()[0].answer()


@pytest.fixture
def refill_controller(monkeypatch):
    from types import SimpleNamespace

    from chinese_frontier import runtime

    scheduled = []
    monkeypatch.setattr(
        runtime,
        "mw",
        SimpleNamespace(
            taskman=SimpleNamespace(
                run_in_background=lambda work, done, **kwargs: scheduled.append(
                    (work, done)
                )
            )
        ),
    )
    controller = runtime.Controller.__new__(runtime.Controller)
    controller.valid = lambda epoch: True
    controller.save_state = lambda: None
    controller.schedule_next = lambda: None
    controller.state = State(day=1, batches=8)
    controller.busy = True
    controller.stop = Event()
    controller.dictionary_folder = Path("/tmp")
    current = runtime.Snapshot(
        1,
        {"这": "this"},
        {},
        [source()],
        {3: [DeckBudget(3, 10, 0, 8, 150, 0, 10)]},
        False,
        set(),
        set(),
    )
    return controller, current, scheduled


def test_chinese_automatically_refills_beyond_legacy_daily_cap(refill_controller):
    controller, current, scheduled = refill_controller
    controller.scanned(current, {}, {"max_batches_per_day": 8}, 0)
    assert len(scheduled) == 1
    assert controller.state.batches == 9


def test_chinese_does_not_generate_when_native_slots_are_reserved(refill_controller):
    controller, current, scheduled = refill_controller
    current.budgets[3] = [DeckBudget(3, 10, 0, 10, 150, 0, 10)]
    controller.scanned(current, {}, {}, 0)
    assert not scheduled
    assert controller.state.batches == 8
    assert "No admission slots" in controller.status


def test_chinese_manual_daily_cap_still_applies_when_auto_fill_is_disabled(
    refill_controller,
):
    controller, current, scheduled = refill_controller
    controller.scanned(current, {}, {"auto_fill": False, "max_batches_per_day": 8}, 0)
    assert not scheduled and controller.state.batches == 8


def test_deferred_chinese_targets_cool_down_without_spending_a_batch(refill_controller):
    from chinese_frontier.state import vocabulary_version

    controller, current, scheduled = refill_controller
    controller.state.batches = 0
    controller.state.defer(source().key, "ambiguous", vocabulary_version(current.known))
    controller.scanned(current, {}, {}, 0)
    assert not scheduled and controller.state.batches == 0
    assert controller.state.refill_at > 0


def test_chinese_refill_statistics_and_cooldown_survive_restart(tmp_path):
    path = tmp_path / "state.json"
    state = State(day=1, batches=8)
    assert state.can_refill(2, 0, {}, 100)
    state.reserve_batch()
    state.record_batch(4, 1, {}, 100)
    state.pause_refill({}, 100)
    state.save(path)
    restored = State.load(path)
    assert not restored.can_refill(2, 0, {}, 101)
    assert (restored.batches, restored.attempted, restored.approved) == (9, 4, 1)
    assert restored.can_refill(2, 0, {}, 1900)
    restored.reset_day(2)
    assert restored.batches == restored.attempted == restored.approved == 0
    assert restored.refill_at == 0


def test_rejected_chinese_repair_audits_do_not_inflate_approval_yield(
    refill_controller, lexicon
):
    from concurrent.futures import Future

    from chinese_frontier.generation import GenerationResult

    controller, current, scheduled = refill_controller
    controller.apply = lambda *args: None
    accepted = audited_draft(source(), candidate())
    rejected = replace(accepted, audit_only=True)
    future = Future()
    future.set_result(GenerationResult([accepted, rejected], {}, lexicon))
    controller.generated(future, {}, {}, "v1", 0, attempted=4)
    assert controller.state.attempted == 4 and controller.state.approved == 1


def test_chinese_runtime_resumes_on_the_same_day_after_cycle_cooldown(
    refill_controller, monkeypatch
):
    from chinese_frontier import runtime

    controller, current, scheduled = refill_controller
    controller.state.cycle_limit = controller.state.cycle_batches = 1
    monkeypatch.setattr(runtime.time, "time", lambda: 100)
    controller.scanned(current, {}, {"refill_retry_seconds": 60}, 0)
    assert not scheduled and controller.state.refill_at == 160
    monkeypatch.setattr(runtime.time, "time", lambda: 160)
    controller.scanned(current, {}, {}, 0)
    assert len(scheduled) == 1 and controller.state.batches == 9


def test_old_chinese_state_can_refill_without_losing_deferred_targets(tmp_path):
    path = tmp_path / "state.json"
    old = {
        "version": 1,
        "generation_policy": GENERATION_POLICY,
        "day": 1,
        "batches": 8,
        "deferred": {
            "key": {"reason": "ambiguous", "vocabulary": "v1", "retry_after": 9999}
        },
    }
    path.write_text(json.dumps(old))
    state = State.load(path)
    assert state.can_refill(2, 0, {"max_batches_per_day": 8}, 100)
    assert state.batches == 8 and state.attempted == state.approved == 0
    assert state.deferred == old["deferred"]


def test_chinese_refill_stages_character_siblings_to_fill_exactly_ten_cards(
    collection, lexicon
):
    settings = {"enabled": True, "deck_id": 1}
    collection.set_config(SETTINGS_KEY, settings)
    deck = collection.decks.get(1)
    deck["newLimit"] = 10
    collection.decks.update_dict(deck)
    for word in ("有", "一", "页"):
        add_word(collection, word, established=True)
    for _ in range(8):
        add_word(collection, "保留")
    add_word(collection, "出租车", suspended=True)
    target = next(
        s for s in snapshot(collection, settings, {}).sources if s.word == "出租车"
    )
    c = Candidate(
        "出租车有一页。", "The taxi has one page.", "chū zū chē", "", "characters"
    )
    result = admit(collection, settings, {}, [audited_draft(target, c)], lexicon)
    assert len(result.admitted) == 1 and len(result.remaining) == 1
    assert len(collection.get_note(result.admitted[0]).cards()) == 2
    assert result.added_cards == 2
    assert collection.db.scalar("select count(*) from cards where queue=0") == 10
    assert snapshot(collection, settings, {}).slots(1) == 0


def test_chinese_buffer_prepares_at_zero_capacity_and_stops_when_full(
    refill_controller,
):
    controller, current, scheduled = refill_controller
    current.budgets[3] = [DeckBudget(3, 10, 0, 10, 150, 0, 10)]
    controller.scanned(current, {}, {"buffer_notes": 1}, 0)
    assert len(scheduled) == 1
    assert current.slots(3) == 0
    controller.state.drafts = [audited_draft(source(), candidate())]
    scheduled.clear()
    controller.scanned(current, {}, {"buffer_notes": 1}, 0)
    assert not scheduled and not controller.busy


def test_chinese_writer_receives_proven_usage_and_lessons_with_bounded_inventory(
    tmp_path, lexicon, monkeypatch
):
    from chinese_frontier import generation

    prompts = []
    events = []
    monkeypatch.setattr(generation.Lexicon, "load", lambda _folder: lexicon)
    monkeypatch.setattr(generation.CliGenerator, "__init__", lambda *args: None)
    monkeypatch.setattr(generation.CliGenerator, "check_login", lambda self: None)

    def request(self, prompt, schema):
        prompts.append(json.loads(prompt.split("Data:\n", 1)[1]))
        return {"proposals": []}

    monkeypatch.setattr(generation.CliGenerator, "request", request)
    known = {chr(0x5000 + i): "familiar" for i in range(1000)} | {"书": "book"}
    result = generate(
        [source()],
        known,
        {},
        tmp_path,
        Event(),
        lessons={source().key: {"reason": "Use a page count."}},
        contexts={
            "书": [{"card": 99, "context": "这本书有五百页。A book with pages."}]
        },
        progress=events.append,
    )
    assert not result.drafts
    assert max(len(p["known_words"]) for p in prompts) <= 160
    assert all(p["context_evidence"]["书"][0]["card"] == 99 for p in prompts)
    assert all(
        p["previous_lessons"][source().key]["reason"] == "Use a page count."
        for p in prompts
    )
    assert len(events) == 2 * len(prompts)
    assert all(
        e["prompt_chars"] > 0 and e["elapsed_seconds"] >= 0
        for e in events
        if e["finished"]
    )


def test_chinese_approved_checkpoint_survives_later_model_failure(
    tmp_path, lexicon, monkeypatch
):
    from chinese_frontier import generation

    monkeypatch.setattr(generation.Lexicon, "load", lambda _folder: lexicon)
    monkeypatch.setattr(generation.CliGenerator, "__init__", lambda *args: None)
    monkeypatch.setattr(generation.CliGenerator, "check_login", lambda self: None)
    # A multi-character word has a character fallback attempt left to fail.
    second = replace(source(), card_id=2, note_id=2, word="学生")
    responses = iter(
        [
            {"proposals": [{"card_id": 1, "candidates": [candidate().__dict__]}]},
            {
                "reviews": [
                    audit_report(source(), candidate())
                    | {"card_id": 1, "sentence": candidate().sentence}
                ]
            },
        ]
    )

    def request(self, prompt, schema):
        try:
            return next(responses)
        except StopIteration:
            raise TimeoutError("later request timed out")

    monkeypatch.setattr(generation.CliGenerator, "request", request)
    saved = []
    with pytest.raises(TimeoutError):
        generate(
            [source(), second],
            {w: "familiar" for w in ("这", "本", "书", "有", "一", "页")},
            {},
            tmp_path,
            Event(),
            on_approved=saved.extend,
        )
    assert len(saved) == 1 and saved[0].source.key == source().key
    generation.validate_audit(saved[0])


def test_chinese_usage_evidence_never_promotes_suspended_or_visible_words(collection):
    add_word(collection, "书", established=True)
    add_word(collection, "千", suspended=True)
    current = snapshot(collection, {"enabled": True, "deck_id": 1}, {})
    assert current.contexts["书"][0]["card"] in current.evidence["书"]
    assert "千" not in current.contexts and "千" not in current.known


def test_chinese_rejection_lessons_and_metrics_survive_restart(tmp_path):
    from chinese_frontier.preparation import previous_lessons

    state = State(day=1)
    report = {
        "reason": "ambiguous",
        "alternatives": [{"word": "x", "reason": "y" * 2000}] * 8,
    }
    from chinese_frontier.preparation import pack_lesson

    state.defer("target", pack_lesson(report), "v1")
    state.model_calls = [{"prompt_chars": 1234, "elapsed_seconds": 2}]
    state.save(tmp_path / "state.json")
    restored = State.load(tmp_path / "state.json")
    assert previous_lessons(restored.deferred, ["target"])["target"]["alternatives"]
    assert restored.model_calls == state.model_calls


def test_chinese_timeout_yield_includes_checkpoints_but_not_old_buffer(
    refill_controller,
):
    from concurrent.futures import Future

    controller, current, scheduled = refill_controller
    old = audited_draft(source(), candidate())
    new = replace(old, source=replace(source(), card_id=2, word="千"))
    controller.state.drafts = [old, new]
    controller.failed = lambda *args: None
    future = Future()
    future.set_exception(TimeoutError("later request"))
    controller.generated(
        future, {}, {}, "v1", 0, attempted=4, retained_keys={old.source.key}
    )
    assert controller.state.attempted == 4 and controller.state.approved == 1
    assert controller.state.drafts == [old, new]


def test_chinese_empty_proposal_reason_guides_next_retrieval_and_is_persisted(
    tmp_path, lexicon, monkeypatch
):
    from chinese_frontier import generation

    payloads = []
    monkeypatch.setattr(generation.Lexicon, "load", lambda _folder: lexicon)
    monkeypatch.setattr(generation.CliGenerator, "__init__", lambda *args: None)
    monkeypatch.setattr(generation.CliGenerator, "check_login", lambda self: None)

    def request(self, prompt, schema):
        payloads.append(json.loads(prompt.split("Data:\n", 1)[1]))
        return {
            "proposals": [
                {"card_id": 1, "candidates": [], "reason": "Need payment context."}
            ]
        }

    monkeypatch.setattr(generation.CliGenerator, "request", request)
    known = {chr(0x3400 + i): "ordinary" for i in range(400)} | {"付款": "payment"}
    # A single-character word has no second attempt to guide.
    target = source("学生")
    result = generate([target], known, {}, tmp_path, Event())
    assert "付款" not in {w["word"] for w in payloads[0]["known_words"]}
    assert "付款" in {w["word"] for w in payloads[1]["known_words"]}
    assert payloads[1]["feedback"][target.key] == "Need payment context."
    assert json.loads(result.deferred[target.key])["reason"] == "Need payment context."


METADATA = {"Source", "Dependencies", "Disambiguation"}


def field_flags(model):
    return {f["name"]: (f["excludeFromSearch"], f["collapsed"]) for f in model["flds"]}


def test_new_model_hides_metadata_fields(collection: Collection) -> None:
    flags = field_flags(ensure_model(collection))
    assert flags == {name: (name in METADATA, name in METADATA) for name in flags}


def test_existing_model_gets_metadata_flags_once(collection: Collection) -> None:
    model = ensure_model(collection)
    for f in model["flds"]:
        f["excludeFromSearch"] = f["collapsed"] = False
    collection.models.update_dict(model)
    upgraded = ensure_model(collection)
    flags = field_flags(upgraded)
    assert flags == {name: (name in METADATA, name in METADATA) for name in flags}
    stored = collection.models.get(upgraded["id"])
    assert field_flags(stored) == flags
    before = (stored["mod"], stored["flds"])
    again = ensure_model(collection)
    assert (again["mod"], again["flds"]) == before
