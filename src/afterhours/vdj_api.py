"""Bounded requests shared by the local HTTP server and future UI controls."""
from afterhours.dj_bridge import VirtualDJ, perform
from afterhours.vdj_catalog import catalog_status, catalog_search, load_selected, queue_selected
from afterhours.vdj_queue import queue_status

READ_ACTIONS = {"status", "catalog-status", "queue-status"}
PLAY_ACTIONS = {"play", "pause", "automix-start", "automix-stop", "takeover", "next"}
CATALOG_ACTIONS = {"catalog-search", "load-selected", "queue-selected"}


def request(data, port=7474, client_factory=VirtualDJ):
    if not isinstance(data, dict):
        raise ValueError("Expected an object")
    action = data.get("action")
    if not isinstance(action, str) or action not in READ_ACTIONS | PLAY_ACTIONS | CATALOG_ACTIONS:
        raise ValueError("Unknown VirtualDJ action")
    if set(data) - {"action", "execute", "deck", "query", "selection", "folder"}:
        raise ValueError("Unsupported VirtualDJ request fields")
    execute = data.get("execute", False)
    if type(execute) is not bool:
        raise ValueError("execute must be a boolean")
    deck = data.get("deck", 1)
    if type(deck) is not int or deck not in (1, 2):
        raise ValueError("deck must be 1 or 2")
    for field in ("selection", "folder"):
        value = data.get(field)
        if value is not None and (not isinstance(value, str) or len(value) != 64
                                  or any(c not in "0123456789abcdef" for c in value)):
            raise ValueError(f"{field} must be an ID from catalog-status")

    def invoke(client):
        if action == "status":
            return client.status()
        if action == "catalog-status":
            return catalog_status(client)
        if action == "queue-status":
            return queue_status(client)
        if action == "catalog-search":
            return catalog_search(client, data.get("query"), data.get("folder"), execute)
        if action == "load-selected":
            return load_selected(client, deck, data.get("selection"), data.get("folder"), execute)
        if action == "queue-selected":
            return queue_selected(client, data.get("selection"), data.get("folder"), execute)
        return perform(client, action, deck, execute)

    if action not in READ_ACTIONS and not execute:
        return invoke(None)
    # Bound the entire serial operation, not just each individual OSC query.
    # Status returns before the browser's 3 s deadline; catalog/load checks
    # cannot retain the server's control lock for dozens of per-query timeouts.
    budget = 2.5 if action == "status" else 4 if action in READ_ACTIONS else 6
    with client_factory(port, timeout=.8, budget_seconds=budget) as client:
        return invoke(client)
