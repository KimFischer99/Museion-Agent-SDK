"""Read-only host seam; no account access or model calls on import.

The host must supply only the current profile's user/assistant turns from
channels authorized for these jobs, after removing credentials/private side chats.
Enable policy.conversation_context in PasConfig to consume this excerpt.
"""

from proactive_sdk import ConversationTurn, ProactiveAgent


class ScopedConversation:
    def __init__(self, authorized_turns: tuple[ConversationTurn, ...]) -> None:
        self.turns = tuple(sorted(authorized_turns, key=lambda turn: (turn.at_ms, turn.turn_id)))

    async def recent_turns(self, since_ms: int, limit: int) -> tuple[ConversationTurn, ...]:
        return tuple(turn for turn in self.turns if turn.at_ms >= since_ms)[-limit:]


def build_agent(*, config, executor, authorized_turns: tuple[ConversationTurn, ...]) -> ProactiveAgent:
    return ProactiveAgent(state_dir=config.state_dir, profile=config.profile,
                          timezone=config.timezone, locale=config.locale, config=config,
                          executor=executor, conversation=ScopedConversation(authorized_turns))
