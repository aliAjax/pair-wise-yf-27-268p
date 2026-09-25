"""博物馆藏品来源与返还审查系统。"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "provenance.db"
CLAIM_TRANSITIONS = {
    "submitted": {"under_review"},
    "under_review": {"negotiating", "resolved_return", "rejected"},
    "negotiating": {"resolved_return", "rejected"},
    "resolved_return": set(),
    "rejected": set(),
}


class BusinessError(Exception):
    def __init__(self, message, status=400, code="bad_request"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProvenanceStore:
    def __init__(self, db_path=DEFAULT_DB):
        self.db_path = str(db_path)
        self._lock = threading.Lock()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def init_schema(self):
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('staff','reviewer','claimant','public'))
                );
                CREATE TABLE IF NOT EXISTS sources(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    source_type TEXT NOT NULL, reference TEXT NOT NULL,
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    UNIQUE(name,reference)
                );
                CREATE TABLE IF NOT EXISTS objects(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, inventory_no TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL, object_type TEXT NOT NULL, current_holder TEXT NOT NULL,
                    public_summary TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    event_type TEXT NOT NULL, date_start TEXT NOT NULL, date_end TEXT,
                    place TEXT NOT NULL, description TEXT NOT NULL,
                    source_id INTEGER REFERENCES sources(id),
                    visibility TEXT NOT NULL CHECK(visibility IN ('public','internal')),
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    event_id INTEGER REFERENCES events(id), filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL, size INTEGER NOT NULL, content BLOB NOT NULL,
                    visibility TEXT NOT NULL CHECK(visibility IN ('public','internal')),
                    uploaded_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claims(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    claimant_id TEXT NOT NULL REFERENCES users(id),
                    claimed_by TEXT NOT NULL, desired_outcome TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'submitted'
                        CHECK(status IN ('submitted','under_review','negotiating','resolved_return','rejected')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claim_reviews(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    old_status TEXT NOT NULL, new_status TEXT NOT NULL,
                    note TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS joint_cases(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL UNIQUE REFERENCES objects(id),
                    status TEXT NOT NULL DEFAULT 'pending_verification'
                        CHECK(status IN ('pending_verification','in_review','concluded')),
                    created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS case_parties(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL REFERENCES joint_cases(id),
                    claim_id INTEGER REFERENCES claims(id),
                    name TEXT NOT NULL, identity_relation TEXT NOT NULL,
                    authorization_evidence_id INTEGER REFERENCES evidence(id),
                    qualification TEXT NOT NULL DEFAULT 'pending'
                        CHECK(qualification IN ('pending','confirmed','rejected')),
                    qualification_note TEXT NOT NULL DEFAULT '',
                    qualified_by TEXT REFERENCES users(id), qualified_at TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(case_id,claim_id)
                );
                CREATE TABLE IF NOT EXISTS panels(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL REFERENCES joint_cases(id),
                    seq INTEGER NOT NULL, summary TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','concluded')),
                    conclusion TEXT CHECK(conclusion IN ('return','reject')),
                    effective INTEGER NOT NULL DEFAULT 0,
                    invalidated_at TEXT, invalidation_detail TEXT,
                    opened_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL, concluded_at TEXT,
                    UNIQUE(case_id,seq)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS panels_one_open ON panels(case_id) WHERE status='open';
                CREATE TABLE IF NOT EXISTS panel_votes(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    panel_id INTEGER NOT NULL REFERENCES panels(id),
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    decision TEXT NOT NULL CHECK(decision IN ('return','reject','abstain')),
                    opinion TEXT NOT NULL, evidence_ids TEXT NOT NULL,
                    published INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(panel_id,reviewer_id)
                );
                CREATE TABLE IF NOT EXISTS object_versions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    version INTEGER NOT NULL, snapshot TEXT NOT NULL,
                    changed_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    UNIQUE(object_id,version)
                );
                CREATE TABLE IF NOT EXISTS audit_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, object_id INTEGER REFERENCES objects(id),
                    actor_id TEXT NOT NULL REFERENCES users(id), action TEXT NOT NULL,
                    detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
                """
            )

    def seed(self):
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name,role) VALUES(?,?,?)",
                [
                    ("staff", "藏品研究员", "staff"),
                    ("reviewer1", "返还审查员", "reviewer"),
                    ("reviewer2", "返还审查员乙", "reviewer"),
                    ("reviewer3", "返还审查员丙", "reviewer"),
                    ("claimant1", "权利主张人", "claimant"),
                    ("claimant2", "权利主张人乙", "claimant"),
                    ("public", "公众访客", "public"),
                ],
            )

    def _user(self, conn, user_id, roles=None):
        if not user_id:
            raise BusinessError("缺少 X-User-Id", 401, "authentication_required")
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise BusinessError("用户不存在", 401, "unknown_user")
        if roles and user["role"] not in roles:
            raise BusinessError("当前角色无权执行此操作", 403, "forbidden")
        return user

    def _object(self, conn, object_id):
        row = conn.execute("SELECT * FROM objects WHERE id=?", (object_id,)).fetchone()
        if not row:
            raise BusinessError("藏品不存在", 404, "not_found")
        return row

    def _audit(self, conn, object_id, actor, action, detail):
        conn.execute(
            "INSERT INTO audit_log(object_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (object_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), now()),
        )

    def _snapshot(self, conn, object_id, actor):
        row = self._object(conn, object_id)
        snapshot = {
            "object": dict(row),
            "events": [dict(x) for x in conn.execute("SELECT * FROM events WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
            "claims": [dict(x) for x in conn.execute("SELECT * FROM claims WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
            "joint_case": self._joint_snapshot(conn, object_id),
        }
        conn.execute(
            "INSERT INTO object_versions(object_id,version,snapshot,changed_by,created_at) VALUES(?,?,?,?,?)",
            (object_id, row["version"], json.dumps(snapshot, ensure_ascii=False, sort_keys=True), actor, now()),
        )

    def _joint_snapshot(self, conn, object_id):
        case = conn.execute("SELECT * FROM joint_cases WHERE object_id=?", (object_id,)).fetchone()
        if not case:
            return None
        return {
            "case": dict(case),
            "parties": [dict(x) for x in conn.execute("SELECT * FROM case_parties WHERE case_id=? ORDER BY id", (case["id"],)).fetchall()],
            "panels": [dict(p) | {"votes": [dict(v) for v in conn.execute("SELECT * FROM panel_votes WHERE panel_id=? ORDER BY id", (p["id"],)).fetchall()]}
                       for p in conn.execute("SELECT * FROM panels WHERE case_id=? ORDER BY seq", (case["id"],)).fetchall()],
        }

    def _bump_and_snapshot(self, conn, object_id, actor):
        row = self._object(conn, object_id)
        next_version = row["version"] + 1
        conn.execute("UPDATE objects SET version=?,updated_at=? WHERE id=?", (next_version, now(), object_id))
        self._snapshot(conn, object_id, actor)
        return next_version

    def _invalidate_panels(self, conn, object_id, actor, change):
        """新来源材料补入时让生效结论失效，并记录哪项依据变了。"""
        case = conn.execute("SELECT * FROM joint_cases WHERE object_id=?", (object_id,)).fetchone()
        if not case:
            return
        panels = conn.execute("SELECT * FROM panels WHERE case_id=? AND effective=1", (case["id"],)).fetchall()
        for panel in panels:
            basis = set()
            for v in conn.execute("SELECT evidence_ids FROM panel_votes WHERE panel_id=?", (panel["id"],)).fetchall():
                basis.update(json.loads(v["evidence_ids"]))
            basis_items = []
            if basis:
                marks = ",".join("?" * len(basis))
                basis_items = [dict(r) for r in conn.execute(
                    f"SELECT id,filename,sha256 FROM evidence WHERE id IN ({marks}) ORDER BY id", tuple(sorted(basis))).fetchall()]
            detail = {"reason": "new_source_material", "changed_basis": [change], "panel_basis": basis_items}
            conn.execute(
                "UPDATE panels SET effective=0,invalidated_at=?,invalidation_detail=? WHERE id=?",
                (now(), json.dumps(detail, ensure_ascii=False, sort_keys=True), panel["id"]),
            )
            self._audit(conn, object_id, actor, "panel.invalidate", {"panel_id": panel["id"], "change": change})
        if panels:
            self._refresh_case_status(conn, case["id"])

    def create_object(self, user_id, inventory_no, title, object_type, holder, public_summary):
        inventory_no, title = inventory_no.strip(), title.strip()
        if not inventory_no or len(title) < 2:
            raise BusinessError("库存号和标题不能为空", 422, "invalid_object")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff"})
            try:
                cur = conn.execute(
                    """INSERT INTO objects(inventory_no,title,object_type,current_holder,public_summary,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (inventory_no, title, object_type.strip() or "未分类", holder.strip() or "馆藏", public_summary.strip(), user_id, now(), now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("库存号已存在", 409, "inventory_exists")
            object_id = cur.lastrowid
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "object.create", {"inventory_no": inventory_no})
            return {"id": object_id, "inventory_no": inventory_no, "version": 1}

    def update_object(self, user_id, object_id, changes):
        allowed = {"title", "object_type", "current_holder", "public_summary"}
        clean = {k: str(v).strip() for k, v in changes.items() if k in allowed and str(v).strip()}
        if not clean:
            raise BusinessError("没有可更新字段", 422, "empty_update")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff"})
            row = self._object(conn, object_id)
            new_version = row["version"] + 1
            assignments = ",".join(f"{k}=?" for k in clean)
            conn.execute(
                f"UPDATE objects SET {assignments},version=?,updated_at=? WHERE id=?",
                (*clean.values(), new_version, now(), object_id),
            )
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "object.update", {"version": new_version, "changes": clean})
            return {"id": object_id, "version": new_version, "changes": clean}

    def add_source(self, user_id, name, source_type, reference):
        if not name.strip() or not reference.strip():
            raise BusinessError("来源名称和引用不能为空", 422, "invalid_source")
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            try:
                cur = conn.execute(
                    "INSERT INTO sources(name,source_type,reference,created_by,created_at) VALUES(?,?,?,?,?)",
                    (name.strip(), source_type.strip() or "archive", reference.strip(), user_id, now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("来源记录已存在", 409, "source_exists")
            return {"id": cur.lastrowid, "name": name.strip(), "reference": reference.strip()}

    def add_event(self, user_id, object_id, event_type, date_start, date_end, place, description, source_id=None, visibility="internal"):
        if not event_type.strip() or not description.strip() or not place.strip():
            raise BusinessError("事件类型、地点和说明不能为空", 422, "invalid_event")
        try:
            start = date.fromisoformat(date_start)
            end = date.fromisoformat(date_end) if date_end else start
        except ValueError:
            raise BusinessError("事件日期必须是 YYYY-MM-DD", 422, "invalid_date")
        if end < start:
            raise BusinessError("事件结束日期不能早于开始日期", 422, "invalid_date_range")
        if visibility not in {"public", "internal"}:
            raise BusinessError("visibility 必须是 public 或 internal", 422, "invalid_visibility")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff"})
            self._object(conn, object_id)
            if source_id and not conn.execute("SELECT 1 FROM sources WHERE id=?", (source_id,)).fetchone():
                raise BusinessError("来源不存在", 404, "source_not_found")
            cur = conn.execute(
                """INSERT INTO events(object_id,event_type,date_start,date_end,place,description,source_id,visibility,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (object_id, event_type.strip(), date_start, date_end or None, place.strip(), description.strip(), source_id, visibility, user_id, now()),
            )
            change = {"kind": "event", "id": cur.lastrowid, "event_type": event_type.strip(), "description": description.strip()}
            self._invalidate_panels(conn, object_id, user_id, change)
            new_version = self._bump_and_snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "event.add", {"event_id": cur.lastrowid, "version": new_version, "visibility": visibility})
            return {"id": cur.lastrowid, "object_id": object_id, "object_version": new_version}

    def upload_evidence(self, user_id, object_id, filename, content_b64, visibility, event_id=None):
        if not filename.strip():
            raise BusinessError("文件名不能为空", 422, "invalid_filename")
        if visibility not in {"public", "internal"}:
            raise BusinessError("visibility 必须是 public 或 internal", 422, "invalid_visibility")
        try:
            content = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError):
            raise BusinessError("content_b64 不是合法 Base64", 422, "invalid_base64")
        digest = hashlib.sha256(content).hexdigest()
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff", "reviewer"})
            self._object(conn, object_id)
            if event_id and not conn.execute("SELECT 1 FROM events WHERE id=? AND object_id=?", (event_id, object_id)).fetchone():
                raise BusinessError("证据关联的事件不存在", 404, "event_not_found")
            cur = conn.execute(
                """INSERT INTO evidence(object_id,event_id,filename,sha256,size,content,visibility,uploaded_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (object_id, event_id, filename.strip(), digest, len(content), content, visibility, user_id, now()),
            )
            change = {"kind": "evidence", "id": cur.lastrowid, "filename": filename.strip(), "sha256": digest}
            self._invalidate_panels(conn, object_id, user_id, change)
            self._bump_and_snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "evidence.upload", {"evidence_id": cur.lastrowid, "sha256": digest, "visibility": visibility})
            return {"id": cur.lastrowid, "filename": filename.strip(), "sha256": digest, "size": len(content)}

    def create_claim(self, user_id, object_id, claimed_by, desired_outcome):
        if not claimed_by.strip() or not desired_outcome.strip():
            raise BusinessError("主张人和期望结果不能为空", 422, "invalid_claim")
        with self.connect() as conn:
            claimant = self._user(conn, user_id, {"claimant"})
            self._object(conn, object_id)
            cur = conn.execute(
                """INSERT INTO claims(object_id,claimant_id,claimed_by,desired_outcome,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (object_id, user_id, claimed_by.strip(), desired_outcome.strip(), now(), now()),
            )
            self._audit(conn, object_id, user_id, "claim.create", {"claim_id": cur.lastrowid})
            return {"id": cur.lastrowid, "object_id": object_id, "status": "submitted"}

    def transition_claim(self, user_id, claim_id, new_status, note):
        if len(note.strip()) < 5:
            raise BusinessError("阶段审查说明至少 5 字", 422, "review_note_required")
        with self.connect() as conn:
            reviewer = self._user(conn, user_id, {"reviewer"})
            try:
                conn.execute("BEGIN IMMEDIATE")
                claim = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
                if not claim:
                    raise BusinessError("权利主张不存在", 404, "not_found")
                allowed = CLAIM_TRANSITIONS.get(claim["status"], set())
                if new_status not in allowed:
                    raise BusinessError(f"不能从 {claim['status']} 直接变更为 {new_status}", 409, "invalid_transition")
                conn.execute("UPDATE claims SET status=?,updated_at=? WHERE id=?", (new_status, now(), claim_id))
                conn.execute(
                    "INSERT INTO claim_reviews(claim_id,reviewer_id,old_status,new_status,note,created_at) VALUES(?,?,?,?,?,?)",
                    (claim_id, user_id, claim["status"], new_status, note.strip(), now()),
                )
                next_version = self._bump_and_snapshot(conn, claim["object_id"], user_id)
                self._audit(conn, claim["object_id"], user_id, "claim.transition", {"claim_id": claim_id, "from": claim["status"], "to": new_status})
                return {"claim_id": claim_id, "old_status": claim["status"], "status": new_status, "object_version": next_version}
            except Exception:
                conn.rollback()
                raise

    # ---- 共同审理：同一藏品的多名主张人共用一个案件 ----

    def _case(self, conn, case_id):
        row = conn.execute("SELECT * FROM joint_cases WHERE id=?", (case_id,)).fetchone()
        if not row:
            raise BusinessError("共同审理案件不存在", 404, "not_found")
        return row

    def _refresh_case_status(self, conn, case_id):
        case = self._case(conn, case_id)
        total = conn.execute("SELECT COUNT(*) c FROM case_parties WHERE case_id=?", (case_id,)).fetchone()["c"]
        pending = conn.execute("SELECT COUNT(*) c FROM case_parties WHERE case_id=? AND qualification='pending'", (case_id,)).fetchone()["c"]
        effective = conn.execute("SELECT 1 FROM panels WHERE case_id=? AND effective=1", (case_id,)).fetchone()
        if total == 0 or pending:
            status = "pending_verification"
        elif effective:
            status = "concluded"
        else:
            status = "in_review"
        if status != case["status"]:
            conn.execute("UPDATE joint_cases SET status=?,updated_at=? WHERE id=?", (status, now(), case_id))
        return status

    def create_joint_case(self, user_id, object_id):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff"})
            self._object(conn, object_id)
            try:
                cur = conn.execute(
                    "INSERT INTO joint_cases(object_id,created_by,created_at,updated_at) VALUES(?,?,?,?)",
                    (object_id, user_id, now(), now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("该藏品已建立共同审理案件", 409, "case_exists")
            case_id = cur.lastrowid
            status = self._refresh_case_status(conn, case_id)
            self._bump_and_snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "joint_case.create", {"case_id": case_id})
            return {"id": case_id, "object_id": object_id, "status": status}

    def add_party(self, user_id, case_id, claim_id, name, identity_relation, authorization_evidence_id=None):
        if not name.strip() or not identity_relation.strip():
            raise BusinessError("参与人姓名和身份关系不能为空", 422, "invalid_party")
        for label, value in (("claim_id", claim_id), ("authorization_evidence_id", authorization_evidence_id)):
            if value is not None and not isinstance(value, int):
                raise BusinessError(f"{label} 必须是整数", 422, "invalid_reference")
        with self.connect() as conn:
            self._user(conn, user_id, {"staff"})
            case = self._case(conn, case_id)
            object_id = case["object_id"]
            if claim_id is not None and not conn.execute("SELECT 1 FROM claims WHERE id=? AND object_id=?", (claim_id, object_id)).fetchone():
                raise BusinessError("关联的主张不存在或不属于该藏品", 404, "claim_not_found")
            if authorization_evidence_id is not None and not conn.execute("SELECT 1 FROM evidence WHERE id=? AND object_id=?", (authorization_evidence_id, object_id)).fetchone():
                raise BusinessError("授权材料证据不存在或不属于该藏品", 404, "evidence_not_found")
            try:
                cur = conn.execute(
                    """INSERT INTO case_parties(case_id,claim_id,name,identity_relation,authorization_evidence_id,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (case_id, claim_id, name.strip(), identity_relation.strip(), authorization_evidence_id, now(), now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("该主张已登记为案件参与人", 409, "party_exists")
            # 新参与人加入会改变审理基础，已生效结论一并失效。
            self._invalidate_panels(conn, object_id, user_id, {"kind": "party", "id": cur.lastrowid, "name": name.strip()})
            status = self._refresh_case_status(conn, case_id)
            self._bump_and_snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "joint_case.add_party", {"case_id": case_id, "party_id": cur.lastrowid, "claim_id": claim_id})
            return {"id": cur.lastrowid, "case_id": case_id, "qualification": "pending", "case_status": status}

    def set_qualification(self, user_id, case_id, party_id, qualification, note):
        if qualification not in {"confirmed", "rejected"}:
            raise BusinessError("qualification 必须是 confirmed 或 rejected", 422, "invalid_qualification")
        if len(note.strip()) < 5:
            raise BusinessError("资格核验说明至少 5 字", 422, "qualification_note_required")
        with self.connect() as conn:
            self._user(conn, user_id, {"reviewer"})
            case = self._case(conn, case_id)
            party = conn.execute("SELECT * FROM case_parties WHERE id=? AND case_id=?", (party_id, case_id)).fetchone()
            if not party:
                raise BusinessError("案件参与人不存在", 404, "not_found")
            conn.execute(
                "UPDATE case_parties SET qualification=?,qualification_note=?,qualified_by=?,qualified_at=?,updated_at=? WHERE id=?",
                (qualification, note.strip(), user_id, now(), now(), party_id),
            )
            status = self._refresh_case_status(conn, case_id)
            self._bump_and_snapshot(conn, case["object_id"], user_id)
            self._audit(conn, case["object_id"], user_id, "joint_case.qualify", {"case_id": case_id, "party_id": party_id, "qualification": qualification})
            return {"party_id": party_id, "qualification": qualification, "case_status": status}

    def open_panel(self, user_id, case_id, summary=""):
        with self.connect() as conn:
            self._user(conn, user_id, {"reviewer"})
            case = self._case(conn, case_id)
            if case["status"] == "concluded":
                raise BusinessError("已有生效结论，需待新来源材料使其失效后才能重新合议", 409, "case_concluded")
            seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 s FROM panels WHERE case_id=?", (case_id,)).fetchone()["s"]
            try:
                cur = conn.execute(
                    "INSERT INTO panels(case_id,seq,summary,opened_by,created_at) VALUES(?,?,?,?,?)",
                    (case_id, seq, summary.strip(), user_id, now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("已有进行中的合议，不能重复开庭", 409, "panel_open")
            self._bump_and_snapshot(conn, case["object_id"], user_id)
            self._audit(conn, case["object_id"], user_id, "panel.open", {"case_id": case_id, "panel_id": cur.lastrowid, "seq": seq})
            return {"id": cur.lastrowid, "case_id": case_id, "seq": seq, "status": "open"}

    def cast_vote(self, user_id, panel_id, decision, opinion, evidence_ids, published=False):
        if decision not in {"return", "reject", "abstain"}:
            raise BusinessError("decision 必须是 return、reject 或 abstain", 422, "invalid_decision")
        if len(opinion.strip()) < 5:
            raise BusinessError("合议意见至少 5 字", 422, "opinion_required")
        if not isinstance(evidence_ids, list) or not evidence_ids or not all(isinstance(x, int) for x in evidence_ids):
            raise BusinessError("投票必须列出所用来源证据 evidence_ids", 422, "invalid_basis")
        with self.connect() as conn:
            self._user(conn, user_id, {"reviewer"})
            panel = conn.execute("SELECT * FROM panels WHERE id=?", (panel_id,)).fetchone()
            if not panel:
                raise BusinessError("合议不存在", 404, "not_found")
            if panel["status"] != "open":
                raise BusinessError("合议已结束，不能再投票", 409, "panel_closed")
            case = self._case(conn, panel["case_id"])
            basis = sorted(set(evidence_ids))
            for eid in basis:
                if not conn.execute("SELECT 1 FROM evidence WHERE id=? AND object_id=?", (eid, case["object_id"])).fetchone():
                    raise BusinessError(f"证据 {eid} 不存在或不属于该藏品", 404, "evidence_not_found")
            try:
                cur = conn.execute(
                    "INSERT INTO panel_votes(panel_id,reviewer_id,decision,opinion,evidence_ids,published,created_at) VALUES(?,?,?,?,?,?,?)",
                    (panel_id, user_id, decision, opinion.strip(), json.dumps(basis), 1 if published else 0, now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("该审查员已在本次合议投票", 409, "vote_exists")
            self._bump_and_snapshot(conn, case["object_id"], user_id)
            self._audit(conn, case["object_id"], user_id, "panel.vote", {"panel_id": panel_id, "decision": decision})
            return {"id": cur.lastrowid, "panel_id": panel_id, "decision": decision, "published": bool(published)}

    def conclude_panel(self, user_id, panel_id, conclusion):
        if conclusion not in {"return", "reject"}:
            raise BusinessError("conclusion 必须是 return 或 reject", 422, "invalid_conclusion")
        with self.connect() as conn:
            self._user(conn, user_id, {"reviewer"})
            try:
                conn.execute("BEGIN IMMEDIATE")
                panel = conn.execute("SELECT * FROM panels WHERE id=?", (panel_id,)).fetchone()
                if not panel:
                    raise BusinessError("合议不存在", 404, "not_found")
                if panel["status"] != "open":
                    raise BusinessError("合议已结束", 409, "panel_closed")
                case = self._case(conn, panel["case_id"])
                object_id = case["object_id"]
                total = conn.execute("SELECT COUNT(*) c FROM case_parties WHERE case_id=?", (case["id"],)).fetchone()["c"]
                pending = conn.execute("SELECT COUNT(*) c FROM case_parties WHERE case_id=? AND qualification='pending'", (case["id"],)).fetchone()["c"]
                if total == 0 or pending:
                    raise BusinessError("仍有参与人资格未确认，案件停留在待核验，返还结论不能生效", 409, "qualification_unconfirmed")
                reviewers = conn.execute("SELECT COUNT(*) c FROM users WHERE role='reviewer'").fetchone()["c"]
                votes = conn.execute("SELECT * FROM panel_votes WHERE panel_id=?", (panel_id,)).fetchall()
                participants = len({v["reviewer_id"] for v in votes})
                if participants * 2 <= reviewers:
                    raise BusinessError(f"合议参与 {participants} 人，未超过审查员总数 {reviewers} 的一半", 409, "quorum_not_met")
                agree = sum(1 for v in votes if v["decision"] == conclusion)
                if agree < 2:
                    raise BusinessError("结论至少需要两名审查员同意", 409, "insufficient_agreement")
                conn.execute(
                    "UPDATE panels SET status='concluded',conclusion=?,effective=1,concluded_at=? WHERE id=?",
                    (conclusion, now(), panel_id),
                )
                status = self._refresh_case_status(conn, case["id"])
                # 共同审理结论是返还决定的权威来源，联动已确认资格参与人的主张阶段。
                target = "resolved_return" if conclusion == "return" else "rejected"
                linked = conn.execute(
                    """SELECT c.* FROM claims c JOIN case_parties p ON p.claim_id=c.id
                       WHERE p.case_id=? AND p.qualification='confirmed' AND c.status!=?""",
                    (case["id"], target),
                ).fetchall()
                for claim in linked:
                    conn.execute("UPDATE claims SET status=?,updated_at=? WHERE id=?", (target, now(), claim["id"]))
                    conn.execute(
                        "INSERT INTO claim_reviews(claim_id,reviewer_id,old_status,new_status,note,created_at) VALUES(?,?,?,?,?,?)",
                        (claim["id"], user_id, claim["status"], target, f"共同审理第{panel['seq']}次合议结论生效", now()),
                    )
                self._bump_and_snapshot(conn, object_id, user_id)
                self._audit(conn, object_id, user_id, "panel.conclude", {"panel_id": panel_id, "conclusion": conclusion, "participants": participants, "agree": agree})
                return {"panel_id": panel_id, "conclusion": conclusion, "effective": True, "participants": participants, "agree": agree, "case_status": status}
            except Exception:
                conn.rollback()
                raise

    def _case_public_view(self, conn, case):
        opinions = conn.execute(
            """SELECT p.seq AS panel_seq,u.name AS reviewer,v.decision,v.opinion,v.created_at
               FROM panel_votes v JOIN panels p ON p.id=v.panel_id JOIN users u ON u.id=v.reviewer_id
               WHERE p.case_id=? AND v.published=1 ORDER BY v.id""",
            (case["id"],),
        ).fetchall()
        return {"id": case["id"], "object_id": case["object_id"], "status": case["status"],
                "published_opinions": [dict(o) for o in opinions]}

    def _my_parties(self, conn, case_id, user_id):
        return [dict(r) for r in conn.execute(
            """SELECT p.id,p.name,p.identity_relation,p.qualification,p.qualification_note,p.qualified_at
               FROM case_parties p JOIN claims c ON c.id=p.claim_id
               WHERE p.case_id=? AND c.claimant_id=? ORDER BY p.id""",
            (case_id, user_id),
        ).fetchall()]

    def _case_full_view(self, conn, case):
        obj = self._object(conn, case["object_id"])
        parties = []
        for p in conn.execute("SELECT * FROM case_parties WHERE case_id=? ORDER BY id", (case["id"],)).fetchall():
            d = dict(p)
            ev = conn.execute("SELECT id,filename,sha256 FROM evidence WHERE id=?", (p["authorization_evidence_id"],)).fetchone() if p["authorization_evidence_id"] else None
            d["authorization_evidence"] = dict(ev) if ev else None
            claim = conn.execute("SELECT status FROM claims WHERE id=?", (p["claim_id"],)).fetchone() if p["claim_id"] else None
            d["claim_status"] = claim["status"] if claim else None
            parties.append(d)
        panels = []
        for p in conn.execute("SELECT * FROM panels WHERE case_id=? ORDER BY seq", (case["id"],)).fetchall():
            d = dict(p)
            d["effective"] = bool(d["effective"])
            raw = d.pop("invalidation_detail")
            d["invalidation"] = json.loads(raw) if raw else None
            votes = []
            for v in conn.execute("SELECT * FROM panel_votes WHERE panel_id=? ORDER BY id", (p["id"],)).fetchall():
                vd = dict(v)
                vd["published"] = bool(vd["published"])
                vd["evidence_ids"] = json.loads(vd["evidence_ids"])
                if vd["evidence_ids"]:
                    marks = ",".join("?" * len(vd["evidence_ids"]))
                    vd["evidence"] = [dict(e) for e in conn.execute(
                        f"SELECT id,filename,sha256 FROM evidence WHERE id IN ({marks}) ORDER BY id", tuple(vd["evidence_ids"])).fetchall()]
                else:
                    vd["evidence"] = []
                votes.append(vd)
            d["votes"] = votes
            panels.append(d)
        return {"id": case["id"], "object_id": case["object_id"], "status": case["status"],
                "object": {"id": obj["id"], "inventory_no": obj["inventory_no"], "title": obj["title"]},
                "created_at": case["created_at"], "updated_at": case["updated_at"],
                "parties": parties, "panels": panels}

    def get_joint_case(self, user_id, case_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            case = self._case(conn, case_id)
            if user["role"] in {"staff", "reviewer"}:
                return self._case_full_view(conn, case)
            view = self._case_public_view(conn, case)
            if user["role"] == "claimant":
                view["my_parties"] = self._my_parties(conn, case_id, user_id)
            return view

    def get_object(self, user_id, object_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            obj = self._object(conn, object_id)
            if user["role"] == "public":
                events = conn.execute(
                    "SELECT id,event_type,date_start,date_end,place,description,visibility,created_at FROM events WHERE object_id=? AND visibility='public' ORDER BY id",
                    (object_id,),
                ).fetchall()
                claims = conn.execute(
                    "SELECT id,claimed_by,desired_outcome,status,created_at FROM claims WHERE object_id=? ORDER BY id", (object_id,)
                ).fetchall()
                case = conn.execute("SELECT * FROM joint_cases WHERE object_id=?", (object_id,)).fetchone()
                return {
                    "id": obj["id"], "inventory_no": obj["inventory_no"], "title": obj["title"],
                    "object_type": obj["object_type"], "public_summary": obj["public_summary"], "version": obj["version"],
                    "events": [dict(e) for e in events], "claims": [dict(c) for c in claims],
                    "joint_case": self._case_public_view(conn, case) if case else None,
                }
            result = {
                "id": obj["id"], "inventory_no": obj["inventory_no"], "title": obj["title"],
                "object_type": obj["object_type"], "current_holder": obj["current_holder"],
                "public_summary": obj["public_summary"], "version": obj["version"],
                "events": [dict(x) | {"source": dict(conn.execute("SELECT id,name,source_type,reference FROM sources WHERE id=?", (x["source_id"],)).fetchone()) if x["source_id"] else None,
                                     "evidence": [dict(e) for e in conn.execute("SELECT id,filename,sha256,size,visibility FROM evidence WHERE event_id=? ORDER BY id", (x["id"],)).fetchall()]}
                            for x in conn.execute("SELECT * FROM events WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
                "claims": [dict(c) | {"reviews": [dict(r) for r in conn.execute("SELECT * FROM claim_reviews WHERE claim_id=? ORDER BY id", (c["id"],)).fetchall()]}
                           for c in conn.execute("SELECT * FROM claims WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
                "unlinked_evidence": [dict(e) for e in conn.execute("SELECT id,filename,sha256,size,visibility FROM evidence WHERE object_id=? AND event_id IS NULL ORDER BY id", (object_id,)).fetchall()],
            }
            case = conn.execute("SELECT * FROM joint_cases WHERE object_id=?", (object_id,)).fetchone()
            if user["role"] in {"staff", "reviewer"}:
                result["joint_case"] = self._case_full_view(conn, case) if case else None
            elif case:
                result["joint_case"] = self._case_public_view(conn, case) | {"my_parties": self._my_parties(conn, case["id"], user_id)}
            else:
                result["joint_case"] = None
            if user["role"] == "claimant":
                # 主张人只看到公开来源事件和自己的主张，不能浏览内部调查材料。
                result["events"] = [e for e in result["events"] if e["visibility"] == "public"]
                result["unlinked_evidence"] = []
                result["claims"] = [c for c in result["claims"] if c["claimant_id"] == user_id]
                for c in result["claims"]:
                    c.pop("claimant_id", None)
            return result

    def list_objects(self, user_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            if user["role"] == "public":
                rows = conn.execute("SELECT id,inventory_no,title,object_type,public_summary,version FROM objects ORDER BY id").fetchall()
            else:
                rows = conn.execute("SELECT * FROM objects ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def object_history(self, user_id, object_id):
        with self.connect() as conn:
            user = self._user(conn, user_id, {"staff", "reviewer"})
            self._object(conn, object_id)
            rows = conn.execute("SELECT id,version,changed_by,created_at FROM object_versions WHERE object_id=? ORDER BY version", (object_id,)).fetchall()
            return [dict(r) for r in rows]

    def history_detail(self, user_id, object_id, version):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            row = conn.execute("SELECT * FROM object_versions WHERE object_id=? AND version=?", (object_id, version)).fetchone()
            if not row:
                raise BusinessError("历史版本不存在", 404, "not_found")
            return dict(row) | {"snapshot": json.loads(row["snapshot"])}


class Handler(BaseHTTPRequestHandler):
    server_version = "Provenance/1.0"

    def _store(self): return self.server.store  # type: ignore[attr-defined]

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise BusinessError("请求体必须是合法 JSON", 400, "invalid_json")
        if not isinstance(data, dict):
            raise BusinessError("JSON 顶层必须是对象", 422, "invalid_json")
        return data

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        parts = [p for p in path.split("/") if p]
        user = self.headers.get("X-User-Id", "")
        if method == "GET" and path == "/":
            body = (BASE_DIR / "web" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if method == "GET" and path == "/health": return self._send(200, {"ok": True})
        store = self._store()
        if parts == ["api", "objects"] and method == "GET": return self._send(200, {"items": store.list_objects(user)})
        if parts == ["api", "objects"] and method == "POST":
            d = self._body(); return self._send(201, store.create_object(user, d.get("inventory_no", ""), d.get("title", ""), d.get("object_type", ""), d.get("current_holder", ""), d.get("public_summary", "")))
        if parts == ["api", "sources"] and method == "POST":
            d = self._body(); return self._send(201, store.add_source(user, d.get("name", ""), d.get("source_type", ""), d.get("reference", "")))
        if len(parts) >= 3 and parts[:2] == ["api", "objects"]:
            object_id = int(parts[2])
            if len(parts) == 3 and method == "GET": return self._send(200, store.get_object(user, object_id))
            if len(parts) == 4 and parts[3] == "update" and method == "POST": return self._send(200, store.update_object(user, object_id, self._body().get("changes", {})))
            if len(parts) == 4 and parts[3] == "events" and method == "POST":
                d = self._body(); return self._send(201, store.add_event(user, object_id, d.get("event_type", ""), d.get("date_start", ""), d.get("date_end", ""), d.get("place", ""), d.get("description", ""), d.get("source_id"), d.get("visibility", "internal")))
            if len(parts) == 4 and parts[3] == "evidence" and method == "POST":
                d = self._body(); return self._send(201, store.upload_evidence(user, object_id, d.get("filename", ""), d.get("content_b64", ""), d.get("visibility", "internal"), d.get("event_id")))
            if len(parts) == 4 and parts[3] == "claims" and method == "POST":
                d = self._body(); return self._send(201, store.create_claim(user, object_id, d.get("claimed_by", ""), d.get("desired_outcome", "")))
            if len(parts) == 4 and parts[3] == "joint-case" and method == "POST": return self._send(201, store.create_joint_case(user, object_id))
            if len(parts) == 4 and parts[3] == "history" and method == "GET": return self._send(200, {"items": store.object_history(user, object_id)})
            if len(parts) == 5 and parts[3] == "history" and method == "GET": return self._send(200, store.history_detail(user, object_id, int(parts[4])))
        if len(parts) == 4 and parts[:2] == ["api", "claims"] and parts[3] == "transition" and method == "POST":
            d = self._body(); return self._send(200, store.transition_claim(user, int(parts[2]), d.get("status", ""), d.get("note", "")))
        if len(parts) >= 3 and parts[:2] == ["api", "joint-cases"]:
            case_id = int(parts[2])
            if len(parts) == 3 and method == "GET": return self._send(200, store.get_joint_case(user, case_id))
            if len(parts) == 4 and parts[3] == "parties" and method == "POST":
                d = self._body(); return self._send(201, store.add_party(user, case_id, d.get("claim_id"), d.get("name", ""), d.get("identity_relation", ""), d.get("authorization_evidence_id")))
            if len(parts) == 4 and parts[3] == "panels" and method == "POST":
                d = self._body(); return self._send(201, store.open_panel(user, case_id, d.get("summary", "")))
            if len(parts) == 6 and parts[3] == "parties" and parts[5] == "qualification" and method == "POST":
                d = self._body(); return self._send(200, store.set_qualification(user, case_id, int(parts[4]), d.get("qualification", ""), d.get("note", "")))
        if len(parts) == 4 and parts[:2] == ["api", "panels"] and parts[3] == "votes" and method == "POST":
            d = self._body(); return self._send(201, store.cast_vote(user, int(parts[2]), d.get("decision", ""), d.get("opinion", ""), d.get("evidence_ids", []), d.get("published", False)))
        if len(parts) == 4 and parts[:2] == ["api", "panels"] and parts[3] == "conclude" and method == "POST":
            d = self._body(); return self._send(200, store.conclude_panel(user, int(parts[2]), d.get("conclusion", "")))
        raise BusinessError("接口不存在", 404, "not_found")

    def _handle(self, method):
        try: self._dispatch(method)
        except BusinessError as exc: self._send(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except (ValueError, TypeError): self._send(400, {"error": {"code": "invalid_path", "message": "路径参数格式错误"}})
        except Exception as exc: self._send(500, {"error": {"code": "internal_error", "message": str(exc)}})

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def log_message(self, fmt, *args): print(f"{self.address_string()} - {fmt % args}")


class ProvenanceServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, store): self.store = store; super().__init__(address, Handler)


def main():
    parser = argparse.ArgumentParser(description="博物馆藏品来源与返还审查")
    parser.add_argument("--db", default=str(DEFAULT_DB)); parser.add_argument("--port", type=int, default=8103)
    parser.add_argument("--init", action="store_true"); parser.add_argument("--seed", action="store_true")
    args = parser.parse_args(); store = ProvenanceStore(args.db); store.init_schema()
    if args.seed: store.seed()
    if args.init or args.seed: print(f"数据库已初始化: {args.db}"); return
    server = ProvenanceServer(("127.0.0.1", args.port), store)
    print(f"来源审查系统运行于 http://127.0.0.1:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
