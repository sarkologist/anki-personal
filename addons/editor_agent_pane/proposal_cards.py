# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from anki.cards import Card
from anki.collection import Collection
from anki.consts import MODEL_CLOZE
from anki.decks import DEFAULT_DECK_ID, DeckId
from anki.notes import Note
from anki.template import TemplateRenderContext, TemplateRenderOutput

from .patches import CardActionPatch, EditorSnapshot, NotePatch, PatchValidationError


class ProposalCard(Card):
    """Keep Previewer's reload requests on the rendered, unsaved proposal."""

    def note(self, reload: bool = False) -> Note:
        assert self._note is not None
        return self._note

    def render_output(
        self, reload: bool = False, browser: bool = False
    ) -> TemplateRenderOutput:
        assert self._render_output is not None
        return self._render_output


def proposal_preview_cards(
    col: Collection, snapshot: EditorSnapshot, patch: NotePatch | CardActionPatch
) -> tuple[ProposalCard, ...]:
    notetype = col.models.get(snapshot.notetype_id)
    if (
        notetype is None
        or tuple(field["name"] for field in notetype["flds"]) != snapshot.field_names()
    ):
        raise PatchValidationError("The note type changed. Request a new proposal.")
    if isinstance(patch, CardActionPatch) and patch.action == "create":
        contents = [(note.fields, note.tags) for note in patch.notes]
    else:
        fields = {field.name: field.html for field in snapshot.fields}
        tags = snapshot.tags
        if isinstance(patch, NotePatch):
            fields.update(patch.field_updates)
            tags = patch.tag_patch.apply(tags)
        contents = [(fields, tags)]

    cards = []
    for fields, tags in contents:
        note = col.new_note(notetype)
        for name, value in fields.items():
            note[name] = value
        note.tags = list(tags)
        if isinstance(patch, CardActionPatch) and patch.action == "delete":
            assert patch.card_id is not None
            ords = [col.get_card(patch.card_id).ord]
        elif notetype["type"] == MODEL_CLOZE:
            ords = sorted(
                number - 1 for number in note.cloze_numbers_in_fields() if number > 0
            ) or [0]
        else:
            ords = list(range(len(notetype["tmpls"])))
        for ord in ords:
            card = ProposalCard(col)
            card.ord = ord
            card.did = DeckId(snapshot.deck_id or DEFAULT_DECK_ID)
            card._note = note
            template = dict(
                notetype["tmpls"][0 if notetype["type"] == MODEL_CLOZE else ord]
            )
            template["ord"] = ord
            card.set_render_output(
                TemplateRenderContext.from_card_layout(
                    note, card, notetype=notetype, template=template, fill_empty=False
                ).render()
            )
            cards.append(card)
    return tuple(cards)
