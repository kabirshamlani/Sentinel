"""GitHub kanban — mirror the coaching board to a real GitHub Projects v2 board.

Explicitly commissioned by the owner (2026-07-28, "full access, I need a kanban on
github"). Division of authority:

  * Issue create/update/close go through the APPROVAL BROKER as pre-approved
    action types (create_issue / update_issue on project sentinel-self) — the
    standing grant the owner gave.
  * Project-board plumbing (create the board, add items, set columns) has no
    broker vocabulary and mutates only board metadata, never content — done
    directly via GraphQL under the same commissioned scope.

The local kanban (~/.sentinel/coaching/kanban.json) stays the source of truth;
GitHub is a view. sync() is idempotent and runs at the end of every coaching
cycle, so board moves (backlog → in review → done) appear on GitHub within a
cycle. Board identity is cached in kv 'coaching_github_board'.
"""

from __future__ import annotations

import logging

from conductor import kv
from coaching import kanban

logger = logging.getLogger("coach.board")

OWNER, REPO = "your-org", "sentinel"
BOARD_TITLE = "Sentinel Coaching"

# kanban status -> (column name, color, issue state)
COLUMNS = {
    "backlog":     ("Backlog", "GRAY", "open"),
    "in_progress": ("In progress", "YELLOW", "open"),
    "in_review":   ("In review", "ORANGE", "open"),
    "done":        ("Done", "GREEN", "closed"),
    "rejected":    ("Rejected", "RED", "closed"),
}


def _gql(query: str, variables: dict | None = None) -> dict:
    from tools.github_tools import graphql
    return graphql(query, variables)


def ensure_project() -> dict | None:
    """Create (once) and cache the Projects v2 board with the coach's columns.
    Returns {project_id, number, url, status_field_id, options{name: id}}."""
    cached = kv.get("coaching_github_board")
    if cached and cached.get("project_id"):
        return cached

    viewer = _gql("query { viewer { id login } }")
    if viewer.get("error"):
        logger.warning("board: viewer query failed: %s", viewer["error"])
        return None
    owner_id = viewer["viewer"]["id"]

    created = _gql(
        """mutation($ownerId:ID!,$title:String!){
             createProjectV2(input:{ownerId:$ownerId,title:$title}){
               projectV2 { id number url } } }""",
        {"ownerId": owner_id, "title": BOARD_TITLE})
    if created.get("error"):
        logger.warning("board: create failed: %s", created["error"])
        return None
    proj = created["createProjectV2"]["projectV2"]

    fields = _gql(
        """query($id:ID!){ node(id:$id){ ... on ProjectV2 {
             fields(first:20){ nodes { ... on ProjectV2SingleSelectField {
               id name options { id name } } } } } } }""",
        {"id": proj["id"]})
    status_field = next((f for f in (fields.get("node", {}).get("fields", {}).get("nodes") or [])
                         if f and f.get("name") == "Status"), None)
    if not status_field:
        logger.warning("board: no Status field found")
        return None

    # replace default Todo/In Progress/Done with the coach's real columns
    opts_input = [{"name": name, "color": color, "description": ""}
                  for name, color, _ in COLUMNS.values()]
    updated = _gql(
        """mutation($fieldId:ID!,$opts:[ProjectV2SingleSelectFieldOptionInput!]!){
             updateProjectV2Field(input:{fieldId:$fieldId, singleSelectOptions:$opts}){
               projectV2Field { ... on ProjectV2SingleSelectField {
                 id options { id name } } } } }""",
        {"fieldId": status_field["id"], "opts": opts_input})
    if updated.get("error"):
        # fall back to the default options rather than failing the board
        logger.warning("board: option update failed (%s) — using defaults", updated["error"])
        options = {o["name"]: o["id"] for o in status_field["options"]}
    else:
        options = {o["name"]: o["id"] for o in
                   updated["updateProjectV2Field"]["projectV2Field"]["options"]}

    # link the board to the repo so it appears in the repo's Projects tab
    repo_node = _gql(
        """query($owner:String!,$name:String!){ repository(owner:$owner,name:$name){ id } }""",
        {"owner": OWNER, "name": REPO})
    repo_id = (repo_node.get("repository") or {}).get("id")
    if repo_id:
        _gql("""mutation($projectId:ID!,$repositoryId:ID!){
                  linkProjectV2ToRepository(input:{projectId:$projectId,repositoryId:$repositoryId}){
                    repository { id } } }""",
             {"projectId": proj["id"], "repositoryId": repo_id})

    board = {"project_id": proj["id"], "number": proj["number"], "url": proj["url"],
             "status_field_id": status_field["id"], "options": options}
    kv.put("coaching_github_board", board)
    logger.info("board: created %s", proj["url"])
    return board


def _issue_node_id(number: int) -> str:
    from tools.github_tools import _get
    try:
        return _get(f"/repos/{OWNER}/{REPO}/issues/{number}").get("node_id", "")
    except Exception:
        return ""


async def sync(broker) -> dict:
    """Idempotent full sync: every kanban row gets a GitHub issue (broker-gated),
    a board item, the right column, the right open/closed state."""
    board = ensure_project()
    if not board:
        return {"status": "board_unavailable"}

    stats = {"issues_created": 0, "items_added": 0, "status_set": 0,
             "closed": 0, "errors": 0}
    for row in kanban.issues():
        status = row.get("status", "backlog")
        col_name, _color, want_state = COLUMNS.get(status, COLUMNS["backlog"])
        gh = row.get("github") or {}

        # 1) issue exists (broker-gated create, pre-approved by the owner's grant)
        if not gh.get("number"):
            res = await kanban.mirror_to_github(row, broker)
            row = kanban.get(row["id"]) or row
            gh = row.get("github") or {}
            if not gh.get("number"):
                stats["errors"] += 1
                continue
            stats["issues_created"] += 1

        # 2) item on the board
        if not gh.get("item_id"):
            node_id = _issue_node_id(int(gh["number"]))
            if not node_id:
                stats["errors"] += 1
                continue
            added = _gql(
                """mutation($projectId:ID!,$contentId:ID!){
                     addProjectV2ItemById(input:{projectId:$projectId,contentId:$contentId}){
                       item { id } } }""",
                {"projectId": board["project_id"], "contentId": node_id})
            item_id = (added.get("addProjectV2ItemById") or {}).get("item", {}).get("id")
            if not item_id:
                stats["errors"] += 1
                continue
            gh["item_id"] = item_id
            kanban.move(row["id"], status, github=gh)
            stats["items_added"] += 1

        # 3) column reflects local status
        option_id = board["options"].get(col_name)
        if option_id and gh.get("last_column") != col_name:
            set_res = _gql(
                """mutation($projectId:ID!,$itemId:ID!,$fieldId:ID!,$optionId:String!){
                     updateProjectV2ItemFieldValue(input:{projectId:$projectId,itemId:$itemId,
                       fieldId:$fieldId, value:{singleSelectOptionId:$optionId}}){
                       projectV2Item { id } } }""",
                {"projectId": board["project_id"], "itemId": gh["item_id"],
                 "fieldId": board["status_field_id"], "optionId": option_id})
            if not set_res.get("error"):
                gh["last_column"] = col_name
                kanban.move(row["id"], status, github=gh)
                stats["status_set"] += 1

        # 4) open/closed state follows the board (broker-gated update)
        if gh.get("last_state") != want_state:
            res = await broker.propose(
                action_type="update_issue",
                description=f"Sync coaching issue #{gh['number']} state -> {want_state}",
                details={"owner": OWNER, "repo": REPO, "number": gh["number"],
                         "state": want_state,
                         "labels": ["coaching", f"status:{status}",
                                    f"rca:{row.get('rca_class','')}"]},
                reasoning="Board sync: local kanban is the source of truth.",
                project="sentinel-self")
            exec_result = (res or {}).get("execution_result") or (res or {}).get("result") or {}
            if isinstance(exec_result, dict) and exec_result.get("updated"):
                gh["last_state"] = want_state
                kanban.move(row["id"], status, github=gh)
                if want_state == "closed":
                    stats["closed"] += 1

    return {"status": "synced", "board_url": board["url"], **stats}
