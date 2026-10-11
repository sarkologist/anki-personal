# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import json
import sys
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
    request_repair,
    snapshot,
)
from chinese_frontier.core import (  # noqa: E402
    Candidate,
    DeckBudget,
    Source,
    ValidationError,
    admission_slots,
    cloze_words,
    effective_limit,
    successful_days,
    validate_candidate,
)
from chinese_frontier.generation import Draft, generate  # noqa: E402
from chinese_frontier.lexicon import Lexicon  # noqa: E402
from chinese_frontier.providers import (  # noqa: E402
    cli_command,
    parse_response,
    subscription_environment,
)
from chinese_frontier.state import State  # noqa: E402

from anki.collection import Collection  # noqa: E402


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
    return settings, note, card, Draft(current.sources[0], candidate())


@pytest.fixture
def lexicon() -> Lexicon:
    return Lexicon({"这", "本", "书", "有", "一", "百", "页", "学", "生", "学生"})


def source(word: str = "百") -> Source:
    return Source(1, 2, 3, word, "bǎi - hundred", "original")


def candidate(sentence: str = "这本书有一百页。") -> Candidate:
    return Candidate(sentence, "This book has one hundred pages.", "bǎi", "hundred")


def test_whole_word_cloze_and_answer_only_translation(lexicon: Lexicon) -> None:
    result = validate_candidate(
        candidate(), source(), {"这", "本", "书", "有", "一", "页"}, lexicon
    )
    assert result.text == "这本书有一{{c1::百::hundred}}页。"
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
    assert replacement["Text"] == "这本书有一{{c1::百::hundred}}页。"
    assert "This book" not in replacement.cards()[0].question()
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
    state = State(drafts=[Draft(source(), candidate())], day=1, batches=8)
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
                    }
                ]
            },
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
    assert result.deferred[source().key] == "unnatural"


def test_failed_render_verification_rolls_back_admission(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, draft = setup_sentence(collection)
    model = ensure_model(collection)
    model["tmpls"][0]["qfmt"] += "{{Translation}}"
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
    updated = Draft(current.sources[0], candidate("这本书有百页。"))
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
    repeated = Draft(current.sources[0], draft.candidate)
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
    result = admit(collection, settings, {}, [Draft(imported, candidate())], lexicon)
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
        collection, settings, {}, [Draft(hundred, candidate("学生有一百页。"))], lexicon
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
