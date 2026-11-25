"""Which deck a record plays on, and which side of the crossfader that is.

A set plays through its decks in order. With four decks that is A, B, C, D and
round again; with two it alternates A and B. VirtualDJ puts odd decks on the
left of the crossfader and even decks on the right, so every step of either
rotation crosses the fader. The engine verifies that assignment before each
handoff; these are only the rules.
"""
TWO = (1, 2)
FOUR = (1, 2, 3, 4)


def _deck(deck):
    if type(deck) is not int or deck not in FOUR:
        raise ValueError('Deck must be 1 to 4.')
    return deck


def for_layout(count):
    """Four-deck rotation needs a four-deck layout; anything else alternates A and B."""
    return FOUR if type(count) in (int, float) and count >= 4 else TWO


def deck_for(index, decks=TWO):
    """The deck the record at this position in the set plays on."""
    return decks[index % len(decks)]


def following(deck, decks=TWO):
    return decks[(decks.index(_deck(deck)) + 1) % len(decks)]


def previous(deck, decks=TWO):
    return decks[(decks.index(_deck(deck)) - 1) % len(decks)]


def side(deck):
    return 'left' if _deck(deck) % 2 else 'right'


def crossfader_end(deck):
    """Where the crossfader rests when only this deck is heard."""
    return 0.0 if _deck(deck) % 2 else 1.0


def letter(deck):
    return 'ABCD'[_deck(deck) - 1]


def pair(outgoing, incoming, decks=TWO):
    """Whether this is one step of the rotation: the next deck, across the fader."""
    return (type(outgoing) is int and type(incoming) is int and outgoing in decks and incoming in decks
            and following(outgoing, decks) == incoming and side(outgoing) != side(incoming))


def spares(active, incoming, decks=TWO):
    """Decks outside the playing pair, the next to play first."""
    result, deck = [], incoming
    for _ in decks:
        deck = following(deck, decks)
        if deck not in (active, incoming):
            result.append(deck)
    return tuple(result)


def row(telemetry, deck):
    """This deck's reading, by number; never by its place in the list."""
    found = next((d for d in (telemetry or {}).get('decks', []) if d.get('deck') == deck), None)
    if found is None:
        raise ValueError(f'Deck {deck} has no reading.')
    return found
