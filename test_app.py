import base64
import tempfile
import unittest
from pathlib import Path

from app import BusinessError, ProvenanceStore


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProvenanceStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_provenance_and_return_review_flow(self):
        source = self.store.add_source("staff", "馆藏购藏档案", "archive", "ACC-1999-7")
        obj = self.store.create_object("staff", "M-1999-7", "青铜器", "礼器", "市博物馆", "1999年入藏，来源待持续核验。")
        event = self.store.add_event("staff", obj["id"], "acquisition", "1999-07-01", "", "本市", "从私人藏家购入", source["id"], "public")
        evidence = self.store.upload_evidence("staff", obj["id"], "purchase.pdf", base64.b64encode(b"purchase record").decode(), "internal", event["id"])
        self.assertEqual(len(evidence["sha256"]), 64)
        updated = self.store.update_object("staff", obj["id"], {"public_summary": "已完成首轮来源整理。"})
        self.assertEqual(updated["version"], 3)
        claim = self.store.create_claim("claimant1", obj["id"], "王氏家族", "返还藏品")
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "材料齐全，进入调查。")
        self.store.transition_claim("reviewer1", claim["id"], "negotiating", "双方开始协商返还安排。")
        self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "签署返还协议。")
        public_view = self.store.get_object("public", obj["id"])
        self.assertNotIn("current_holder", public_view)
        self.assertEqual(len(public_view["events"]), 1)
        self.assertEqual(public_view["claims"][0]["status"], "resolved_return")
        claimant_view = self.store.get_object("claimant1", obj["id"])
        self.assertEqual(len(claimant_view["claims"]), 1)
        self.assertGreaterEqual(len(self.store.object_history("reviewer1", obj["id"])), 6)

    def test_visibility_and_claim_stage_invariants(self):
        obj = self.store.create_object("staff", "M-2001-2", "手稿", "纸质", "资料室", "公开简介。")
        claim = self.store.create_claim("claimant1", obj["id"], "捐赠人后代", "归还手稿")
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "直接结束。")
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.assertNotIn("claimant_id", self.store.get_object("public", obj["id"])["claims"][0])
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_event("public", obj["id"], "note", "2020-01-01", "", "馆内", "未授权事件", None, "public")
        self.assertEqual(ctx.exception.status, 403)


class JointCaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProvenanceStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.obj = self.store.create_object("staff", "M-1937-1", "石佛像", "雕塑", "库房", "战时流失，来源调查中。")
        self.source = self.store.add_source("staff", "战时掠夺档案", "archive", "WAR-1943-11")
        self.claim1 = self.store.create_claim("claimant1", self.obj["id"], "陈氏后裔", "返还佛像")
        self.claim2 = self.store.create_claim("claimant2", self.obj["id"], "本源寺管委会", "返还佛像")
        self.case = self.store.create_joint_case("staff", self.obj["id"], "石佛像返还共同审理", ["reviewer1", "reviewer2", "reviewer3"])

    def tearDown(self):
        self.tmp.cleanup()

    def _participant(self, claim, relation="直系后裔"):
        return self.store.add_participant("staff", self.case["id"], claim["id"], relation, "公证处授权委托书 No.7")

    def _verify_all(self):
        for p in self.store.get_joint_case("reviewer1", self.case["id"])["participants"]:
            self.store.verify_participant("reviewer1", self.case["id"], p["id"], "confirmed", "材料核验通过。")

    def _conclude(self, opinion="资格齐全，多数同意返还。"):
        return self.store.create_session(
            "reviewer1", self.case["id"], opinion, "return", True,
            [{"reviewer_id": "reviewer1", "vote": "agree"}, {"reviewer_id": "reviewer2", "vote": "agree"}],
            [{"kind": "source", "id": self.source["id"]}],
        )

    def test_qualification_gate_blocks_return_conclusion(self):
        self.assertEqual(self.case["status"], "pending_verification")
        p1 = self._participant(self.claim1)
        p2 = self._participant(self.claim2, "寺庙继承人")
        blocked = self.store.create_session(
            "reviewer1", self.case["id"], "拟同意返还，待资格确认。", "return", True,
            [{"reviewer_id": r, "vote": "agree"} for r in ("reviewer1", "reviewer2", "reviewer3")],
            [{"kind": "source", "id": self.source["id"]}],
        )
        self.assertEqual(blocked["conclusion_status"], "blocked_qualification")
        self.assertEqual(blocked["case_status"], "pending_verification")
        self.store.verify_participant("reviewer1", self.case["id"], p1["id"], "confirmed", "身份关系属实。")
        stage = self.store.verify_participant("reviewer1", self.case["id"], p2["id"], "confirmed", "授权材料齐全。")
        self.assertEqual(stage["case_status"], "deliberating")
        done = self._conclude()
        self.assertEqual(done["conclusion_status"], "effective")
        self.assertEqual(done["case_status"], "concluded")
        self.assertEqual(self.store.get_object("public", self.obj["id"])["joint_cases"][0]["stage"], "concluded")

    def test_rejected_participant_does_not_block(self):
        p1 = self._participant(self.claim1)
        p2 = self._participant(self.claim2, "寺庙继承人")
        self.store.verify_participant("reviewer1", self.case["id"], p1["id"], "confirmed", "通过。")
        result = self.store.verify_participant("reviewer1", self.case["id"], p2["id"], "rejected", "授权链断裂。")
        self.assertEqual(result["case_status"], "deliberating")

    def test_quorum_rules(self):
        self._participant(self.claim1)
        self._participant(self.claim2, "寺庙继承人")
        self._verify_all()
        one_present = self.store.create_session(
            "reviewer1", self.case["id"], "仅一人出席。", "return", False,
            [{"reviewer_id": "reviewer1", "vote": "agree"}],
            [{"kind": "source", "id": self.source["id"]}],
        )
        self.assertEqual(one_present["conclusion_status"], "not_effective")
        one_agree = self.store.create_session(
            "reviewer1", self.case["id"], "两人反对。", "return", False,
            [{"reviewer_id": "reviewer1", "vote": "agree"},
             {"reviewer_id": "reviewer2", "vote": "disagree"},
             {"reviewer_id": "reviewer3", "vote": "disagree"}],
            [{"kind": "source", "id": self.source["id"]}],
        )
        self.assertEqual(one_agree["conclusion_status"], "not_effective")
        two_of_three = self._conclude("两名审查员同意。")
        self.assertEqual(two_of_three["conclusion_status"], "effective")

    def test_new_material_invalidates_conclusion(self):
        self._participant(self.claim1)
        self._participant(self.claim2, "寺庙继承人")
        self._verify_all()
        ev = self.store.upload_evidence("staff", self.obj["id"], "1937-inventory.pdf", base64.b64encode(b"old inventory").decode(), "internal")
        self.store.create_session(
            "reviewer1", self.case["id"], "依据旧档案同意返还。", "return", True,
            [{"reviewer_id": "reviewer1", "vote": "agree"}, {"reviewer_id": "reviewer2", "vote": "agree"}],
            [{"kind": "evidence", "id": ev["id"]}],
        )
        self.store.upload_evidence("staff", self.obj["id"], "1944-ledger.pdf", base64.b64encode(b"new ledger").decode(), "internal")
        case = self.store.get_joint_case("reviewer1", self.case["id"])
        self.assertEqual(case["stage"], "deliberating")
        old = case["sessions"][0]
        self.assertEqual(old["conclusion_status"], "invalidated")
        self.assertEqual(old["invalidations"][0]["item_kind"], "evidence")
        self.assertEqual(old["invalidations"][0]["item_label"], "1944-ledger.pdf")
        again = self._conclude("复核后再次同意返还。")
        self.assertEqual(again["conclusion_status"], "effective")
        src2 = self.store.add_source("staff", "新发现海关记录", "archive", "CUSTOMS-1946")
        self.store.add_event("staff", self.obj["id"], "export", "1946-03-01", "", "上海", "海关出境记录", src2["id"], "internal")
        case = self.store.get_joint_case("reviewer1", self.case["id"])
        last = case["sessions"][-1]
        self.assertEqual(last["conclusion_status"], "invalidated")
        self.assertEqual(last["invalidations"][0]["item_kind"], "source")
        self.assertIn("新发现海关记录", last["invalidations"][0]["item_label"])
        self.assertEqual(case["stage"], "deliberating")

    def test_layered_views(self):
        self._participant(self.claim1)
        self._participant(self.claim2, "寺庙继承人")
        self._verify_all()
        self._conclude("公开意见：同意返还。")
        self.store.create_session("reviewer1", self.case["id"], "内部意见，不公开。", "continue", False, [], [])
        pub = self.store.get_joint_case("public", self.case["id"])
        self.assertEqual(pub["stage"], "concluded")
        self.assertEqual(len(pub["opinions"]), 1)
        self.assertNotIn("participants", pub)
        self.assertNotIn("sessions", pub)
        self.assertNotIn("panel", pub)
        mine = self.store.get_joint_case("claimant1", self.case["id"])
        self.assertEqual(mine["my_participation"]["qualification"], "confirmed")
        self.assertNotIn("participants", mine)
        full = self.store.get_joint_case("reviewer2", self.case["id"])
        self.assertEqual(len(full["participants"]), 2)
        self.assertEqual(full["qualification_conflicts"], [])
        self.assertEqual(len(full["sessions"][0]["votes"]), 2)
        self.assertEqual(full["sessions"][0]["basis"][0]["item_kind"], "source")

    def test_conflicts_and_permissions(self):
        self._participant(self.claim1)
        self._participant(self.claim2)
        full = self.store.get_joint_case("staff", self.case["id"])
        self.assertEqual(len(full["qualification_conflicts"]), 1)
        self.assertEqual(full["qualification_conflicts"][0]["identity_relation"], "直系后裔")
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_joint_case("claimant1", self.obj["id"], "越权建案", ["reviewer1", "reviewer2"])
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_participant("claimant1", self.case["id"], self.claim2["id"], "直系后裔", "伪造授权")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_participant("staff", self.case["id"], self.claim1["id"], "直系后裔", "重复登记")
        self.assertEqual(ctx.exception.code, "participant_exists")
        case2 = self.store.create_joint_case("staff", self.obj["id"], "另一共同审理", ["reviewer1", "reviewer2"])
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_session("reviewer3", case2["id"], "我不是合议成员。", "continue", False, [], [])
        self.assertEqual(ctx.exception.code, "not_panel_member")


if __name__ == "__main__":
    unittest.main()
