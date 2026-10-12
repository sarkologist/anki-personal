# Chinese cloze feedback

- Treat a learner's plausible competing answer as evidence that the card needs a clearer distinction, even if the original sentence is grammatical.
- For reported confusions, follow the deck's existing disambiguation style: contrast short examples side by side, cloze whole competing words with the same ordinal, and edit the existing note so one card and its scheduling history are preserved.
- Make the distinguishing relation visible in the Chinese context. Connector examples should distinguish contrast, cause, or an outcome holding across alternatives; an English hint alone does not establish that relation.
- Validate other context words against live-card evidence. A competing word explicitly supplied by the learner may be tested in the contrast; do not infer familiarity for additional words.

- Show the complete, unclozed English translation alongside each Chinese example on the front. Do not use declarative definition hints inside the blank: the task is contextual recall.
- Supply enough context for a learner; do not rely on prior knowledge of idiomatic preference or favor the shortest sentence. Consider plausible competing words for every target, and acknowledge genuine synonym overlap rather than calling an alternative wrong.
- If fuller context, translation, and paired examples still cannot disambiguate, character clozes are an allowed last resort. Keep each character card contextual and count every resulting card against the deck limit; never infer whole-word familiarity from one character answer.

- Automatically audit every generated blank, including every contrast example and character sibling. Store the actual checked alternatives and judgments in the hidden `Disambiguation` field; never treat model approval as stronger than its own report of a fitting alternative.
- Show an explicit Disambiguation label on both review sides for contrast pairs and character fallback. Multiple clozes alone do not identify a disambiguation card.
- Preserve existing IDs/history when repairing. Count every additional sibling against daily limits, stage across days if necessary, and establish whole-word evidence only when all siblings are learned. Record unresolved ambiguity honestly and defer rather than invent exclusive synonym rules.
