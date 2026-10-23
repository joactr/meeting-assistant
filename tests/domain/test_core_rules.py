from meeting_assistant.domain.model import (
    is_question,
    meaningful_transcript,
    new_id,
    union_duration,
)


def test_confirmation_tags_preserve_real_questions() -> None:
    assert not is_question("We ship tomorrow, right?", "en")
    assert not is_question("Lo hacemos mañana, ¿no?", "es")
    assert is_question("Is that right?", "en")
    assert is_question("¿Quién viene, vale?", "es")
    assert is_question("Can we deploy, okay?", "en")


def test_noise_rules_preserve_meaningful_short_answers() -> None:
    for answer in ("yes", "no", "yeah", "sí", "vale", "ok"):
        assert meaningful_transcript(
            answer, "en" if answer in ("yes", "no", "yeah", "ok") else "es"
        )
    assert not meaningful_transcript("...!", "en")
    assert not meaningful_transcript("um uh", "en")
    assert meaningful_transcript("um", "xx")


def test_talk_time_counts_union_not_overlap_twice() -> None:
    assert union_duration([(0, 1000), (500, 1500), (2000, 2500)]) == 2000
    assert union_duration([]) == 0


def test_ids_are_uuid_v7() -> None:
    import uuid

    assert uuid.UUID(new_id()).version == 7
