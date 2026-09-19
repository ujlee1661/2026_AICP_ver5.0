from twinmarket_kr.llm.validation import LLMValidationError


COMMUNITY_DEPTH1_READ_LIMIT = 5
COMMUNITY_DEPTH2_READ_LIMIT = 5
COMMUNITY_POST_BODY_MAX_CHARS = 500


class CommunityValidationError(LLMValidationError):
    pass


def can_author_community_post(agent: dict) -> bool:
    """Allow every D1/D2 agent to decide whether to author a post."""

    try:
        depth = int(agent.get("news_depth"))
    except (TypeError, ValueError):
        return False
    return depth in {1, 2}


def expected_selective_read_limit(depth: int) -> int:
    """Return the approved baseline body-read cap for one community depth."""

    if isinstance(depth, bool) or not isinstance(depth, int):
        raise CommunityValidationError("community depth must be an integer")
    if depth == 0:
        return 0
    if depth == 1:
        return COMMUNITY_DEPTH1_READ_LIMIT
    if depth == 2:
        return COMMUNITY_DEPTH2_READ_LIMIT
    raise CommunityValidationError("community depth must be 0, 1, or 2")


def validate_selective_read_limits(*, depth1: int, depth2: int) -> None:
    """Fail closed when runtime/spec caps drift from the approved baseline."""

    if (
        isinstance(depth1, bool)
        or isinstance(depth2, bool)
        or depth1 != COMMUNITY_DEPTH1_READ_LIMIT
        or depth2 != COMMUNITY_DEPTH2_READ_LIMIT
    ):
        raise CommunityValidationError(
            "community selective-read limits must be D1=5 and D2=5"
        )


def validate_post_body(body: object) -> str:
    """Validate a complete post body without silently truncating it."""

    if not isinstance(body, str) or not body.strip():
        raise CommunityValidationError("community post body must be non-empty text")
    if len(body) > COMMUNITY_POST_BODY_MAX_CHARS:
        raise CommunityValidationError(
            f"community post body exceeds {COMMUNITY_POST_BODY_MAX_CHARS} characters"
        )
    return body
