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
JOINT_STAGES = {
    "pending_verification": "待核验",
    "deliberating": "合议中",
    "concluded": "结论生效",
}
OUTCOME_LABELS = {"continue": "继续审理", "return": "返还", "reject": "驳回主张"}
CONCLUSION_LABELS = {
    "none": "无结论",
    "effective": "已生效",
    "not_effective": "未生效",
    "blocked_qualification": "资格待核验未生效",
    "invalidated": "已失效",
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
                CREATE TABLE IF NOT EXISTS joint_cases(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending_verification'
                        CHECK(status IN ('pending_verification','deliberating','concluded')),
                    panel TEXT NOT NULL,
                    created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS joint_participants(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL REFERENCES joint_cases(id),
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    claimant_id TEXT NOT NULL REFERENCES users(id),
                    claimed_by TEXT NOT NULL,
                    identity_relation TEXT NOT NULL, authorization TEXT NOT NULL,
                    evidence_id INTEGER REFERENCES evidence(id),
                    qualification TEXT NOT NULL DEFAULT 'pending'
                        CHECK(qualification IN ('pending','confirmed','rejected')),
                    qualification_note TEXT, verified_by TEXT REFERENCES users(id), verified_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(case_id,claim_id)
                );
                CREATE TABLE IF NOT EXISTS joint_sessions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL REFERENCES joint_cases(id),
                    opinion TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK(outcome IN ('continue','return','reject')),
                    published INTEGER NOT NULL DEFAULT 0,
                    conclusion_status TEXT NOT NULL
                        CHECK(conclusion_status IN ('none','effective','not_effective','blocked_qualification','invalidated')),
                    conclusion_reason TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS joint_votes(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL REFERENCES joint_sessions(id),
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    vote TEXT NOT NULL CHECK(vote IN ('agree','disagree','abstain')),
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id,reviewer_id)
                );
                CREATE TABLE IF NOT EXISTS joint_basis(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL REFERENCES joint_sessions(id),
                    item_kind TEXT NOT NULL CHECK(item_kind IN ('source','evidence')),
                    item_id INTEGER NOT NULL, item_label TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS joint_invalidations(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL REFERENCES joint_sessions(id),
                    item_kind TEXT NOT NULL CHECK(item_kind IN ('source','evidence','session')),
                    item_id INTEGER NOT NULL, item_label TEXT NOT NULL,
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
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
                    ("reviewer2", "审查员乙", "reviewer"),
                    ("reviewer3", "审查员丙", "reviewer"),
                    ("claimant1", "权利主张人", "claimant"),
                    ("claimant2", "第二主张人", "claimant"),
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
        }
        conn.execute(
            "INSERT INTO object_versions(object_id,version,snapshot,changed_by,created_at) VALUES(?,?,?,?,?)",
            (object_id, row["version"], json.dumps(snapshot, ensure_ascii=False, sort_keys=True), actor, now()),
        )

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
            row = self._object(conn, object_id)
            if source_id and not conn.execute("SELECT 1 FROM sources WHERE id=?", (source_id,)).fetchone():
                raise BusinessError("来源不存在", 404, "source_not_found")
            cur = conn.execute(
                """INSERT INTO events(object_id,event_type,date_start,date_end,place,description,source_id,visibility,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (object_id, event_type.strip(), date_start, date_end or None, place.strip(), description.strip(), source_id, visibility, user_id, now()),
            )
            new_version = row["version"] + 1
            conn.execute("UPDATE objects SET version=?,updated_at=? WHERE id=?", (new_version, now(), object_id))
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "event.add", {"event_id": cur.lastrowid, "version": new_version, "visibility": visibility})
            if source_id:
                src = conn.execute("SELECT name,reference FROM sources WHERE id=?", (source_id,)).fetchone()
                self._invalidate_conclusions(conn, object_id, "source", source_id, f"{src['name']}（{src['reference']}）", user_id)
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
            self._audit(conn, object_id, user_id, "evidence.upload", {"evidence_id": cur.lastrowid, "sha256": digest, "visibility": visibility})
            self._invalidate_conclusions(conn, object_id, "evidence", cur.lastrowid, filename.strip(), user_id)
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
                new_version = claim["object_id"]
                obj = self._object(conn, claim["object_id"])
                next_version = obj["version"] + 1
                conn.execute("UPDATE objects SET version=?,updated_at=? WHERE id=?", (next_version, now(), claim["object_id"]))
                self._snapshot(conn, claim["object_id"], user_id)
                self._audit(conn, claim["object_id"], user_id, "claim.transition", {"claim_id": claim_id, "from": claim["status"], "to": new_status})
                return {"claim_id": claim_id, "old_status": claim["status"], "status": new_status, "object_version": next_version}
            except Exception:
                conn.rollback()
                raise

    def _joint_case(self, conn, case_id):
        row = conn.execute("SELECT * FROM joint_cases WHERE id=?", (case_id,)).fetchone()
        if not row:
            raise BusinessError("共同审理案不存在", 404, "not_found")
        return row

    def _refresh_case_stage(self, conn, case_id):
        pending = conn.execute("SELECT COUNT(*) AS c FROM joint_participants WHERE case_id=? AND qualification='pending'", (case_id,)).fetchone()["c"]
        total = conn.execute("SELECT COUNT(*) AS c FROM joint_participants WHERE case_id=?", (case_id,)).fetchone()["c"]
        effective = conn.execute("SELECT COUNT(*) AS c FROM joint_sessions WHERE case_id=? AND conclusion_status='effective'", (case_id,)).fetchone()["c"]
        if total == 0 or pending > 0:
            stage = "pending_verification"
        elif effective > 0:
            stage = "concluded"
        else:
            stage = "deliberating"
        conn.execute("UPDATE joint_cases SET status=?,updated_at=? WHERE id=?", (stage, now(), case_id))
        return stage

    def _invalidate_conclusions(self, conn, object_id, item_kind, item_id, item_label, actor):
        for case in conn.execute("SELECT id FROM joint_cases WHERE object_id=?", (object_id,)).fetchall():
            effective = conn.execute("SELECT id FROM joint_sessions WHERE case_id=? AND conclusion_status='effective'", (case["id"],)).fetchall()
            for s in effective:
                conn.execute(
                    "UPDATE joint_sessions SET conclusion_status='invalidated', conclusion_reason=? WHERE id=?",
                    (f"新来源材料补入：{item_label}", s["id"]),
                )
                conn.execute(
                    "INSERT INTO joint_invalidations(session_id,item_kind,item_id,item_label,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (s["id"], item_kind, item_id, item_label, actor, now()),
                )
            if effective:
                self._refresh_case_stage(conn, case["id"])
                self._audit(conn, object_id, actor, "joint_case.invalidate", {"case_id": case["id"], "sessions": [s["id"] for s in effective], "item": item_label})

    def create_joint_case(self, user_id, object_id, title, panel):
        title = title.strip()
        if len(title) < 2:
            raise BusinessError("共同审理标题不能为空", 422, "invalid_joint_case")
        if not isinstance(panel, list):
            raise BusinessError("panel 必须是审查员 ID 列表", 422, "invalid_panel")
        panel = list(dict.fromkeys(str(p) for p in panel))
        if len(panel) < 2:
            raise BusinessError("合议审查员至少两人", 422, "panel_too_small")
        with self.connect() as conn:
            self._user(conn, user_id, {"staff"})
            self._object(conn, object_id)
            for rid in panel:
                member = conn.execute("SELECT role FROM users WHERE id=?", (rid,)).fetchone()
                if not member or member["role"] != "reviewer":
                    raise BusinessError(f"合议成员 {rid} 不是审查员", 422, "invalid_panel")
            cur = conn.execute(
                "INSERT INTO joint_cases(object_id,title,status,panel,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (object_id, title, "pending_verification", json.dumps(panel, ensure_ascii=False), user_id, now(), now()),
            )
            self._audit(conn, object_id, user_id, "joint_case.create", {"case_id": cur.lastrowid, "panel": panel})
            return {"id": cur.lastrowid, "object_id": object_id, "status": "pending_verification",
                    "stage_label": JOINT_STAGES["pending_verification"], "panel": panel}

    def add_participant(self, user_id, case_id, claim_id, identity_relation, authorization, evidence_id=None):
        if not identity_relation.strip() or not authorization.strip():
            raise BusinessError("身份关系和授权材料不能为空", 422, "invalid_participant")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff", "claimant"})
            case = self._joint_case(conn, case_id)
            claim = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
            if not claim or claim["object_id"] != case["object_id"]:
                raise BusinessError("主张不存在或不属于该藏品", 404, "claim_not_found")
            if actor["role"] == "claimant" and claim["claimant_id"] != user_id:
                raise BusinessError("只能登记自己的主张", 403, "forbidden")
            if evidence_id and not conn.execute("SELECT 1 FROM evidence WHERE id=? AND object_id=?", (evidence_id, case["object_id"])).fetchone():
                raise BusinessError("授权材料证据不存在", 404, "evidence_not_found")
            try:
                cur = conn.execute(
                    """INSERT INTO joint_participants(case_id,claim_id,claimant_id,claimed_by,identity_relation,authorization,evidence_id,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (case_id, claim_id, claim["claimant_id"], claim["claimed_by"], identity_relation.strip(), authorization.strip(), evidence_id, now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("该主张已登记为本案参与人", 409, "participant_exists")
            stage = self._refresh_case_stage(conn, case_id)
            self._audit(conn, case["object_id"], user_id, "joint_case.participant", {"case_id": case_id, "participant_id": cur.lastrowid, "claim_id": claim_id})
            return {"id": cur.lastrowid, "case_id": case_id, "qualification": "pending",
                    "case_status": stage, "stage_label": JOINT_STAGES[stage]}

    def verify_participant(self, user_id, case_id, participant_id, decision, note=""):
        if decision not in {"confirmed", "rejected"}:
            raise BusinessError("核验结论必须是 confirmed 或 rejected", 422, "invalid_decision")
        if decision == "rejected" and len(note.strip()) < 2:
            raise BusinessError("驳回资格必须说明理由", 422, "verify_note_required")
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            case = self._joint_case(conn, case_id)
            part = conn.execute("SELECT * FROM joint_participants WHERE id=? AND case_id=?", (participant_id, case_id)).fetchone()
            if not part:
                raise BusinessError("参与人不存在", 404, "not_found")
            conn.execute(
                "UPDATE joint_participants SET qualification=?,qualification_note=?,verified_by=?,verified_at=? WHERE id=?",
                (decision, note.strip(), user_id, now(), participant_id),
            )
            stage = self._refresh_case_stage(conn, case_id)
            self._audit(conn, case["object_id"], user_id, "joint_case.verify", {"case_id": case_id, "participant_id": participant_id, "decision": decision})
            return {"participant_id": participant_id, "qualification": decision,
                    "case_status": stage, "stage_label": JOINT_STAGES[stage]}

    def create_session(self, user_id, case_id, opinion, outcome, published, votes, basis):
        opinion = opinion.strip()
        if len(opinion) < 5:
            raise BusinessError("合议意见至少 5 字", 422, "opinion_required")
        if outcome not in OUTCOME_LABELS:
            raise BusinessError("结论类型必须是 continue/return/reject", 422, "invalid_outcome")
        if not isinstance(votes, list) or not isinstance(basis, list):
            raise BusinessError("votes 和 basis 必须是列表", 422, "invalid_session")
        with self.connect() as conn:
            self._user(conn, user_id, {"reviewer"})
            case = self._joint_case(conn, case_id)
            panel = json.loads(case["panel"])
            if user_id not in panel:
                raise BusinessError("只有合议成员可以录入合议记录", 403, "not_panel_member")
            clean_votes, seen = [], set()
            for v in votes:
                rid, vote = str(v.get("reviewer_id", "")), v.get("vote", "")
                if rid not in panel:
                    raise BusinessError(f"投票人 {rid} 不在合议名单内", 422, "voter_not_in_panel")
                if vote not in {"agree", "disagree", "abstain"}:
                    raise BusinessError("投票必须是 agree/disagree/abstain", 422, "invalid_vote")
                if rid in seen:
                    raise BusinessError("同一审查员重复投票", 422, "duplicate_vote")
                seen.add(rid)
                clean_votes.append((rid, vote))
            clean_basis = []
            for b in basis:
                kind, item_id = b.get("kind"), b.get("id")
                if kind == "source":
                    src = conn.execute("SELECT name,reference FROM sources WHERE id=?", (item_id,)).fetchone()
                    if not src:
                        raise BusinessError(f"来源 {item_id} 不存在", 404, "source_not_found")
                    label = f"{src['name']}（{src['reference']}）"
                elif kind == "evidence":
                    ev = conn.execute("SELECT filename,object_id FROM evidence WHERE id=?", (item_id,)).fetchone()
                    if not ev or ev["object_id"] != case["object_id"]:
                        raise BusinessError(f"证据 {item_id} 不存在或不属于该藏品", 404, "evidence_not_found")
                    label = ev["filename"]
                else:
                    raise BusinessError("依据类型必须是 source 或 evidence", 422, "invalid_basis")
                clean_basis.append((kind, int(item_id), label))
            if outcome != "continue":
                if not clean_votes:
                    raise BusinessError("形成结论必须有投票记录", 422, "votes_required")
                if not clean_basis:
                    raise BusinessError("形成结论必须登记所用来源证据", 422, "basis_required")
            agrees = sum(1 for _, v in clean_votes if v == "agree")
            turnout = len(clean_votes)
            pending = conn.execute("SELECT COUNT(*) AS c FROM joint_participants WHERE case_id=? AND qualification='pending'", (case_id,)).fetchone()["c"]
            total = conn.execute("SELECT COUNT(*) AS c FROM joint_participants WHERE case_id=?", (case_id,)).fetchone()["c"]
            if outcome == "continue":
                status, reason = "none", ""
            elif total == 0 or pending > 0:
                status, reason = "blocked_qualification", "存在待核验参与人，返还结论不能生效"
            elif not (turnout * 2 > len(panel) and agrees >= 2):
                status, reason = "not_effective", f"参与 {turnout}/{len(panel)}、同意 {agrees} 人，未满足参与过半且至少两名审查员同意"
            else:
                status, reason = "effective", ""
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    """INSERT INTO joint_sessions(case_id,opinion,outcome,published,conclusion_status,conclusion_reason,created_by,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (case_id, opinion, outcome, 1 if published else 0, status, reason, user_id, now()),
                )
                session_id = cur.lastrowid
                if status == "effective":
                    for old in conn.execute("SELECT id FROM joint_sessions WHERE case_id=? AND conclusion_status='effective' AND id<>?", (case_id, session_id)).fetchall():
                        conn.execute("UPDATE joint_sessions SET conclusion_status='invalidated', conclusion_reason='被新合议结论取代' WHERE id=?", (old["id"],))
                        conn.execute(
                            "INSERT INTO joint_invalidations(session_id,item_kind,item_id,item_label,created_by,created_at) VALUES(?,?,?,?,?,?)",
                            (old["id"], "session", session_id, "被新合议结论取代", user_id, now()),
                        )
                conn.executemany(
                    "INSERT INTO joint_votes(session_id,reviewer_id,vote,created_at) VALUES(?,?,?,?)",
                    [(session_id, rid, v, now()) for rid, v in clean_votes],
                )
                conn.executemany(
                    "INSERT INTO joint_basis(session_id,item_kind,item_id,item_label,created_at) VALUES(?,?,?,?,?)",
                    [(session_id, k, i, label, now()) for k, i, label in clean_basis],
                )
                stage = self._refresh_case_stage(conn, case_id)
                self._audit(conn, case["object_id"], user_id, "joint_case.session",
                            {"case_id": case_id, "session_id": session_id, "outcome": outcome, "conclusion_status": status})
            except Exception:
                conn.rollback()
                raise
            return {"session_id": session_id, "case_id": case_id, "conclusion_status": status,
                    "conclusion_label": CONCLUSION_LABELS[status], "reason": reason,
                    "case_status": stage, "stage_label": JOINT_STAGES[stage]}

    def list_joint_cases(self, user_id, object_id):
        with self.connect() as conn:
            self._user(conn, user_id)
            self._object(conn, object_id)
            rows = conn.execute("SELECT id,title,status,created_at FROM joint_cases WHERE object_id=? ORDER BY id", (object_id,)).fetchall()
            return [{"id": r["id"], "title": r["title"], "stage": r["status"],
                     "stage_label": JOINT_STAGES[r["status"]], "created_at": r["created_at"]} for r in rows]

    def get_joint_case(self, user_id, case_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            case = self._joint_case(conn, case_id)
            base = {"id": case["id"], "object_id": case["object_id"], "title": case["title"],
                    "stage": case["status"], "stage_label": JOINT_STAGES[case["status"]]}
            opinions = [dict(s) | {"outcome_label": OUTCOME_LABELS[s["outcome"]], "conclusion_label": CONCLUSION_LABELS[s["conclusion_status"]]}
                        for s in conn.execute(
                            "SELECT id,opinion,outcome,conclusion_status,created_at FROM joint_sessions WHERE case_id=? AND published=1 ORDER BY id",
                            (case_id,)).fetchall()]
            if user["role"] == "public":
                # 公众只看阶段和已公开意见。
                return base | {"opinions": opinions}
            if user["role"] == "claimant":
                mine = conn.execute(
                    "SELECT id,claimed_by,identity_relation,qualification,qualification_note,created_at FROM joint_participants WHERE case_id=? AND claimant_id=?",
                    (case_id, user_id)).fetchone()
                return base | {"opinions": opinions, "my_participation": dict(mine) if mine else None}
            participants = [dict(p) for p in conn.execute("SELECT * FROM joint_participants WHERE case_id=? ORDER BY id", (case_id,)).fetchall()]
            sessions = []
            for s in conn.execute("SELECT * FROM joint_sessions WHERE case_id=? ORDER BY id", (case_id,)).fetchall():
                sessions.append(dict(s) | {
                    "outcome_label": OUTCOME_LABELS[s["outcome"]],
                    "conclusion_label": CONCLUSION_LABELS[s["conclusion_status"]],
                    "votes": [dict(v) for v in conn.execute("SELECT reviewer_id,vote,created_at FROM joint_votes WHERE session_id=? ORDER BY id", (s["id"],)).fetchall()],
                    "basis": [dict(b) for b in conn.execute("SELECT item_kind,item_id,item_label FROM joint_basis WHERE session_id=? ORDER BY id", (s["id"],)).fetchall()],
                    "invalidations": [dict(i) for i in conn.execute("SELECT item_kind,item_id,item_label,created_at FROM joint_invalidations WHERE session_id=? ORDER BY id", (s["id"],)).fetchall()],
                })
            relations = {}
            for p in participants:
                relations.setdefault(p["identity_relation"], []).append(p["claimed_by"])
            conflicts = [{"identity_relation": k, "claimed_by": v} for k, v in relations.items() if len(v) > 1]
            return base | {"panel": json.loads(case["panel"]), "participants": participants, "sessions": sessions,
                           "qualification_conflicts": conflicts,
                           "created_by": case["created_by"], "created_at": case["created_at"]}

    def get_object(self, user_id, object_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            obj = self._object(conn, object_id)
            joint_cases = [
                {"id": r["id"], "title": r["title"], "stage": r["status"], "stage_label": JOINT_STAGES[r["status"]]}
                for r in conn.execute("SELECT id,title,status FROM joint_cases WHERE object_id=? ORDER BY id", (object_id,)).fetchall()
            ]
            if user["role"] == "public":
                events = conn.execute(
                    "SELECT id,event_type,date_start,date_end,place,description,visibility,created_at FROM events WHERE object_id=? AND visibility='public' ORDER BY id",
                    (object_id,),
                ).fetchall()
                claims = conn.execute(
                    "SELECT id,claimed_by,desired_outcome,status,created_at FROM claims WHERE object_id=? ORDER BY id", (object_id,)
                ).fetchall()
                return {
                    "id": obj["id"], "inventory_no": obj["inventory_no"], "title": obj["title"],
                    "object_type": obj["object_type"], "public_summary": obj["public_summary"], "version": obj["version"],
                    "events": [dict(e) for e in events], "claims": [dict(c) for c in claims],
                    "joint_cases": joint_cases,
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
                "joint_cases": joint_cases,
            }
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
            if len(parts) == 4 and parts[3] == "joint-cases" and method == "GET":
                return self._send(200, {"items": store.list_joint_cases(user, object_id)})
            if len(parts) == 4 and parts[3] == "joint-cases" and method == "POST":
                d = self._body(); return self._send(201, store.create_joint_case(user, object_id, d.get("title", ""), d.get("panel", [])))
            if len(parts) == 4 and parts[3] == "history" and method == "GET": return self._send(200, {"items": store.object_history(user, object_id)})
            if len(parts) == 5 and parts[3] == "history" and method == "GET": return self._send(200, store.history_detail(user, object_id, int(parts[4])))
        if len(parts) >= 3 and parts[:2] == ["api", "joint-cases"]:
            case_id = int(parts[2])
            if len(parts) == 3 and method == "GET": return self._send(200, store.get_joint_case(user, case_id))
            if len(parts) == 4 and parts[3] == "participants" and method == "POST":
                d = self._body(); return self._send(201, store.add_participant(user, case_id, d.get("claim_id"), d.get("identity_relation", ""), d.get("authorization", ""), d.get("evidence_id")))
            if len(parts) == 6 and parts[3] == "participants" and parts[5] == "verify" and method == "POST":
                d = self._body(); return self._send(200, store.verify_participant(user, case_id, int(parts[4]), d.get("decision", ""), d.get("note", "")))
            if len(parts) == 4 and parts[3] == "sessions" and method == "POST":
                d = self._body(); return self._send(201, store.create_session(user, case_id, d.get("opinion", ""), d.get("outcome", "continue"), d.get("published", False), d.get("votes", []), d.get("basis", [])))
        if len(parts) == 4 and parts[:2] == ["api", "claims"] and parts[3] == "transition" and method == "POST":
            d = self._body(); return self._send(200, store.transition_claim(user, int(parts[2]), d.get("status", ""), d.get("note", "")))
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
