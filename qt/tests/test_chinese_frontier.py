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
    ADOPTED_TAG,
    FIXES_KEY,
    MAX_REPAIRS,
    PENDING_TAG,
    RESTORED_TAG,
    SETTINGS_KEY,
    admit,
    cancel_fix,
    ensure_model,
    fix_target,
    initial_scope_settings,
    needs_audit,
    pending_fixes,
    record_feedback,
    report_confusion,
    request_fix,
    request_repair,
    restore_original,
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
    CallTimeout,
    CliGenerator,
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
        establish(col, card)
    if suspended:
        col.sched.suspend_cards([card.id])
    return note, card


def establish(col: Collection, card) -> None:
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


def add_cloze(
    col: Collection,
    text: str,
    extra: str = "",
    *,
    deck: int = 1,
    established: tuple[int, ...] = (),
):
    """A learner's own cloze note; `established` lists cloze numbers with history."""
    note = col.new_note(col.models.by_name("Cloze"))
    note["Text"] = text
    note["Back Extra"] = extra
    col.add_note(note, deck)
    cards = sorted(note.cards(), key=lambda card: card.ord)
    for card in cards:
        if card.ord + 1 in established:
            establish(col, card)
    return note, cards


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


def test_lookahead_tops_up_future_days_of_new_cards() -> None:
    # 10/day, two days ahead: 30 at the start of the day.
    assert admission_slots([DeckBudget(1, 10, 0, 0, 100, 0, 0)], lookahead_days=2) == 30
    # Studying today consumes today's share, not the future days'...
    assert admission_slots([DeckBudget(1, 10, 4, 26, 100, 0, 0)], lookahead_days=2) == 0
    # ...so the next day tops up exactly what was seen.
    assert admission_slots([DeckBudget(1, 10, 0, 26, 100, 0, 0)], lookahead_days=2) == 4
    # Exhausting today's limit still leaves the future days to prepare.
    assert (
        admission_slots([DeckBudget(1, 10, 10, 5, 100, 0, 0)], lookahead_days=2) == 15
    )


def test_lookahead_does_not_count_future_cards_against_todays_reviews() -> None:
    # Today's review room admits 3; future days add their full new limit.
    budget = DeckBudget(1, 10, 0, 0, 10, 2, 5)
    assert admission_slots([budget]) == 3
    assert admission_slots([budget], lookahead_days=1) == 13


def test_lookahead_uses_the_regular_limit_not_a_today_only_override() -> None:
    cram = DeckBudget(1, 100, 0, 0, 1000, 0, 0, daily_new_limit=10)
    assert admission_slots([cram], lookahead_days=2) == 120
    day_off = DeckBudget(1, 0, 0, 0, 1000, 0, 0, daily_new_limit=10)
    assert admission_slots([day_off], lookahead_days=2) == 20


def test_lookahead_snapshot_ignores_today_only_override(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, _draft = setup_sentence(collection)
    deck = collection.decks.get(1)
    deck["newLimit"] = 1
    deck["newLimitToday"] = {"limit": 50, "today": collection.sched.today}
    collection.decks.update_dict(deck)
    add_word(collection, "学生")
    assert snapshot(collection, settings, {"lookahead_days": 2}).slots(1) == 51


def test_lookahead_snapshot_reads_config(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, _draft = setup_sentence(collection)
    deck = collection.decks.get(1)
    deck["newLimit"] = 1
    collection.decks.update_dict(deck)
    add_word(collection, "学生")
    assert snapshot(collection, settings, {}).slots(1) == 0
    assert snapshot(collection, settings, {"lookahead_days": 2}).slots(1) == 2


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


def test_slow_starting_cli_still_receives_a_prompt_larger_than_a_pipe(
    tmp_path: Path,
) -> None:
    # A reader that starts after the first poll must still get the whole
    # prompt and EOF; macOS pipes hold only 16 KB.
    cli = tmp_path / "claude"
    cli.write_text(
        "#!/bin/sh\n"
        "sleep 1\n"
        'size=$(wc -c | tr -d " ")\n'
        'printf \'{"is_error":false,"structured_output":{"size":%s}}\' "$size"\n'
    )
    cli.chmod(0o755)
    generator = CliGenerator(
        {"provider": "claude", "claude_path": str(cli), "timeout_seconds": 10},
        Event(),
    )
    assert generator.request("x" * 100_000, {}) == {"size": 100_000}


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


def test_whole_word_fit_is_computed_not_trusted_from_the_reviewer() -> None:
    # A whole-word blank hides every character, so any word fits its shape.
    c, target = candidate(), source()
    report = audit_report(target, c)
    report["alternatives"][0]["fits_cloze"] = False
    assert validate_review(report, c, target)
    report["alternatives"][0]["fits_context"] = True
    assert not validate_review(report, c, target)
    assert report["alternatives"][0]["fits_cloze"] is True


def test_character_fit_is_computed_from_the_visible_characters() -> None:
    c = replace(candidate("学生有一页。"), mode="characters")
    target = source("学生")
    report = audit_report(target, c)
    for other in report["alternatives"]:
        other.update(fits_context=True, fits_cloze=True)
    # 千 keeps neither visible sibling, so it cannot fill either blank.
    assert validate_review(report, c, target)


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
    "old_label,label",
    [
        ("Disambiguation · contrast pair", "Disambiguation"),
        ("Disambiguation · character recall", ""),
    ],
)
def test_existing_disambiguation_labels_are_shortened(
    collection: Collection, old_label: str, label: str
) -> None:
    note = collection.new_note(ensure_model(collection))
    note["Text"] = "{{c1::推}}辞。"
    note["CardKind"] = old_label
    note.tags = ["chinese-frontier::generated"]
    collection.add_note(note, 1)
    cid = note.cards()[0].id
    upgrade_presentation(collection)
    updated = collection.get_note(note.id)
    assert updated["CardKind"] == label
    assert updated.cards()[0].id == cid
    assert old_label not in updated.cards()[0].question()
    assert old_label not in updated.cards()[0].answer()


def test_character_cue_cards_are_not_labelled_disambiguation(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, target, c = student_target(collection)
    draft = audited_draft(target, replace(c, mode="characters"))
    note = collection.get_note(
        admit(collection, settings, {}, [draft], lexicon).admitted[0]
    )
    assert note["CardKind"] == "" and "disambiguation" not in note.tags
    assert all("Disambiguation" not in card.question() for card in note.cards())


def test_upgrade_drops_the_label_from_existing_character_cue_cards(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, target, c = student_target(collection)
    draft = audited_draft(target, replace(c, mode="characters"))
    note = collection.get_note(
        admit(collection, settings, {}, [draft], lexicon).admitted[0]
    )
    note["CardKind"] = "Disambiguation"
    note.tags.append("disambiguation")
    collection.update_note(note)
    cards = [(card.id, card.reps, card.ivl) for card in note.cards()]
    reported, _ = contrast_note(collection, {"disambiguation": {"with": "千"}})
    upgrade_presentation(collection)
    after = collection.get_note(note.id)
    assert after["CardKind"] == "" and "disambiguation" not in after.tags
    assert [(card.id, card.reps, card.ivl) for card in after.cards()] == cards
    assert not needs_audit(dict(after.items()))
    assert collection.get_note(reported.id)["CardKind"] == "Disambiguation"
    assert not upgrade_presentation(collection).note


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
    with pytest.raises(CallTimeout) as error:
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
    assert error.value.keys == (second.key,)


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


def test_chinese_timeout_defers_the_targets_of_the_call(refill_controller):
    from concurrent.futures import Future

    controller, current, scheduled = refill_controller
    approved = audited_draft(source(), candidate())
    controller.state.drafts = [approved]
    controller.state.defer("lesson", "No blade word is established", "v0")
    controller.failed = lambda *args: None
    future = Future()
    future.set_exception(
        CallTimeout("timed out", [approved.source.key, "fresh", "lesson"])
    )
    controller.generated(future, {}, {}, "v1", 0, attempted=3)
    assert approved.source.key not in controller.state.deferred
    assert not controller.state.can_try("fresh", "v1")
    assert not controller.state.can_try("lesson", "v1")
    assert "No blade word" in controller.state.deferred["lesson"]["reason"]


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


def stored_source(note) -> dict:
    import html as html_module

    return json.loads(html_module.unescape(note["Source"]))


def test_feedback_is_recorded_and_survives_the_repair(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, initial = setup_sentence(collection)
    note_id = admit(collection, settings, {}, [initial], lexicon).admitted[0]
    record_feedback(
        collection,
        note_id,
        reasons=("unnatural",),
        note="sounds stiff",
        avoid_words=("一",),
    )
    tags = collection.get_note(note_id).tags
    assert "chinese-frontier::repair" in tags
    assert "chinese-frontier::feedback::unnatural" in tags
    target = snapshot(collection, settings, {}).sources[0]
    assert target.force_repair and target.avoid_words == ("一",)
    assert target.rejected_sentences == ("这本书有一百页。",)
    assert "sounds stiff" in target.learner_feedback[0]
    assert "这本书有一百页。" in target.learner_feedback[0]
    assert "learner_feedback" in target.context()
    result = admit(
        collection,
        settings,
        {},
        [audited_draft(target, candidate("这本书有百页。"))],
        lexicon,
    )
    assert result.updated == [note_id]
    after = collection.get_note(note_id)
    assert "chinese-frontier::repair" not in after.tags
    assert "chinese-frontier::feedback::unnatural" in after.tags
    assert stored_source(after)["feedback"][0]["note"] == "sounds stiff"
    # Later audits still see the complaint, so the problem cannot come back.
    later = snapshot(collection, settings, {}).sources
    assert all(s.word != "百" or s.avoid_words == ("一",) for s in later)


def test_blank_feedback_is_a_plain_reroll(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, initial = setup_sentence(collection)
    note_id = admit(collection, settings, {}, [initial], lexicon).admitted[0]
    record_feedback(collection, note_id)
    target = snapshot(collection, settings, {}).sources[0]
    assert target.force_repair and target.avoid_words == ()
    assert target.rejected_sentences == ("这本书有一百页。",)


def test_feedback_validates_its_input(collection: Collection, lexicon: Lexicon) -> None:
    settings, _note, _card, initial = setup_sentence(collection)
    note_id = admit(collection, settings, {}, [initial], lexicon).admitted[0]
    with pytest.raises(ValueError):
        record_feedback(collection, note_id, reasons=("bogus",))
    with pytest.raises(ValueError):
        record_feedback(collection, note_id, avoid_words=("page",))
    with pytest.raises(ValueError):
        record_feedback(collection, note_id, avoid_words=("百",))


def test_answered_word_in_feedback_becomes_a_reported_confusion(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, initial = setup_sentence(collection)
    note_id = admit(collection, settings, {}, [initial], lexicon).admitted[0]
    record_feedback(collection, note_id, answered="千")
    target = snapshot(collection, settings, {}).sources[0]
    assert target.reported_confusions == ("千",) and target.force_repair


def test_draft_written_before_feedback_is_rejected(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, initial = setup_sentence(collection)
    note_id = admit(collection, settings, {}, [initial], lexicon).admitted[0]
    record_feedback(collection, note_id)
    stale = audited_draft(
        snapshot(collection, settings, {}).sources[0], candidate("这本书有百页。")
    )
    record_feedback(collection, note_id, note="wrong sense")
    result = admit(collection, settings, {}, [stale], lexicon)
    assert result.updated == [] and result.rejected


def test_repeated_repairs_hand_the_note_to_the_learner(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, initial = setup_sentence(collection)
    note_id = admit(collection, settings, {}, [initial], lexicon).admitted[0]
    for _ in range(MAX_REPAIRS):
        assert not record_feedback(collection, note_id, note="again").handed_over
    assert record_feedback(collection, note_id, note="again").handed_over
    tags = collection.get_note(note_id).tags
    assert "chinese-frontier::manual" in tags
    assert "chinese-frontier::repair" not in tags
    assert snapshot(collection, settings, {}).repairs == {}


def test_avoided_words_and_rejected_sentences_fail_validation(
    lexicon: Lexicon,
) -> None:
    known = {"这", "本", "书", "有", "一", "页"}
    with pytest.raises(ValidationError, match="一"):
        validate_candidate(
            candidate(), replace(source(), avoid_words=("一",)), known, lexicon
        )
    with pytest.raises(ValidationError, match="rejected"):
        validate_candidate(
            candidate(),
            replace(source(), rejected_sentences=("这本书有一百页。",)),
            known,
            lexicon,
        )


def test_learner_feedback_reaches_writer_and_reviewer(
    tmp_path: Path, lexicon: Lexicon, monkeypatch
) -> None:
    target = replace(
        source(),
        force_repair=True,
        learner_feedback=("Problems: too stiff",),
        existing_cards=1,
        existing_candidate=asdict(candidate()),
    )
    better = candidate("这本书有百页。")
    prompts = fake_generator(
        monkeypatch,
        lexicon,
        [
            {"proposals": [{"card_id": 1, "candidates": [asdict(better)]}]},
            {"reviews": [audit_report(target, better)]},
        ],
    )
    result = generate([target], KNOWN_PAGE_WORDS, {}, tmp_path, Event())
    assert result.drafts[0].candidate == better
    assert all("too stiff" in prompt for prompt in prompts)
    assert "learner_feedback" in prompts[1].split("Data:")[0]


def test_draft_round_trip_keeps_feedback() -> None:
    target = replace(
        source(),
        learner_feedback=("x",),
        avoid_words=("一",),
        rejected_sentences=("s",),
    )
    draft = Draft(target, candidate())
    again = Draft.from_dict(json.loads(json.dumps(draft.as_dict())))
    assert again.source == target


def test_answered_word_alone_keeps_the_sentence_for_the_contrast(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, initial = setup_sentence(collection)
    note_id = admit(collection, settings, {}, [initial], lexicon).admitted[0]
    record_feedback(collection, note_id, answered="千")
    target = snapshot(collection, settings, {}).sources[0]
    assert target.rejected_sentences == ()
    assert "Learner answered: 千" in target.learner_feedback[0]


def test_contrast_note_rejects_its_primary_example(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, initial = setup_sentence(collection)
    note_id = admit(collection, settings, {}, [initial], lexicon).admitted[0]
    record_feedback(collection, note_id, answered="千")
    target = snapshot(collection, settings, {}).sources[0]
    admit(
        collection, settings, {}, [audited_draft(target, thousand_contrast())], lexicon
    )
    record_feedback(collection, note_id)
    again = snapshot(collection, settings, {}).sources[0]
    assert again.rejected_sentences == (thousand_contrast().sentence,)


def test_avoid_words_cannot_contain_the_answer(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, initial = setup_sentence(collection)
    note_id = admit(collection, settings, {}, [initial], lexicon).admitted[0]
    with pytest.raises(ValueError):
        record_feedback(collection, note_id, avoid_words=("一百",))


def test_manual_note_still_counts_its_word_as_covered(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, initial = setup_sentence(collection)
    note_id = admit(collection, settings, {}, [initial], lexicon).admitted[0]
    for _ in range(MAX_REPAIRS + 1):
        record_feedback(collection, note_id)
    current = snapshot(collection, settings, {})
    assert "百" in current.covered_words and current.repairs == {}


def test_retry_now_drops_stale_drafts_and_lifts_backoff() -> None:
    state = State()
    draft = Draft(source(), candidate())
    state.drafts = [draft]
    state.defer(source().key, "earlier failure", "v")
    assert not state.can_try(source().key, "v")
    state.retry_now(source().key)
    assert state.drafts == [] and state.can_try(source().key, "v")
    assert "earlier failure" in str(state.deferred[source().key]["reason"])


# Fixing the learner's own cards: a new linked note inherits the card's schedule.

PAGE_CLOZE = (
    "<div>这本书有一{{c1::百}}{{c2::页}}</div><div>This book has 100 pages.</div>"
)
SCHEDULING = ("type", "queue", "due", "ivl", "factor", "reps", "lapses", "left")


def scheduling(card) -> tuple:
    return (*(getattr(card, name) for name in SCHEDULING), card.memory_state)


def setup_fix(col: Collection):
    settings = {"enabled": True, "deck_id": 1}
    col.set_config(SETTINGS_KEY, settings)
    for word in ("这", "本", "书", "有", "一", "页"):
        add_word(col, word, established=True)
    note, cards = add_cloze(col, PAGE_CLOZE, "bǎi: hundred", established=(1, 2))
    card = cards[0]
    card.ivl, card.factor, card.lapses = 30, 2300, 1
    col.update_card(card)
    return settings, note, col.get_card(card.id), cards[1]


def fix(col: Collection, card_id: int, **values):
    values.setdefault("word", "百")
    values.setdefault("definition", "hundred")
    return request_fix(col, card_id, config={}, **values)


def cloze_fields(text: str, extra: str = "") -> dict[str, str]:
    return {"Text": text, "Back Extra": extra}


def test_fix_target_reads_the_reviewed_ordinal(collection: Collection) -> None:
    model = collection.models.by_name("Cloze")
    target = fix_target(cloze_fields(PAGE_CLOZE, "bǎi: hundred"), model, 0, "D", {})
    assert target.words == ("百",) and target.cloze
    assert target.sentence == "这本书有一百页"
    assert "This book has 100 pages." in target.meaning
    assert "bǎi: hundred" in target.meaning
    assert fix_target(cloze_fields(PAGE_CLOZE), model, 1, "D", {}).words == ("页",)


def test_fix_target_defaults_a_lone_character_to_its_lexicon_word(
    collection: Collection,
) -> None:
    model = collection.models.by_name("Cloze")
    text = "{{c5::弄}}{{c6::虚}} {{c2::作假}}, {{c3::骗取}}{{c4::荣誉}}"
    words = Lexicon({"弄", "虚", "弄虚作假", "作假", "骗取", "荣誉"})
    target = fix_target(cloze_fields(text), model, 4, "D", {}, words)
    assert target.words == ("弄虚作假", "弄")
    # Without a cached dictionary the clozed character is still offered.
    assert fix_target(cloze_fields(text), model, 4, "D", {}).words == ("弄",)
    # A clozed word is never widened.
    assert fix_target(cloze_fields(text), model, 1, "D", {}, words).words == ("作假",)


def test_fix_target_offers_every_word_on_one_ordinal(collection: Collection) -> None:
    model = collection.models.by_name("Cloze")
    text = "这本书写得很{{c1::生}}动。我{{c1::激}}动"
    words = Lexicon({"这", "本", "书", "写", "得", "很", "生动", "激动", "我"})
    target = fix_target(cloze_fields(text), model, 0, "D", {}, words)
    assert target.words == ("生动", "激动", "生", "激")


def test_fix_target_ignores_non_chinese_clozes(collection: Collection) -> None:
    model = collection.models.by_name("Cloze")
    assert fix_target(cloze_fields("{{c1::hello}} 你好"), model, 0, "D", {}) is None


def test_fix_target_on_basic_cards_needs_the_front_tested(
    collection: Collection,
) -> None:
    basic = collection.models.by_name("Basic")
    fields = {"Front": "百 3", "Back": "hundred"}
    target = fix_target(fields, basic, 0, "HSK 3", {})
    assert target.words == ("百",) and target.meaning == "hundred"
    assert not target.cloze
    reversed_model = collection.models.by_name("Basic (and reversed card)")
    assert fix_target(fields, reversed_model, 1, "HSK 3", {}) is None


def test_request_fix_suspends_only_the_reviewed_card(collection: Collection) -> None:
    _settings, note, card, sibling = setup_fix(collection)
    fields = list(note.fields)
    result = fix(
        collection, card.id, reasons=("unnatural",), avoid_words=("一",), answered="千"
    )
    assert collection.get_card(card.id).queue == -1
    assert collection.get_card(sibling.id).queue == 2
    after = collection.get_note(note.id)
    assert after.fields == fields and PENDING_TAG in after.tags
    record = collection.get_config(FIXES_KEY)[str(card.id)]
    assert record["key"] == result.key
    assert record["word"] == "百" and record["sentence"] == "这本书有一百页"
    assert record["feedback"][0]["reasons"] == ["unnatural"]
    assert record["feedback"][0]["avoid_words"] == ["一"]
    assert record["disambiguation"]["with"] == "千"
    collection.undo()
    assert collection.get_card(card.id).queue == 2
    assert PENDING_TAG not in collection.get_note(note.id).tags
    assert collection.get_config(FIXES_KEY, {}) == {}


def test_second_fix_request_appends_feedback(collection: Collection) -> None:
    _settings, _note, card, _sibling = setup_fix(collection)
    fix(collection, card.id, reasons=("unnatural",))
    fix(collection, card.id, note="still stiff")
    entries = collection.get_config(FIXES_KEY)[str(card.id)]["feedback"]
    assert [e["note"] for e in entries] == ["", "still stiff"]


def test_fix_request_validates_its_input(collection: Collection) -> None:
    _settings, _note, card, _sibling = setup_fix(collection)
    for values in (
        {"reasons": ("bogus",)},
        {"avoid_words": ("百",)},
        {"avoid_words": ("page",)},
        {"answered": "百"},
        {"word": "千"},
        {"definition": " "},
    ):
        with pytest.raises(ValueError):
            fix(collection, card.id, **values)
    elsewhere = collection.decks.id("Elsewhere")
    _other, cards = add_cloze(collection, PAGE_CLOZE, deck=elsewhere)
    with pytest.raises(ValueError):
        fix(collection, cards[0].id)
    assert collection.get_config(FIXES_KEY, {}) == {}


def test_generated_notes_keep_their_own_feedback_path(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, initial = setup_sentence(collection)
    note_id = admit(collection, settings, {}, [initial], lexicon).admitted[0]
    with pytest.raises(ValueError):
        fix(collection, collection.get_note(note_id).cards()[0].id)


def test_cancel_fix_unsuspends_untags_and_forgets(collection: Collection) -> None:
    _settings, note, card, sibling = setup_fix(collection)
    fix(collection, card.id)
    fix(collection, sibling.id)
    cancel_fix(collection, card.id)
    assert collection.get_card(card.id).queue == 2
    # The sibling's fix still waits, so the note stays findable.
    assert PENDING_TAG in collection.get_note(note.id).tags
    cancel_fix(collection, sibling.id)
    assert PENDING_TAG not in collection.get_note(note.id).tags
    assert collection.get_config(FIXES_KEY) == {}
    collection.undo()
    assert collection.get_card(sibling.id).queue == -1
    assert PENDING_TAG in collection.get_note(note.id).tags
    assert list(collection.get_config(FIXES_KEY)) == [str(sibling.id)]


def test_pending_fix_is_a_forced_source_ahead_of_others(
    collection: Collection,
) -> None:
    settings, _note, card, _sibling = setup_fix(collection)
    add_word(collection, "学生", suspended=True)
    fix(collection, card.id, reasons=("unnatural",), answered="千")
    current = snapshot(collection, settings, {})
    target = current.sources[0]
    assert target.card_id == card.id and target.key in current.fixes
    assert target.force_repair and target.existing_cards == 1
    assert target.avoid_sentence == "这本书有一百页"
    assert target.rejected_sentences == ("这本书有一百页",)
    assert target.reported_confusions == ("千",)
    assert "Sounds unnatural" in target.learner_feedback[0]
    # Familiar, yet the learner asked for it; the suspended card still counts.
    assert "百" in current.known


def test_pending_fix_ignores_its_word_being_covered(collection: Collection) -> None:
    settings, _note, card, _sibling = setup_fix(collection)
    add_cloze(collection, "这本书有一{{c1::百}}页", established=(1,))
    fix(collection, card.id)
    current = snapshot(collection, settings, {})
    assert "百" in current.covered_words
    assert [s.card_id for s in current.sources] == [card.id]


def test_stale_fix_is_reported_not_deleted(collection: Collection) -> None:
    settings, note, card, sibling = setup_fix(collection)
    fix(collection, card.id)
    fix(collection, sibling.id, word="页", definition="page")
    note["Back Extra"] = "edited by the learner"
    collection.update_note(note)
    current = snapshot(collection, settings, {})
    assert all(s.card_id not in (card.id, sibling.id) for s in current.sources)
    assert current.skipped["fix stalled: original note was edited"] == 2
    assert collection.get_card(card.id).queue == -1
    assert len(collection.get_config(FIXES_KEY)) == 2
    problems = {item.card_id: item.problem for item in pending_fixes(collection)}
    assert problems[card.id] == "original note was edited"


def admit_fix(col: Collection, settings, lexicon: Lexicon, sentence="这本书有百页。"):
    current = snapshot(col, settings, {})
    target = next(s for s in current.sources if s.key in current.fixes)
    draft = audited_draft(target, candidate(sentence))
    return target, admit(col, settings, {}, [draft], lexicon)


def zero_new_cards(col: Collection) -> None:
    deck = col.decks.get(1)
    deck["newLimit"] = 0
    col.decks.update_dict(deck)


def test_fix_admission_transplants_the_schedule_without_a_new_slot(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, note, card, sibling = setup_fix(collection)
    before = scheduling(card)
    fix(collection, card.id, reasons=("unnatural",))
    zero_new_cards(collection)
    target, result = admit_fix(collection, settings, lexicon)
    assert len(result.admitted) == 1 and result.added_cards == 0
    adopted = collection.get_note(result.admitted[0])
    data = stored_source(adopted)
    assert data["adopted"]["card_id"] == card.id
    assert data["adopted"]["note_id"] == note.id
    assert data["feedback"][0]["reasons"] == ["unnatural"]
    assert data["key"] == target.key
    assert {
        "chinese-frontier::generated",
        ADOPTED_TAG,
        "chinese-frontier::key::" + target.key,
        f"chinese-frontier::source::{card.id}",
        "chinese-frontier::feedback::unnatural",
    } <= set(adopted.tags)
    new_card = adopted.cards()[0]
    assert new_card.did == card.did
    assert scheduling(new_card) == before
    assert collection.get_card(card.id).queue == -1
    assert collection.get_card(sibling.id).queue == 2
    assert PENDING_TAG not in collection.get_note(note.id).tags
    assert collection.get_config(FIXES_KEY) == {}
    collection.undo()
    assert collection.find_notes("tag:" + ADOPTED_TAG) == []
    assert collection.get_card(card.id).queue == -1
    assert PENDING_TAG in collection.get_note(note.id).tags
    assert list(collection.get_config(FIXES_KEY)) == [str(card.id)]


def test_fix_is_placed_in_the_original_subdeck(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, _sibling = setup_fix(collection)
    child = collection.decks.id("Default::Child")
    _other, cards = add_cloze(collection, PAGE_CLOZE, deck=child, established=(1,))
    fix(collection, cards[0].id)
    _target, result = admit_fix(collection, settings, lexicon)
    assert collection.get_note(result.admitted[0]).cards()[0].did == child


def test_character_fix_transplants_the_first_card_and_budgets_siblings(
    collection: Collection,
) -> None:
    settings = {"enabled": True, "deck_id": 1}
    collection.set_config(SETTINGS_KEY, settings)
    for word in ("学", "生", "有", "一", "页"):
        add_word(collection, word, established=True)
    _note, cards = add_cloze(
        collection, "{{c1::学生}}有一页", "student", established=(1,)
    )
    before = scheduling(cards[0])
    request_fix(collection, cards[0].id, config={}, word="学生", definition="student")
    zero_new_cards(collection)
    words = Lexicon({"学", "生", "学生", "有", "一", "页"})
    current = snapshot(collection, settings, {})
    target = current.sources[0]
    chars = replace(
        Candidate("学生有一页。", "The student has one page.", "xué shēng", ""),
        mode="characters",
        explanation="Either character identifies the word.",
    )
    result = admit(collection, settings, {}, [audited_draft(target, chars)], words)
    adopted = collection.get_note(result.admitted[0])
    assert len(adopted.cards()) == 1 and result.added_cards == 0
    assert scheduling(adopted.cards()[0]) == before
    assert len(result.remaining) == 1
    assert "{{c1::学}}" in adopted["Text"] and "{{c2::" not in adopted["Text"]


def test_draft_written_before_more_fix_feedback_is_stale(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, card, _sibling = setup_fix(collection)
    fix(collection, card.id)
    current = snapshot(collection, settings, {})
    stale = audited_draft(current.sources[0], candidate("这本书有百页。"))
    fix(collection, card.id, note="wrong sense")
    result = admit(collection, settings, {}, [stale], lexicon)
    assert result.admitted == [] and result.rejected[stale.source.key]
    assert collection.get_config(FIXES_KEY)


def test_reported_confusion_on_a_fix_needs_a_contrast(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, card, _sibling = setup_fix(collection)
    fix(collection, card.id, answered="千")
    current = snapshot(collection, settings, {})
    target = current.sources[0]
    single = admit(
        collection,
        settings,
        {},
        [audited_draft(target, candidate("这本书有百页。"))],
        lexicon,
    )
    assert single.admitted == [] and "contrast" in single.rejected[target.key]
    pair = admit(
        collection,
        settings,
        {},
        [audited_draft(target, thousand_contrast())],
        Lexicon({*lexicon.words, "千"}),
    )
    assert len(pair.admitted) == 1
    adopted = collection.get_note(pair.admitted[0])
    assert stored_source(adopted)["disambiguation"]["with"] == "千"
    assert adopted["CardKind"] == "Disambiguation"


def test_fix_source_reaches_writer_as_a_contrast_request(
    collection: Collection, tmp_path: Path, lexicon: Lexicon, monkeypatch
) -> None:
    settings, _note, card, _sibling = setup_fix(collection)
    fix(collection, card.id, answered="千", note="the number was unclear")
    target = snapshot(collection, settings, {}).sources[0]
    pair = thousand_contrast()
    prompts = fake_generator(
        monkeypatch,
        lexicon,
        [
            {"proposals": [{"card_id": card.id, "candidates": [asdict(pair)]}]},
            {"reviews": [audit_report(target, pair)]},
        ],
    )
    result = generate([target], KNOWN_PAGE_WORDS, {}, tmp_path, Event())
    assert 'mode="contrast"' in prompts[0] and "千" in prompts[0]
    assert all("the number was unclear" in prompt for prompt in prompts)
    assert result.drafts[0].candidate == pair


def swapped(collection: Collection, lexicon: Lexicon):
    settings, note, card, sibling = setup_fix(collection)
    fix(collection, card.id, reasons=("unnatural",))
    _target, result = admit_fix(collection, settings, lexicon)
    return settings, note, card, collection.get_note(result.admitted[0])


def test_adopted_note_is_repaired_from_its_adopted_metadata(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, card, adopted = swapped(collection, lexicon)
    new_card = adopted.cards()[0]
    record_feedback(collection, adopted.id, note="still odd")
    target = snapshot(collection, settings, {}).sources[0]
    assert (
        target.card_id == card.id
        and target.key in snapshot(collection, settings, {}).repairs
    )
    # Carried-over feedback counts toward the repair limit.
    assert len(target.learner_feedback) == 2
    result = admit(
        collection,
        settings,
        {},
        [audited_draft(target, candidate("这本书有一百页。"))],
        lexicon,
    )
    assert result.updated == [adopted.id]
    again = collection.get_card(new_card.id)
    assert scheduling(again) == scheduling(new_card)


def test_original_history_keeps_the_word_familiar(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, card, _sibling = setup_fix(collection)
    fix(collection, card.id)
    assert "百" in snapshot(collection, settings, {}).known
    _target, result = admit_fix(collection, settings, lexicon)
    current = snapshot(collection, settings, {})
    assert "百" in current.known
    assert "百" in current.covered_words


def test_restore_original_puts_the_learner_card_back(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, note, card, adopted = swapped(collection, lexicon)
    new_card = adopted.cards()[0]
    new_card.ivl, new_card.due = 45, collection.sched.today + 40
    collection.update_card(new_card)
    restore_original(collection, adopted.id)
    original = collection.get_card(card.id)
    assert original.queue == 2 and (original.ivl, original.due) == (
        45,
        collection.sched.today + 40,
    )
    after = collection.get_note(adopted.id)
    assert all(c.queue == -1 for c in after.cards())
    assert RESTORED_TAG in after.tags and stored_source(after)["adopted"]["restored_at"]
    current = snapshot(collection, settings, {})
    assert current.repairs == {} and all(s.card_id != card.id for s in current.sources)
    record_feedback(collection, adopted.id, note="ignored")
    assert snapshot(collection, settings, {}).repairs == {}
    with pytest.raises(ValueError):
        restore_original(collection, adopted.id)
    collection.undo()
    collection.undo()
    assert collection.get_card(card.id).queue == -1
    assert RESTORED_TAG not in collection.get_note(adopted.id).tags


def test_restored_note_does_not_cover_its_word(
    collection: Collection, lexicon: Lexicon
) -> None:
    settings, _note, _card, adopted = swapped(collection, lexicon)
    restore_original(collection, adopted.id)
    current = snapshot(collection, settings, {})
    # The restored learner card covers the word again; the adopted one does not.
    assert adopted.id not in current.repairs.values()


@pytest.fixture
def menu_controller(monkeypatch, collection):
    from types import SimpleNamespace

    from chinese_frontier import runtime

    monkeypatch.setattr(
        runtime,
        "mw",
        SimpleNamespace(
            col=collection,
            addonManager=SimpleNamespace(getConfig=lambda _module: {}),
        ),
    )
    controller = runtime.Controller.__new__(runtime.Controller)
    controller.state = State()
    controller.save_state = lambda: None
    ticks = []
    controller.tick = lambda force=False: ticks.append(force)
    return controller, ticks


class FakeMenu:
    def __init__(self) -> None:
        self.labels: list[str] = []
        self.handlers: list = []

    def addAction(self, label: str):
        from types import SimpleNamespace

        self.labels.append(label)
        return SimpleNamespace(triggered=SimpleNamespace(connect=self.handlers.append))


def menu_for(controller, card) -> list[str]:
    from types import SimpleNamespace

    menu = FakeMenu()
    controller.reviewer_menu(SimpleNamespace(card=card), menu)
    return menu.labels


def test_reviewer_menu_offers_fixes_on_learner_cards_in_the_chosen_deck(
    collection: Collection, lexicon: Lexicon, menu_controller
) -> None:
    controller, _ticks = menu_controller
    settings, _note, card, _sibling = setup_fix(collection)
    assert menu_for(controller, card) == ["Fix this sentence..."]
    _word, basic = add_word(collection, "学生")
    assert menu_for(controller, basic) == ["Make a sentence for this word..."]
    vocabulary = collection.decks.id("Vocabulary")
    collection.set_config(
        SETTINGS_KEY,
        {**settings, "vocabulary_deck_ids": [vocabulary], "source_deck_ids": []},
    )
    _other, outside = add_cloze(collection, PAGE_CLOZE, deck=vocabulary)
    assert menu_for(controller, outside[0]) == []
    _plain, english = add_cloze(collection, "{{c1::hello}} 你好")
    assert menu_for(controller, english[0]) == []


def test_reviewer_menu_keeps_generated_items_and_offers_restore_on_adopted(
    collection: Collection, lexicon: Lexicon, menu_controller
) -> None:
    controller, _ticks = menu_controller
    settings, _note, _card, adopted = swapped(collection, lexicon)
    assert menu_for(controller, adopted.cards()[0]) == [
        "Fix this sentence...",
        "Restore the original card",
    ]
    restore_original(collection, adopted.id)
    assert menu_for(controller, adopted.cards()[0]) == ["Fix this sentence..."]


def test_fix_dialog_submission_requests_and_retries_now(
    collection: Collection, menu_controller, monkeypatch
) -> None:
    from chinese_frontier import runtime

    controller, ticks = menu_controller
    _settings, _note, card, _sibling = setup_fix(collection)

    class ImmediateOp:
        def __init__(self, parent, op):
            self.result = op(collection)

        def success(self, done):
            done(self.result)
            return self

        def run_in_background(self, **_kwargs):
            pass

    monkeypatch.setattr(runtime, "CollectionOp", ImmediateOp)
    monkeypatch.setattr(runtime, "tooltip", lambda *_args, **_kwargs: None)
    retried = []
    monkeypatch.setattr(controller.state, "retry_now", retried.append)
    controller.submit_fix(card.id, word="百", definition="hundred", note="stiff")
    record = collection.get_config(FIXES_KEY)[str(card.id)]
    assert retried == [record["key"]] and ticks == [True]
