"""Leads — the pipeline, not a submissions log.

Filters are assembled as parameterised fragments; no request value is
ever interpolated into SQL text. Sortable columns come from a fixed map
so `?sort=` cannot reach an arbitrary expression.
"""

import csv
import io
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from .. import db, events
from ..schemas import (
    BulkAction,
    BulkRequest,
    LeadStatus,
    LeadUpdate,
    NoteRequest,
    keep_lines,
    valid_email,
)
from ..security import CurrentUser, client_ip, require_role, require_user, tenant_db

router = APIRouter(prefix="/api/leads", tags=["leads"])

SORTABLE = {
    "created_at": "l.created_at",
    "updated_at": "l.updated_at",
    "full_name": "l.full_name",
    "status": "l.status",
    "follow_up_on": "l.follow_up_on",
}

EXPORT_COLUMNS = [
    "id", "full_name", "email", "phone", "company", "status", "message",
    "source_page", "utm_source", "utm_medium", "utm_campaign", "assignee", "created_at",
]


class LeadFilters:
    """Shared filter builder so the list and the export can't drift apart."""

    def __init__(
        self,
        q: str | None = Query(default=None, max_length=120),
        status: LeadStatus | None = None,
        assigned_to: str | None = Query(default=None, max_length=20),
        utm_source: str | None = Query(default=None, max_length=80),
        date_from: date | None = Query(default=None, alias="from"),
        date_to: date | None = Query(default=None, alias="to"),
        spam: str | None = None,
        sort: str = "created_at",
        dir: str = "desc",
    ) -> None:
        self.q = q
        self.status = status
        self.assigned_to = assigned_to
        self.utm_source = utm_source
        self.date_from = date_from
        self.date_to = date_to
        self.spam = spam
        self.sort = SORTABLE.get(sort, SORTABLE["created_at"])
        self.direction = "ASC" if dir == "asc" else "DESC"

    def build(self, tenant_id: int) -> tuple[str, list[Any]]:
        args: list[Any] = [tenant_id]
        clauses = ["l.tenant_id = $1"]

        def add(fragment: str, value: Any) -> None:
            args.append(value)
            clauses.append(fragment.format(n=len(args)))

        if self.status:
            add("l.status = ${n}::lead_status", self.status.value)
        if self.assigned_to == "unassigned":
            clauses.append("l.assigned_to IS NULL")
        elif self.assigned_to:
            try:
                add("l.assigned_to = ${n}", int(self.assigned_to))
            except ValueError:
                raise HTTPException(400, "Owner filter must be a user id.") from None
        if self.utm_source:
            add("l.utm_source = ${n}", self.utm_source)
        if self.date_from:
            add("l.created_at >= ${n}", self.date_from)
        if self.date_to:
            add("l.created_at < (${n}::date + 1)", self.date_to)

        clauses.append("l.is_spam" if self.spam == "1" else "NOT l.is_spam")

        if self.q:
            args.append(f"%{self.q}%")
            p = f"${len(args)}"
            clauses.append(
                f"(l.full_name ILIKE {p} OR l.email::text ILIKE {p} OR l.phone ILIKE {p}"
                f" OR l.company ILIKE {p} OR l.message ILIKE {p})"
            )

        return " AND ".join(clauses), args


@router.get("")
async def list_leads(
    filters: LeadFilters = Depends(),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=5, le=100),
    user: CurrentUser = Depends(require_user),
) -> dict:
    where, args = filters.build(user.tenant_id)
    args.extend([per_page, (page - 1) * per_page])

    rows = await db.fetch(
        f"""SELECT l.id, l.full_name, l.email, l.phone, l.company, l.status,
                   l.follow_up_on, l.created_at, l.utm_source, l.utm_campaign, l.source_page,
                   u.display_name AS assignee_name, l.assigned_to,
                   count(*) OVER() AS total_count
              FROM leads l
              LEFT JOIN users u ON u.id = l.assigned_to
             WHERE {where}
             ORDER BY {filters.sort} {filters.direction} NULLS LAST
             LIMIT ${len(args) - 1} OFFSET ${len(args)}""",
        *args,
    )

    total = int(rows[0]["total_count"]) if rows else 0
    for row in rows:
        row.pop("total_count", None)

    return {
        "leads": rows,
        "pagination": {
            "page": page,
            "perPage": per_page,
            "total": total,
            "pages": (total + per_page - 1) // per_page,
        },
    }


@router.get("/counts")
async def lead_counts(scoped: db.TenantDB = Depends(tenant_db)) -> dict:
    rows = await scoped.fetch(
        """SELECT status::text AS status, count(*)::int AS n
             FROM leads WHERE tenant_id = $1 AND NOT is_spam GROUP BY status"""
    )
    counts = {status.value: 0 for status in LeadStatus}
    counts.update({row["status"]: row["n"] for row in rows})
    counts["all"] = sum(row["n"] for row in rows)
    return counts


@router.get("/export/csv")
async def export_csv(
    request: Request,
    filters: LeadFilters = Depends(),
    user: CurrentUser = Depends(require_role("agent")),
) -> Response:
    where, args = filters.build(user.tenant_id)

    rows = await db.fetch(
        f"""SELECT l.id, l.full_name, l.email, l.phone, l.company, l.status, l.message,
                   l.source_page, l.utm_source, l.utm_medium, l.utm_campaign,
                   u.display_name AS assignee, l.created_at
              FROM leads l LEFT JOIN users u ON u.id = l.assigned_to
             WHERE {where} ORDER BY l.created_at DESC LIMIT 10000""",
        *args,
    )

    def cell(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        text = str(value)
        # Neutralise spreadsheet formulas: a lead "named" =cmd|'…' must not
        # execute when someone opens the export in Excel.
        return f"'{text}" if text[:1] in "=+-@\t\r" else text

    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL)
    writer.writerow(EXPORT_COLUMNS)
    for row in rows:
        writer.writerow([cell(row[column]) for column in EXPORT_COLUMNS])
    buffer.seek(0)

    await events.log_activity(
        user.tenant_id,
        "lead.exported",
        user_id=user.id,
        meta={"count": len(rows)},
        ip=db.to_inet(client_ip(request)),
    )

    filename = f"leads-{date.today().isoformat()}.csv"
    # Buffered rather than streamed: the export is capped at 10k rows, so
    # the body is small and a correct Content-Length is worth more than
    # incremental delivery.
    return Response(
        content=buffer.getvalue().encode("utf-8-sig"),  # BOM so Excel reads UTF-8
        media_type="text/csv; charset=utf-8",
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/bulk")
async def bulk(
    payload: BulkRequest,
    request: Request,
    user: CurrentUser = Depends(require_role("agent")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)

    if payload.action is BulkAction.status:
        try:
            status = LeadStatus(str(payload.value))
        except ValueError:
            raise HTTPException(400, "Unknown pipeline stage.") from None
        rows = await scoped.fetch(
            """UPDATE leads SET status = $3::lead_status
                WHERE tenant_id = $1 AND id = ANY($2::bigint[]) RETURNING id""",
            payload.ids,
            status.value,
        )
    elif payload.action is BulkAction.assign:
        assignee = int(payload.value) if payload.value else None
        rows = await scoped.fetch(
            """UPDATE leads SET assigned_to = $3
                WHERE tenant_id = $1 AND id = ANY($2::bigint[]) RETURNING id""",
            payload.ids,
            assignee,
        )
    elif payload.action is BulkAction.spam:
        rows = await scoped.fetch(
            """UPDATE leads SET is_spam = TRUE
                WHERE tenant_id = $1 AND id = ANY($2::bigint[]) RETURNING id""",
            payload.ids,
        )
    else:  # delete
        if user.role not in ("owner", "admin"):
            raise HTTPException(403, "Deleting leads needs admin access.")
        rows = await scoped.fetch(
            "DELETE FROM leads WHERE tenant_id = $1 AND id = ANY($2::bigint[]) RETURNING id",
            payload.ids,
        )

    await events.log_activity(
        user.tenant_id,
        f"lead.bulk_{payload.action.value}",
        user_id=user.id,
        meta={"count": len(rows), "value": payload.value},
        ip=db.to_inet(client_ip(request)),
    )
    return {"updated": len(rows)}


@router.get("/{lead_id}")
async def lead_detail(lead_id: int, scoped: db.TenantDB = Depends(tenant_db)) -> dict:
    lead = await scoped.fetch_one(
        """SELECT l.*, u.display_name AS assignee_name, f.name AS form_name
             FROM leads l
             LEFT JOIN users u ON u.id = l.assigned_to
             LEFT JOIN forms f ON f.id = l.form_id
            WHERE l.tenant_id = $1 AND l.id = $2""",
        lead_id,
    )
    if not lead:
        raise HTTPException(404, "That lead no longer exists.")

    notes = await scoped.fetch(
        """SELECT n.id, n.body, n.created_at, u.display_name AS author
             FROM lead_notes n LEFT JOIN users u ON u.id = n.user_id
            WHERE n.tenant_id = $1 AND n.lead_id = $2
            ORDER BY n.created_at DESC""",
        lead_id,
    )
    if lead.get("ip") is not None:
        lead["ip"] = str(lead["ip"])
    return {"lead": lead, "notes": notes}


@router.patch("/{lead_id}")
async def update_lead(
    lead_id: int,
    payload: LeadUpdate,
    request: Request,
    user: CurrentUser = Depends(require_role("agent")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    before = await scoped.fetch_one(
        "SELECT id, status, assigned_to FROM leads WHERE tenant_id = $1 AND id = $2", lead_id
    )
    if not before:
        raise HTTPException(404, "That lead no longer exists.")

    sent = payload.model_dump(exclude_unset=True)
    if not sent:
        raise HTTPException(400, "Nothing to save.")

    args: list[Any] = [user.tenant_id, lead_id]
    assignments: list[str] = []

    def assign(column: str, value: Any, cast: str = "") -> None:
        args.append(value)
        assignments.append(f"{column} = ${len(args)}{cast}")

    if "status" in sent:
        assign("status", payload.status.value, "::lead_status")
    if "assigned_to" in sent:
        assign("assigned_to", payload.assigned_to or None)
    if "follow_up_on" in sent:
        assign("follow_up_on", payload.follow_up_on)
    if "full_name" in sent:
        assign("full_name", payload.full_name or "Unnamed lead")
    if "email" in sent:
        assign("email", valid_email(payload.email))
    if "phone" in sent:
        assign("phone", payload.phone)
    if "company" in sent:
        assign("company", payload.company)
    if "value_amount" in sent:
        assign("value_amount", payload.value_amount)
    if "is_spam" in sent:
        assign("is_spam", bool(payload.is_spam))

    lead = await db.fetch_one(
        f"""UPDATE leads SET {', '.join(assignments)}
             WHERE tenant_id = $1 AND id = $2 RETURNING *""",
        *args,
    )
    if lead and lead.get("ip") is not None:
        lead["ip"] = str(lead["ip"])

    ip = db.to_inet(client_ip(request))
    if "status" in sent and payload.status.value != before["status"]:
        await events.log_activity(
            user.tenant_id,
            "lead.status_changed",
            user_id=user.id,
            object_type="lead",
            object_id=lead_id,
            meta={"from": before["status"], "to": payload.status.value},
            ip=ip,
        )
        await events.emit(
            user.tenant_id,
            "lead.status_changed",
            {
                "id": lead_id,
                "from": before["status"],
                "to": payload.status.value,
                "name": lead["full_name"],
            },
        )
    else:
        await events.log_activity(
            user.tenant_id,
            "lead.updated",
            user_id=user.id,
            object_type="lead",
            object_id=lead_id,
            meta={"fields": list(sent.keys())},
            ip=ip,
        )

    return {"lead": lead}


@router.post("/{lead_id}/notes", status_code=201)
async def add_note(
    lead_id: int,
    payload: NoteRequest,
    request: Request,
    user: CurrentUser = Depends(require_role("agent")),
) -> dict:
    scoped = db.TenantDB(user.tenant_id)
    body = keep_lines(payload.body, 4000)
    if not body:
        raise HTTPException(400, "Write something first.")

    exists = await scoped.fetch_one(
        "SELECT 1 FROM leads WHERE tenant_id = $1 AND id = $2", lead_id
    )
    if not exists:
        raise HTTPException(404, "That lead no longer exists.")

    note = await scoped.fetch_one(
        """INSERT INTO lead_notes (tenant_id, lead_id, user_id, body)
           VALUES ($1, $2, $3, $4) RETURNING id, body, created_at""",
        lead_id,
        user.id,
        body,
    )
    await events.log_activity(
        user.tenant_id,
        "lead.note_added",
        user_id=user.id,
        object_type="lead",
        object_id=lead_id,
        ip=db.to_inet(client_ip(request)),
    )
    return {"note": {**note, "author": user.name}}


@router.get("/{lead_id}/timeline")
async def lead_timeline(lead_id: int, scoped: db.TenantDB = Depends(tenant_db)) -> dict:
    """One merged history for a lead: notes, audit entries, conversion
    events and its own form submission, newest first.

    Merged in Python rather than a UNION so each source keeps its own
    shape — the UI renders a note differently from a status change, and
    a three-way UNION would flatten them into a lowest-common-denominator
    row.
    """
    lead = await scoped.fetch_one(
        """SELECT l.id, l.full_name, l.email::text AS email, l.status::text AS status,
                  l.created_at, l.assigned_to, u.display_name AS assignee_name,
                  f.name AS form_name
             FROM leads l
             LEFT JOIN users u ON u.id = l.assigned_to
             LEFT JOIN forms f ON f.id = l.form_id
            WHERE l.tenant_id = $1 AND l.id = $2""",
        lead_id,
    )
    if not lead:
        raise HTTPException(404, "That lead no longer exists.")

    entries: list[dict] = []

    for note in await scoped.fetch(
        """SELECT n.id, n.body, n.created_at, u.display_name AS actor
             FROM lead_notes n LEFT JOIN users u ON u.id = n.user_id
            WHERE n.tenant_id = $1 AND n.lead_id = $2""",
        lead_id,
    ):
        entries.append(
            {
                "kind": "note",
                "at": note["created_at"],
                "actor": note["actor"],
                "title": "Note added",
                "detail": note["body"],
                "id": note["id"],
            }
        )

    for entry in await scoped.fetch(
        """SELECT a.id, a.action, a.meta, a.created_at, u.display_name AS actor
             FROM activity_log a LEFT JOIN users u ON u.id = a.user_id
            WHERE a.tenant_id = $1 AND a.object_type = 'lead' AND a.object_id = $2""",
        lead_id,
    ):
        entries.append(
            {
                "kind": "activity",
                "at": entry["created_at"],
                "actor": entry["actor"],
                "title": _describe_lead_action(entry["action"], entry["meta"] or {}),
                "detail": None,
                "meta": entry["meta"],
                "id": entry["id"],
            }
        )

    for conversion in await scoped.fetch(
        """SELECT id, kind, name, label, source_page, created_at
             FROM conversion_events WHERE tenant_id = $1 AND lead_id = $2""",
        lead_id,
    ):
        entries.append(
            {
                "kind": "conversion",
                "at": conversion["created_at"],
                "actor": None,
                "title": f"{conversion['kind'].title()} conversion: {conversion['name']}",
                "detail": conversion["source_page"],
                "id": conversion["id"],
            }
        )

    for submission in await scoped.fetch(
        """SELECT s.id, s.payload, s.created_at, f.name AS form_name
             FROM form_submissions s LEFT JOIN forms f ON f.id = s.form_id
            WHERE s.tenant_id = $1 AND s.lead_id = $2""",
        lead_id,
    ):
        entries.append(
            {
                "kind": "submission",
                "at": submission["created_at"],
                "actor": None,
                "title": f"Submitted “{submission['form_name'] or 'a form'}”",
                "detail": None,
                "payload": submission["payload"],
                "id": submission["id"],
            }
        ) 

    entries.sort(key=lambda entry: entry["at"], reverse=True)
    return {"lead": lead, "timeline": entries}


LEAD_ACTION_LABELS = {
    "lead.created": "Lead captured",
    "lead.updated": "Lead updated",
    "lead.status_changed": "Status changed",
    "lead.assigned": "Assigned",
    "lead.note_added": "Note added",
    "lead.deleted": "Lead deleted",
}


def _describe_lead_action(action: str, meta: dict) -> str:
    label = LEAD_ACTION_LABELS.get(action, action.replace("lead.", "").replace("_", " ").title())
    if action == "lead.status_changed" and meta.get("to"):
        return f"Status → {meta['to']}"
    if meta.get("fields"):
        return f"{label}: {', '.join(str(f) for f in meta['fields'][:6])}"
    return label
